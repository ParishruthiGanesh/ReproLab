"""SQLite persistence for runs and their evidence.

Run state lives in the database, not in a request handler, so a run keeps
progressing (and keeps its history) across browser refreshes and restarts.
Every row that can hold third-party text is redacted before it is written.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from .security.redact import redact, redact_obj

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id                  TEXT PRIMARY KEY,
    created_at          TEXT NOT NULL,
    updated_at          TEXT NOT NULL,
    started_at          TEXT,
    finished_at         TEXT,
    paper_url           TEXT NOT NULL,
    repo_url            TEXT NOT NULL,
    repo_owner          TEXT NOT NULL,
    repo_name           TEXT NOT NULL,
    repo_commit         TEXT,
    goal                TEXT NOT NULL DEFAULT '',
    task_type           TEXT NOT NULL,
    runtime_budget_s    INTEGER NOT NULL,
    status              TEXT NOT NULL,
    phase               TEXT NOT NULL DEFAULT 'queued',
    outcome             TEXT,
    outcome_detail      TEXT,
    plan_json           TEXT,
    report_md           TEXT,
    manifest_json       TEXT,
    model_used          TEXT,
    environment_json    TEXT,
    error               TEXT,
    repair_attempts     INTEGER NOT NULL DEFAULT 0,
    cancel_requested    INTEGER NOT NULL DEFAULT 0,
    is_example          INTEGER NOT NULL DEFAULT 0,
    runner_backend      TEXT
);

CREATE TABLE IF NOT EXISTS events (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id    TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    ts        TEXT NOT NULL,
    kind      TEXT NOT NULL,
    title     TEXT NOT NULL,
    detail    TEXT NOT NULL DEFAULT '',
    data_json TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_run ON events(run_id, id);

CREATE TABLE IF NOT EXISTS tool_calls (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id      TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    ts          TEXT NOT NULL,
    tool        TEXT NOT NULL,
    input_json  TEXT NOT NULL,
    output_json TEXT NOT NULL,
    ok          INTEGER NOT NULL,
    duration_ms INTEGER NOT NULL DEFAULT 0,
    summary     TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_tool_calls_run ON tool_calls(run_id, id);

CREATE TABLE IF NOT EXISTS steps (
    id           TEXT PRIMARY KEY,
    run_id       TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    idx          INTEGER NOT NULL,
    kind         TEXT NOT NULL,
    purpose      TEXT NOT NULL DEFAULT '',
    command      TEXT NOT NULL,
    argv_json    TEXT NOT NULL,
    phase        TEXT NOT NULL,
    workdir      TEXT NOT NULL DEFAULT '.',
    status        TEXT NOT NULL,
    exit_code    INTEGER,
    started_at   TEXT,
    finished_at  TEXT,
    duration_ms  INTEGER,
    log_path     TEXT,
    log_bytes    INTEGER NOT NULL DEFAULT 0,
    truncated    INTEGER NOT NULL DEFAULT 0,
    timed_out    INTEGER NOT NULL DEFAULT 0,
    network      TEXT NOT NULL DEFAULT 'none',
    repair_index INTEGER
);
CREATE INDEX IF NOT EXISTS idx_steps_run ON steps(run_id, idx);

CREATE TABLE IF NOT EXISTS sources (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id    TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    ts        TEXT NOT NULL,
    kind      TEXT NOT NULL,
    title     TEXT NOT NULL DEFAULT '',
    url       TEXT NOT NULL DEFAULT '',
    snippet   TEXT NOT NULL DEFAULT '',
    retrieved_via TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_sources_run ON sources(run_id, id);

CREATE TABLE IF NOT EXISTS patches (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id     TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    ts         TEXT NOT NULL,
    attempt    INTEGER NOT NULL,
    path       TEXT NOT NULL,
    diff       TEXT NOT NULL,
    rationale  TEXT NOT NULL DEFAULT '',
    applied    INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_patches_run ON patches(run_id, id);

CREATE TABLE IF NOT EXISTS metrics (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id       TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    step_id      TEXT NOT NULL,
    name         TEXT NOT NULL,
    value        REAL,
    raw_value    TEXT NOT NULL DEFAULT '',
    unit         TEXT NOT NULL DEFAULT '',
    parser       TEXT NOT NULL,
    provenance   TEXT NOT NULL,
    source_file  TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_metrics_run ON metrics(run_id, id);
"""

# --- run lifecycle vocabulary ----------------------------------------------
# In-flight phases.
PHASE_QUEUED = "queued"
PHASE_RESEARCHING = "researching"
PHASE_AWAITING_APPROVAL = "awaiting_approval"
PHASE_RUNNING = "running"
PHASE_REPAIRING = "repairing"
PHASE_REPORTING = "reporting"
PHASE_DONE = "done"

# Terminal statuses, deliberately distinct (see docs/ARCHITECTURE.md).
STATUS_QUEUED = "Queued"
STATUS_ACTIVE = "Active"
STATUS_AWAITING_APPROVAL = "Awaiting approval"
STATUS_COMPLETED = "Completed"
STATUS_FAILED = "Failed"
STATUS_BLOCKED = "Blocked"
STATUS_CANCELLED = "Cancelled"
STATUS_NOT_ATTEMPTED = "Not attempted"

TERMINAL_STATUSES = frozenset(
    {
        STATUS_COMPLETED,
        STATUS_FAILED,
        STATUS_BLOCKED,
        STATUS_CANCELLED,
        STATUS_NOT_ATTEMPTED,
    }
)


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def new_id(prefix: str = "run") -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


class Database:
    """Thread-safe SQLite wrapper.

    A lock serialises writes; SQLite in WAL mode handles concurrent readers,
    which is what the API needs while the worker thread writes progress.
    """

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._local = threading.local()
        # executescript() implicitly commits, so it runs outside the
        # transaction helper below.
        with self._lock:
            self._conn().executescript(SCHEMA)

    def _conn(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(self.path, timeout=30.0, isolation_level=None)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("PRAGMA busy_timeout=30000")
            self._local.conn = conn
        return conn

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        """Run a block inside one immediate transaction.

        Statements that commit implicitly (e.g. ``executescript``) can leave no
        transaction active, so commit and rollback are both guarded.
        """
        with self._lock:
            conn = self._conn()
            try:
                conn.execute("BEGIN IMMEDIATE")
                yield conn
                if conn.in_transaction:
                    conn.execute("COMMIT")
            except Exception:
                try:
                    if conn.in_transaction:
                        conn.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
                raise

    def query(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        with self._lock:
            return list(self._conn().execute(sql, params).fetchall())

    def query_one(self, sql: str, params: tuple = ()) -> sqlite3.Row | None:
        rows = self.query(sql, params)
        return rows[0] if rows else None

    def execute(self, sql: str, params: tuple = ()) -> None:
        with self.connect() as conn:
            conn.execute(sql, params)

    # -- runs ---------------------------------------------------------------
    def create_run(
        self,
        *,
        paper_url: str,
        repo_url: str,
        repo_owner: str,
        repo_name: str,
        goal: str,
        task_type: str,
        runtime_budget_s: int,
        runner_backend: str,
        is_example: bool = False,
        status: str = STATUS_QUEUED,
        phase: str = PHASE_QUEUED,
        run_id: str | None = None,
    ) -> str:
        rid = run_id or new_id()
        now = utcnow()
        self.execute(
            """INSERT INTO runs (id, created_at, updated_at, paper_url, repo_url,
                   repo_owner, repo_name, goal, task_type, runtime_budget_s,
                   status, phase, is_example, runner_backend)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                rid,
                now,
                now,
                paper_url,
                repo_url,
                repo_owner,
                repo_name,
                redact(goal)[:2000],
                task_type,
                int(runtime_budget_s),
                status,
                phase,
                1 if is_example else 0,
                runner_backend,
            ),
        )
        return rid

    def update_run(self, run_id: str, **fields: Any) -> None:
        if not fields:
            return
        allowed = {
            "started_at",
            "finished_at",
            "repo_commit",
            "status",
            "phase",
            "outcome",
            "outcome_detail",
            "plan_json",
            "report_md",
            "manifest_json",
            "model_used",
            "environment_json",
            "error",
            "repair_attempts",
            "cancel_requested",
        }
        unknown = set(fields) - allowed
        if unknown:
            raise ValueError(f"cannot update unknown run fields: {sorted(unknown)}")
        sets = ", ".join(f"{key} = ?" for key in fields)
        values = []
        for key, value in fields.items():
            if isinstance(value, str) and key not in {"plan_json", "manifest_json"}:
                value = redact(value)
            values.append(value)
        values.append(utcnow())
        values.append(run_id)
        self.execute(
            f"UPDATE runs SET {sets}, updated_at = ? WHERE id = ?", tuple(values)
        )

    def get_run(self, run_id: str) -> sqlite3.Row | None:
        return self.query_one("SELECT * FROM runs WHERE id = ?", (run_id,))

    def list_runs(self, limit: int = 50) -> list[sqlite3.Row]:
        return self.query(
            "SELECT * FROM runs ORDER BY created_at DESC, rowid DESC LIMIT ?",
            (int(limit),),
        )

    def claim_next_queued_run(self) -> sqlite3.Row | None:
        """Atomically move one queued run into the researching phase."""
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM runs WHERE phase = ? AND is_example = 0 "
                "ORDER BY created_at LIMIT 1",
                (PHASE_QUEUED,),
            ).fetchone()
            if row is None:
                return None
            conn.execute(
                "UPDATE runs SET phase = ?, status = ?, started_at = "
                "COALESCE(started_at, ?), updated_at = ? WHERE id = ?",
                (PHASE_RESEARCHING, STATUS_ACTIVE, utcnow(), utcnow(), row["id"]),
            )
            return row

    def claim_next_approved_run(self) -> sqlite3.Row | None:
        """Atomically move one approved run into the running phase."""
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM runs WHERE phase = ? AND is_example = 0 "
                "ORDER BY created_at LIMIT 1",
                ("approved",),
            ).fetchone()
            if row is None:
                return None
            conn.execute(
                "UPDATE runs SET phase = ?, status = ?, updated_at = ? WHERE id = ?",
                (PHASE_RUNNING, STATUS_ACTIVE, utcnow(), row["id"]),
            )
            return row

    def cancel_requested(self, run_id: str) -> bool:
        row = self.query_one("SELECT cancel_requested FROM runs WHERE id = ?", (run_id,))
        return bool(row and row["cancel_requested"])

    # -- events -------------------------------------------------------------
    def add_event(
        self,
        run_id: str,
        kind: str,
        title: str,
        detail: str = "",
        data: Any | None = None,
    ) -> int:
        payload = (
            json.dumps(redact_obj(data), ensure_ascii=False, default=str)
            if data is not None
            else None
        )
        with self.connect() as conn:
            cursor = conn.execute(
                "INSERT INTO events (run_id, ts, kind, title, detail, data_json) "
                "VALUES (?,?,?,?,?,?)",
                (run_id, utcnow(), kind, redact(title)[:300], redact(detail)[:4000], payload),
            )
            return int(cursor.lastrowid or 0)

    def list_events(self, run_id: str, after_id: int = 0) -> list[sqlite3.Row]:
        return self.query(
            "SELECT * FROM events WHERE run_id = ? AND id > ? ORDER BY id",
            (run_id, int(after_id)),
        )

    # -- tool calls ---------------------------------------------------------
    def add_tool_call(
        self,
        run_id: str,
        tool: str,
        tool_input: Any,
        tool_output: Any,
        ok: bool,
        duration_ms: int,
        summary: str = "",
    ) -> None:
        self.execute(
            "INSERT INTO tool_calls (run_id, ts, tool, input_json, output_json, ok, "
            "duration_ms, summary) VALUES (?,?,?,?,?,?,?,?)",
            (
                run_id,
                utcnow(),
                tool,
                json.dumps(redact_obj(tool_input), ensure_ascii=False, default=str)[:20000],
                json.dumps(redact_obj(tool_output), ensure_ascii=False, default=str)[:20000],
                1 if ok else 0,
                int(duration_ms),
                redact(summary)[:500],
            ),
        )

    def list_tool_calls(self, run_id: str) -> list[sqlite3.Row]:
        return self.query(
            "SELECT * FROM tool_calls WHERE run_id = ? ORDER BY id", (run_id,)
        )

    # -- steps (execution evidence) -----------------------------------------
    def add_step(
        self,
        run_id: str,
        *,
        idx: int,
        kind: str,
        purpose: str,
        command: str,
        argv: list[str],
        phase: str,
        workdir: str,
        network: str,
        status: str,
        repair_index: int | None = None,
        step_id: str | None = None,
    ) -> str:
        sid = step_id or f"ev-{idx:02d}"
        self.execute(
            """INSERT INTO steps (id, run_id, idx, kind, purpose, command, argv_json,
                   phase, workdir, status, network, repair_index)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                sid,
                run_id,
                int(idx),
                kind,
                redact(purpose)[:300],
                redact(command)[:1000],
                json.dumps(argv),
                phase,
                workdir,
                status,
                network,
                repair_index,
            ),
        )
        return sid

    def update_step(self, run_id: str, step_id: str, **fields: Any) -> None:
        allowed = {
            "status",
            "exit_code",
            "started_at",
            "finished_at",
            "duration_ms",
            "log_path",
            "log_bytes",
            "truncated",
            "timed_out",
        }
        unknown = set(fields) - allowed
        if unknown:
            raise ValueError(f"cannot update unknown step fields: {sorted(unknown)}")
        if not fields:
            return
        sets = ", ".join(f"{key} = ?" for key in fields)
        params = tuple(fields.values()) + (run_id, step_id)
        self.execute(
            f"UPDATE steps SET {sets} WHERE run_id = ? AND id = ?", params
        )

    def list_steps(self, run_id: str) -> list[sqlite3.Row]:
        return self.query("SELECT * FROM steps WHERE run_id = ? ORDER BY idx", (run_id,))

    def next_step_index(self, run_id: str) -> int:
        row = self.query_one(
            "SELECT COALESCE(MAX(idx), 0) AS m FROM steps WHERE run_id = ?", (run_id,)
        )
        return int(row["m"]) + 1 if row else 1

    # -- sources ------------------------------------------------------------
    def add_source(
        self,
        run_id: str,
        kind: str,
        title: str,
        url: str,
        snippet: str = "",
        retrieved_via: str = "",
    ) -> None:
        existing = self.query_one(
            "SELECT id FROM sources WHERE run_id = ? AND url = ? AND url != ''",
            (run_id, url),
        )
        if existing:
            return
        self.execute(
            "INSERT INTO sources (run_id, ts, kind, title, url, snippet, retrieved_via)"
            " VALUES (?,?,?,?,?,?,?)",
            (
                run_id,
                utcnow(),
                kind,
                redact(title)[:300],
                url[:2048],
                redact(snippet)[:2000],
                retrieved_via[:64],
            ),
        )

    def list_sources(self, run_id: str) -> list[sqlite3.Row]:
        return self.query("SELECT * FROM sources WHERE run_id = ? ORDER BY id", (run_id,))

    # -- patches ------------------------------------------------------------
    def add_patch(
        self,
        run_id: str,
        attempt: int,
        path: str,
        diff: str,
        rationale: str,
        applied: bool,
    ) -> None:
        self.execute(
            "INSERT INTO patches (run_id, ts, attempt, path, diff, rationale, applied)"
            " VALUES (?,?,?,?,?,?,?)",
            (
                run_id,
                utcnow(),
                int(attempt),
                path[:512],
                redact(diff)[:40000],
                redact(rationale)[:2000],
                1 if applied else 0,
            ),
        )

    def list_patches(self, run_id: str) -> list[sqlite3.Row]:
        return self.query("SELECT * FROM patches WHERE run_id = ? ORDER BY id", (run_id,))

    # -- metrics ------------------------------------------------------------
    def add_metric(
        self,
        run_id: str,
        *,
        step_id: str,
        name: str,
        value: float | None,
        raw_value: str,
        unit: str,
        parser: str,
        provenance: str,
        source_file: str = "",
    ) -> None:
        self.execute(
            """INSERT INTO metrics (run_id, step_id, name, value, raw_value, unit,
                   parser, provenance, source_file) VALUES (?,?,?,?,?,?,?,?,?)""",
            (
                run_id,
                step_id,
                name[:120],
                value,
                raw_value[:120],
                unit[:40],
                parser[:60],
                redact(provenance)[:1000],
                source_file[:512],
            ),
        )

    def list_metrics(self, run_id: str) -> list[sqlite3.Row]:
        return self.query("SELECT * FROM metrics WHERE run_id = ? ORDER BY id", (run_id,))


def row_to_dict(row: sqlite3.Row | None) -> dict[str, Any]:
    return dict(row) if row is not None else {}
