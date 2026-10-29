"""ReproLab HTTP API.

The API is a thin layer over the database and the worker. Starting a run only
inserts a row; the worker does the work. That is why refreshing the browser
loses nothing.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Literal

from fastapi import Depends, FastAPI, Header, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import PlainTextResponse, StreamingResponse
from pydantic import BaseModel, Field, field_validator

from . import __version__
from .config import Settings, get_settings
from .curated import RepoNotPermitted, assert_executable, catalogue, is_curated
from .db import (
    Database,
    PHASE_AWAITING_APPROVAL,
    PHASE_DONE,
    STATUS_NOT_ATTEMPTED,
    TERMINAL_STATUSES,
    utcnow,
)
from .example_run import EXAMPLE_RUN_ID, seed_example
from .providers.nebius import NebiusClient, NebiusError
from .providers.tavily import TavilyClient, TavilyError
from .runner.commands import ALLOWED_PROGRAMS
from .runner.docker_runner import RunnerUnavailable
from .runner.factory import build_runner
from .security.urls import UrlNotAllowed, normalize_github_repo, validate_public_url
from .worker import LogBus, Worker

MIN_BUDGET = 60
MAX_BUDGET = 1800

settings: Settings = get_settings()
settings.ensure_dirs()
db = Database(settings.db_path)
log_bus = LogBus()
worker = Worker(db, settings, log_bus)

app = FastAPI(
    title="ReproLab",
    version=__version__,
    description=(
        "An AI research engineering agent that prepares, runs and reports on a "
        "reproducibility check, backed by actual execution evidence."
    ),
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=list(settings.cors_origins),
    allow_credentials=False,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
)


# --- auth -------------------------------------------------------------------
def require_token(x_reprolab_token: str | None = Header(default=None)) -> None:
    """Shared-secret gate for write endpoints.

    Executing downloaded code must not be exposed anonymously. If
    REPROLAB_API_TOKEN is set, every write requires it.
    """
    if not settings.api_token:
        return
    if x_reprolab_token != settings.api_token:
        raise HTTPException(status_code=401, detail="invalid or missing X-ReproLab-Token")


# --- request models ---------------------------------------------------------
class CreateRunRequest(BaseModel):
    paper_url: str = Field(max_length=2048)
    repo_url: str = Field(max_length=2048)
    goal: str = Field(default="", max_length=1000)
    task_type: Literal["setup_check", "small_experiment"] = "setup_check"
    runtime_budget_seconds: int = Field(default=600, ge=MIN_BUDGET, le=MAX_BUDGET)

    @field_validator("paper_url", "repo_url")
    @classmethod
    def _strip(cls, value: str) -> str:
        return value.strip()


# --- serialisation ----------------------------------------------------------
def _run_summary(row: Any) -> dict[str, Any]:
    return {
        "id": row["id"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "started_at": row["started_at"],
        "finished_at": row["finished_at"],
        "paper_url": row["paper_url"],
        "repo_url": row["repo_url"],
        "repo_name": f"{row['repo_owner']}/{row['repo_name']}",
        "repo_commit": row["repo_commit"],
        "goal": row["goal"],
        "task_type": row["task_type"],
        "runtime_budget_seconds": row["runtime_budget_s"],
        "status": row["status"],
        "phase": row["phase"],
        "outcome": row["outcome"],
        "outcome_detail": row["outcome_detail"],
        "model_used": row["model_used"],
        "repair_attempts": row["repair_attempts"],
        "cancel_requested": bool(row["cancel_requested"]),
        "is_example": bool(row["is_example"]),
        "is_terminal": row["status"] in TERMINAL_STATUSES,
        "has_report": bool(row["report_md"]),
    }


def _run_detail(row: Any) -> dict[str, Any]:
    detail = _run_summary(row)
    detail["plan"] = json.loads(row["plan_json"]) if row["plan_json"] else None
    detail["manifest"] = json.loads(row["manifest_json"]) if row["manifest_json"] else None
    detail["environment"] = (
        json.loads(row["environment_json"]) if row["environment_json"] else None
    )
    detail["error"] = row["error"]
    run_id = row["id"]
    detail["events"] = [
        {
            "id": e["id"],
            "ts": e["ts"],
            "kind": e["kind"],
            "title": e["title"],
            "detail": e["detail"],
            "data": json.loads(e["data_json"]) if e["data_json"] else None,
        }
        for e in db.list_events(run_id)
    ]
    detail["steps"] = [
        {
            "evidence_id": s["id"],
            "index": s["idx"],
            "kind": s["kind"],
            "purpose": s["purpose"],
            "command": s["command"],
            "phase": s["phase"],
            "network": s["network"],
            "workdir": s["workdir"],
            "status": s["status"],
            "exit_code": s["exit_code"],
            "duration_ms": s["duration_ms"],
            "log_bytes": s["log_bytes"],
            "truncated": bool(s["truncated"]),
            "timed_out": bool(s["timed_out"]),
            "repair_attempt": s["repair_index"],
        }
        for s in db.list_steps(run_id)
    ]
    detail["sources"] = [
        {
            "kind": s["kind"],
            "title": s["title"],
            "url": s["url"],
            "snippet": s["snippet"],
            "retrieved_via": s["retrieved_via"],
        }
        for s in db.list_sources(run_id)
    ]
    detail["patches"] = [
        {
            "attempt": p["attempt"],
            "path": p["path"],
            "diff": p["diff"],
            "rationale": p["rationale"],
            "applied": bool(p["applied"]),
        }
        for p in db.list_patches(run_id)
    ]
    detail["metrics"] = [
        {
            "name": m["name"],
            "value": m["value"],
            "raw_value": m["raw_value"],
            "unit": m["unit"],
            "parser": m["parser"],
            "provenance": m["provenance"],
            "evidence_id": m["step_id"],
        }
        for m in db.list_metrics(run_id)
    ]
    detail["agent_actions"] = [
        {
            "ts": t["ts"],
            "tool": t["tool"],
            "ok": bool(t["ok"]),
            "duration_ms": t["duration_ms"],
            "summary": t["summary"],
            "input": json.loads(t["input_json"]) if t["input_json"] else None,
            "output": json.loads(t["output_json"]) if t["output_json"] else None,
        }
        for t in db.list_tool_calls(run_id)
    ]
    return detail


def _require_run(run_id: str) -> Any:
    row = db.get_run(run_id)
    if row is None:
        raise HTTPException(status_code=404, detail="run not found")
    return row


# --- lifecycle --------------------------------------------------------------
@app.on_event("startup")
def _startup() -> None:
    settings.ensure_dirs()
    seed_example(db, settings)
    worker.start()


@app.on_event("shutdown")
def _shutdown() -> None:
    worker.stop()


# --- system -----------------------------------------------------------------
@app.get("/api/health")
def health() -> dict[str, Any]:
    """Configuration and capability report. Never returns secret values."""
    runner_status: dict[str, Any] = {"backend": settings.runner_backend}
    try:
        runner = build_runner(settings)
        runner.preflight()
        runner_status.update({"available": True, **runner.runtime_info()})
    except RunnerUnavailable as exc:
        runner_status.update({"available": False, "error": str(exc)})

    return {
        "service": "reprolab",
        "version": __version__,
        "status": "ok",
        "integrations": {
            "nebius_token_factory": {
                "configured": settings.has_nebius,
                "base_url": settings.nebius_base_url,
                "requested_model": settings.nebius_model,
                "autoselect": settings.nebius_model_autoselect,
                "role": "NVIDIA model inference (planning, diagnosis, reporting)",
            },
            "tavily": {
                "configured": settings.has_tavily,
                "role": "paper pages, official documentation, error troubleshooting",
            },
        },
        "runner": runner_status,
        "policy": {
            "curated_repositories_only": not settings.allow_arbitrary_repos,
            "api_token_required": bool(settings.api_token),
            "allowed_programs": sorted(ALLOWED_PROGRAMS),
        },
        "limits": {
            "max_runtime_seconds": settings.limits.max_runtime_seconds,
            "step_timeout_seconds": settings.limits.step_timeout_seconds,
            "container_cpus": settings.limits.container_cpus,
            "container_memory_mb": settings.limits.container_memory_mb,
            "max_log_bytes": settings.limits.max_log_bytes,
            "max_repairs": settings.limits.max_repairs,
            "runtime_budget_bounds": [MIN_BUDGET, MAX_BUDGET],
            "gpu": False,
        },
    }


@app.get("/api/integrations/verify")
def verify_integrations(_: None = Depends(require_token)) -> dict[str, Any]:
    """Make one real call to each configured provider.

    Used to prove the integrations work, and to say plainly which are
    unverified when they do not.
    """
    report: dict[str, Any] = {"checked_at": utcnow()}

    if not settings.has_nebius:
        report["nebius_token_factory"] = {
            "ok": False,
            "verified": False,
            "error": "NEBIUS_API_KEY is not configured",
        }
    else:
        try:
            client = NebiusClient(
                api_key=settings.nebius_api_key,
                base_url=settings.nebius_base_url,
                model=settings.nebius_model,
                autoselect=settings.nebius_model_autoselect,
            )
            report["nebius_token_factory"] = {"verified": True, **client.healthcheck()}
        except NebiusError as exc:
            report["nebius_token_factory"] = {
                "ok": False,
                "verified": False,
                "error": str(exc),
            }

    if not settings.has_tavily:
        report["tavily"] = {
            "ok": False,
            "verified": False,
            "error": "TAVILY_API_KEY is not configured",
        }
    else:
        try:
            client = TavilyClient(
                api_key=settings.tavily_api_key, base_url=settings.tavily_base_url
            )
            report["tavily"] = {"verified": True, **client.healthcheck()}
        except TavilyError as exc:
            report["tavily"] = {"ok": False, "verified": False, "error": str(exc)}

    try:
        runner = build_runner(settings)
        runner.preflight()
        state = runner.ensure_image()
        report["runner"] = {
            "ok": True,
            "verified": True,
            "image_state": state,
            **runner.runtime_info(),
        }
    except RunnerUnavailable as exc:
        report["runner"] = {"ok": False, "verified": False, "error": str(exc)}

    return report


@app.get("/api/curated")
def curated_repositories() -> dict[str, Any]:
    return {
        "curated_only": not settings.allow_arbitrary_repos,
        "repositories": catalogue(),
    }


# --- runs -------------------------------------------------------------------
@app.post("/api/runs", status_code=201)
def create_run(
    request: CreateRunRequest, _: None = Depends(require_token)
) -> dict[str, Any]:
    try:
        validate_public_url(request.paper_url, resolve=False)
    except UrlNotAllowed as exc:
        raise HTTPException(status_code=400, detail=f"paper_url: {exc}") from exc
    try:
        owner, repo, canonical = normalize_github_repo(request.repo_url)
    except UrlNotAllowed as exc:
        raise HTTPException(status_code=400, detail=f"repo_url: {exc}") from exc
    try:
        assert_executable(canonical, allow_arbitrary=settings.allow_arbitrary_repos)
    except RepoNotPermitted as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc

    run_id = db.create_run(
        paper_url=request.paper_url,
        repo_url=canonical,
        repo_owner=owner,
        repo_name=repo,
        goal=request.goal,
        task_type=request.task_type,
        runtime_budget_s=request.runtime_budget_seconds,
        runner_backend=settings.runner_backend,
    )
    db.add_event(
        run_id,
        "created",
        "Run queued",
        f"{owner}/{repo} — {request.task_type.replace('_', ' ')}",
        {"curated": is_curated(canonical)},
    )
    return {"id": run_id, "status": "Queued"}


@app.get("/api/runs")
def list_runs(limit: int = Query(default=50, ge=1, le=200)) -> dict[str, Any]:
    rows = db.list_runs(limit)
    real = [r for r in rows if not r["is_example"]]
    return {
        "runs": [_run_summary(r) for r in rows],
        # Statistics deliberately exclude the example so it can never be
        # mistaken for a real completed run.
        "statistics": {
            "total_real_runs": len(real),
            "completed": sum(1 for r in real if r["status"] == "Completed"),
            "failed": sum(1 for r in real if r["status"] == "Failed"),
            "blocked": sum(1 for r in real if r["status"] == "Blocked"),
            "cancelled": sum(1 for r in real if r["status"] == "Cancelled"),
            "example_runs_excluded": sum(1 for r in rows if r["is_example"]),
        },
    }


@app.get("/api/runs/{run_id}")
def get_run(run_id: str) -> dict[str, Any]:
    return _run_detail(_require_run(run_id))


@app.post("/api/runs/{run_id}/approve")
def approve_run(run_id: str, _: None = Depends(require_token)) -> dict[str, Any]:
    """The approval gate. Execution of downloaded code begins only here."""
    row = _require_run(run_id)
    if row["is_example"]:
        raise HTTPException(
            status_code=400, detail="the example report is not an executable run"
        )
    if row["phase"] != PHASE_AWAITING_APPROVAL:
        raise HTTPException(
            status_code=409,
            detail=f"run is in phase {row['phase']!r}, not awaiting approval",
        )
    if not row["plan_json"]:
        raise HTTPException(status_code=409, detail="run has no plan to approve")
    db.update_run(run_id, phase="approved", status="Active")
    db.add_event(
        run_id,
        "approval",
        "Execution approved by the user",
        "ReproLab will now run the approved commands in the isolated runner.",
    )
    return {"id": run_id, "status": "Active", "phase": "approved"}


@app.post("/api/runs/{run_id}/cancel")
def cancel_run(run_id: str, _: None = Depends(require_token)) -> dict[str, Any]:
    row = _require_run(run_id)
    if row["is_example"]:
        raise HTTPException(status_code=400, detail="the example report is not a run")
    if row["status"] in TERMINAL_STATUSES:
        return {"id": run_id, "status": row["status"], "already_finished": True}
    killed = worker.cancel(run_id)
    return {"id": run_id, "status": "Cancelled", "terminated_execution": killed}


@app.post("/api/runs/{run_id}/decline")
def decline_run(run_id: str, _: None = Depends(require_token)) -> dict[str, Any]:
    """Reject a plan without running it: recorded as 'Not attempted'."""
    row = _require_run(run_id)
    if row["phase"] != PHASE_AWAITING_APPROVAL:
        raise HTTPException(status_code=409, detail="run is not awaiting approval")
    db.update_run(
        run_id,
        status=STATUS_NOT_ATTEMPTED,
        phase=PHASE_DONE,
        outcome=STATUS_NOT_ATTEMPTED,
        outcome_detail="The plan was declined, so no repository code was executed.",
        finished_at=utcnow(),
    )
    db.add_event(run_id, "approval", "Plan declined", "No code was executed.")
    return {"id": run_id, "status": STATUS_NOT_ATTEMPTED}


# --- live progress ----------------------------------------------------------
@app.get("/api/runs/{run_id}/logs")
def run_logs(
    run_id: str, after: int = Query(default=0, ge=0)
) -> dict[str, Any]:
    """Live log tail for a running run, plus persisted logs once finished."""
    row = _require_run(run_id)
    live = log_bus.since(run_id, after)
    return {
        "run_id": run_id,
        "status": row["status"],
        "chunks": live,
        "last_seq": live[-1]["seq"] if live else after,
    }


@app.get("/api/runs/{run_id}/logs/{evidence_id}", response_class=PlainTextResponse)
def step_log(run_id: str, evidence_id: str) -> str:
    """The recorded log file for one evidence id."""
    _require_run(run_id)
    row = db.query_one(
        "SELECT log_path FROM steps WHERE run_id = ? AND id = ?", (run_id, evidence_id)
    )
    if row is None:
        raise HTTPException(status_code=404, detail="evidence id not found")
    if not row["log_path"]:
        return "(no log was recorded for this step)"
    # Confine reads to this run's own artifact directory.
    expected_root = (settings.artifacts_dir / run_id).resolve()
    path = Path(row["log_path"]).resolve()
    try:
        path.relative_to(expected_root)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="log path is out of bounds") from exc
    if not path.is_file():
        return "(the log file is no longer available)"
    return path.read_text(encoding="utf-8", errors="replace")


@app.get("/api/runs/{run_id}/events")
def run_events(run_id: str, after: int = Query(default=0, ge=0)) -> dict[str, Any]:
    row = _require_run(run_id)
    events = db.list_events(run_id, after)
    return {
        "run_id": run_id,
        "status": row["status"],
        "phase": row["phase"],
        "is_terminal": row["status"] in TERMINAL_STATUSES,
        "events": [
            {
                "id": e["id"],
                "ts": e["ts"],
                "kind": e["kind"],
                "title": e["title"],
                "detail": e["detail"],
                "data": json.loads(e["data_json"]) if e["data_json"] else None,
            }
            for e in events
        ],
        "last_id": events[-1]["id"] if events else after,
    }


@app.get("/api/runs/{run_id}/stream")
def stream_run(run_id: str, after: int = Query(default=0, ge=0)) -> StreamingResponse:
    """Server-sent events for timeline and log updates.

    Polling the same endpoints works identically; this is the low-latency path.
    """
    _require_run(run_id)

    def generator():
        import time as _time

        last_event = after
        last_log = 0
        deadline = _time.monotonic() + 1800
        yield "retry: 2000\n\n"
        while _time.monotonic() < deadline:
            row = db.get_run(run_id)
            if row is None:
                break
            events = db.list_events(run_id, last_event)
            for event in events:
                last_event = event["id"]
                payload = {
                    "id": event["id"],
                    "ts": event["ts"],
                    "kind": event["kind"],
                    "title": event["title"],
                    "detail": event["detail"],
                    "data": json.loads(event["data_json"]) if event["data_json"] else None,
                }
                yield f"event: timeline\ndata: {json.dumps(payload)}\n\n"
            chunks = log_bus.since(run_id, last_log)
            for chunk in chunks:
                last_log = chunk["seq"]
                yield f"event: log\ndata: {json.dumps(chunk)}\n\n"
            state = {
                "status": row["status"],
                "phase": row["phase"],
                "is_terminal": row["status"] in TERMINAL_STATUSES,
            }
            yield f"event: state\ndata: {json.dumps(state)}\n\n"
            if row["status"] in TERMINAL_STATUSES:
                yield "event: done\ndata: {}\n\n"
                break
            _time.sleep(1.0)

    return StreamingResponse(
        generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# --- downloads --------------------------------------------------------------
@app.get("/api/runs/{run_id}/report.md", response_class=PlainTextResponse)
def report_markdown(run_id: str) -> str:
    row = _require_run(run_id)
    if not row["report_md"]:
        raise HTTPException(status_code=409, detail="this run has no report yet")
    return row["report_md"]


@app.get("/api/runs/{run_id}/manifest.json")
def report_manifest(run_id: str) -> Any:
    row = _require_run(run_id)
    if not row["manifest_json"]:
        raise HTTPException(status_code=409, detail="this run has no manifest yet")
    return json.loads(row["manifest_json"])


@app.get("/api/example")
def example_report() -> dict[str, Any]:
    row = db.get_run(EXAMPLE_RUN_ID)
    if row is None:
        seed_example(db, settings)
        row = db.get_run(EXAMPLE_RUN_ID)
    detail = _run_detail(row)
    detail["notice"] = (
        "This is a labelled example of ReproLab's report format, not a real "
        "run. It is excluded from run history statistics."
    )
    return detail
