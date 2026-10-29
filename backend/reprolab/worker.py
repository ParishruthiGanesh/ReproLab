"""Background worker.

Runs live in SQLite, not in a request. The worker polls for queued and
approved runs and drives them through the agent loop on its own threads, so a
run keeps going -- and keeps its history -- when the browser is refreshed or
closed. Progress reaches the UI through the database and a small in-memory
log fan-out for live streaming.
"""

from __future__ import annotations

import threading
import time
from collections import defaultdict, deque
from typing import Any

from .agent.loop import AgentRun
from .config import Settings
from .db import (
    Database,
    PHASE_DONE,
    PHASE_QUEUED,
    STATUS_CANCELLED,
    STATUS_FAILED,
    utcnow,
)
from .providers.nebius import NebiusClient, NebiusError
from .providers.tavily import TavilyClient, TavilyError
from .runner.docker_runner import RunnerUnavailable
from .runner.factory import build_runner

POLL_INTERVAL = 1.0
MAX_LIVE_LOG_LINES = 500


class LogBus:
    """Keeps the tail of each run's live output for streaming clients."""

    def __init__(self, max_lines: int = MAX_LIVE_LOG_LINES) -> None:
        self._lock = threading.Lock()
        self._buffers: dict[str, deque[dict[str, Any]]] = defaultdict(
            lambda: deque(maxlen=max_lines)
        )
        self._counters: dict[str, int] = defaultdict(int)

    def publish(self, run_id: str, step_id: str, chunk: str) -> None:
        with self._lock:
            self._counters[run_id] += 1
            self._buffers[run_id].append(
                {"seq": self._counters[run_id], "step_id": step_id, "text": chunk}
            )

    def since(self, run_id: str, after_seq: int = 0) -> list[dict[str, Any]]:
        with self._lock:
            return [
                item for item in self._buffers.get(run_id, ()) if item["seq"] > after_seq
            ]

    def drop(self, run_id: str) -> None:
        with self._lock:
            self._buffers.pop(run_id, None)
            self._counters.pop(run_id, None)


class Worker:
    """Polls the database and drives runs. One thread per active run."""

    def __init__(self, db: Database, settings: Settings, log_bus: LogBus) -> None:
        self.db = db
        self.settings = settings
        self.log_bus = log_bus
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._active: dict[str, threading.Thread] = {}
        self._runners: dict[str, Any] = {}
        self._lock = threading.Lock()
        self.max_concurrent = 2

    # -- lifecycle ----------------------------------------------------------
    def start(self) -> None:
        if self._thread is not None:
            return
        self._reclaim_interrupted_runs()
        self._thread = threading.Thread(target=self._loop, name="reprolab-worker", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 10.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            self._thread = None
        with self._lock:
            runners = list(self._runners.items())
        for run_id, runner in runners:
            try:
                runner.cancel(run_id)
            except Exception:  # pragma: no cover - defensive
                pass

    def _reclaim_interrupted_runs(self) -> None:
        """Mark runs left mid-flight by a crash, rather than leaving them 'Active'."""
        stuck = self.db.query(
            "SELECT id, phase FROM runs WHERE phase NOT IN (?, 'awaiting_approval', ?)"
            " AND is_example = 0",
            (PHASE_QUEUED, PHASE_DONE),
        )
        for row in stuck:
            self.db.update_run(
                row["id"],
                status=STATUS_FAILED,
                phase=PHASE_DONE,
                outcome=STATUS_FAILED,
                outcome_detail=(
                    "The backend restarted while this run was in progress, so it "
                    "was not completed. Start a new run."
                ),
                finished_at=utcnow(),
            )
            self.db.add_event(
                row["id"],
                "error",
                "Run interrupted by a backend restart",
                "ReproLab does not resume partially-executed runs, because the "
                "disposable workspace does not survive a restart.",
            )

    # -- main loop ----------------------------------------------------------
    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self._reap()
                if len(self._active) < self.max_concurrent:
                    claimed = self.db.claim_next_queued_run()
                    if claimed is not None:
                        self._spawn(claimed, phase="research")
                        continue
                if len(self._active) < self.max_concurrent:
                    claimed = self.db.claim_next_approved_run()
                    if claimed is not None:
                        self._spawn(claimed, phase="execute")
                        continue
            except Exception:  # pragma: no cover - the worker must not die
                pass
            self._stop.wait(POLL_INTERVAL)

    def _reap(self) -> None:
        with self._lock:
            finished = [rid for rid, t in self._active.items() if not t.is_alive()]
            for rid in finished:
                self._active.pop(rid, None)
                self._runners.pop(rid, None)

    def _spawn(self, run_row: Any, *, phase: str) -> None:
        run_id = run_row["id"]
        thread = threading.Thread(
            target=self._drive,
            args=(run_row, phase),
            name=f"reprolab-run-{run_id}",
            daemon=True,
        )
        with self._lock:
            self._active[run_id] = thread
        thread.start()

    # -- driving one run ----------------------------------------------------
    def _build_clients(self, run_id: str) -> tuple[Any | None, Any | None]:
        nebius = None
        tavily = None
        if self.settings.has_nebius:
            try:
                nebius = NebiusClient(
                    api_key=self.settings.nebius_api_key,
                    base_url=self.settings.nebius_base_url,
                    model=self.settings.nebius_model,
                    autoselect=self.settings.nebius_model_autoselect,
                )
                resolution = nebius.resolve_model()
                if resolution.substituted and resolution.note:
                    self.db.add_event(
                        run_id, "model", "Model id resolved", resolution.note
                    )
                self.db.add_event(
                    run_id,
                    "model",
                    f"Using {resolution.resolved}",
                    "Served by Nebius Token Factory for inference only.",
                )
            except NebiusError as exc:
                self.db.add_event(
                    run_id, "error", "Nebius Token Factory unavailable", str(exc)[:1000]
                )
                nebius = None
        else:
            self.db.add_event(
                run_id,
                "error",
                "NEBIUS_API_KEY is not configured",
                "ReproLab needs an NVIDIA model served by Nebius Token Factory "
                "to plan an experiment.",
            )
        if self.settings.has_tavily:
            try:
                tavily = TavilyClient(
                    api_key=self.settings.tavily_api_key,
                    base_url=self.settings.tavily_base_url,
                )
            except TavilyError as exc:
                self.db.add_event(run_id, "error", "Tavily unavailable", str(exc)[:500])
                tavily = None
        else:
            self.db.add_event(
                run_id,
                "warning",
                "TAVILY_API_KEY is not configured",
                "External documentation retrieval is disabled for this run; the "
                "agent will work from the repository and paper URL only.",
            )
        return nebius, tavily

    def _drive(self, run_row: Any, phase: str) -> None:
        run_id = run_row["id"]
        try:
            nebius, tavily = self._build_clients(run_id)
            try:
                runner = build_runner(self.settings)
            except RunnerUnavailable as exc:
                self.db.add_event(run_id, "error", "Runner unavailable", str(exc)[:1000])
                runner = None
            if runner is not None:
                with self._lock:
                    self._runners[run_id] = runner

            fresh = self.db.get_run(run_id) or run_row
            agent = AgentRun(
                db=self.db,
                settings=self.settings,
                run_row=fresh,
                nebius=nebius,
                tavily=tavily,
                runner=runner,
                log_sink=lambda step_id, chunk: self.log_bus.publish(
                    run_id, step_id, chunk
                ),
            )
            if phase == "research":
                agent.research_and_plan()
            else:
                agent.execute()
        except Exception as exc:  # pragma: no cover - last resort
            self.db.add_event(run_id, "error", "Worker error", str(exc)[:2000])
            self.db.update_run(
                run_id,
                status=STATUS_FAILED,
                phase=PHASE_DONE,
                outcome=STATUS_FAILED,
                outcome_detail=f"Worker error: {exc}",
                finished_at=utcnow(),
            )

    # -- cancellation -------------------------------------------------------
    def cancel(self, run_id: str) -> bool:
        """Request cancellation and kill any container the run is executing."""
        self.db.update_run(run_id, cancel_requested=1)
        self.db.add_event(
            run_id, "cancel", "Cancellation requested", "Terminating execution."
        )
        with self._lock:
            runner = self._runners.get(run_id)
            thread = self._active.get(run_id)
        killed = False
        if runner is not None:
            try:
                killed = runner.cancel(run_id)
            except Exception:  # pragma: no cover - defensive
                killed = False
        if thread is None:
            # Nothing is running: finalise immediately so the UI is accurate.
            row = self.db.get_run(run_id)
            if row is not None and row["phase"] != PHASE_DONE:
                self.db.update_run(
                    run_id,
                    status=STATUS_CANCELLED,
                    phase=PHASE_DONE,
                    outcome=STATUS_CANCELLED,
                    outcome_detail="Cancelled before execution started.",
                    finished_at=utcnow(),
                )
        return killed

    def is_active(self, run_id: str) -> bool:
        with self._lock:
            thread = self._active.get(run_id)
        return thread is not None and thread.is_alive()
