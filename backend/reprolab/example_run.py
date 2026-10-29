"""The labelled example report.

A worked illustration of ReproLab's output, seeded once so the UI has
something to show before anything has been run. It is stored with
``is_example = 1``, is never claimed by the worker, is excluded from history
statistics, and every surface that renders it says so.
"""

from __future__ import annotations

import json

from .config import Settings
from .db import Database, STATUS_BLOCKED
from .report import build_markdown_report

EXAMPLE_RUN_ID = "run_example_0001"


def _manifest(settings: Settings) -> dict:
    return {
        "schema_version": "1.0",
        "generated_at": "2026-01-01T00:00:00+00:00",
        "is_example": True,
        "run": {
            "id": EXAMPLE_RUN_ID,
            "created_at": "2026-01-01T00:00:00+00:00",
            "started_at": "2026-01-01T00:00:00+00:00",
            "finished_at": "2026-01-01T00:03:12+00:00",
            "task_type": "small_experiment",
            "goal": "Reproduce the reported BEIR retrieval throughput on Natural Questions.",
            "runtime_budget_seconds": 900,
            "paper_url": "https://arxiv.org/abs/2407.03618",
            "repository_url": "https://github.com/xhluca/bm25s",
            "repository_commit": "0000000000000000000000000000000000000000",
        },
        "status": {
            "value": STATUS_BLOCKED,
            "meaning": (
                "A required resource could not be obtained here, so the "
                "experiment was not attempted or could not continue. This is not "
                "a failure of the code."
            ),
            "detail": (
                "The repository installed and its test suite passed, but the "
                "paper's throughput numbers are measured on the BEIR Natural "
                "Questions corpus, which is not bundled with the repository and "
                "is not downloadable during the network-isolated experiment "
                "phase."
            ),
            "blocked_category": "missing_dataset",
        },
        "outcomes": {
            "repository_inspected": True,
            "environment_installed": True,
            "smoke_test_passed": True,
            "experiment_completed": False,
            "specific_result_reproduced": False,
            "reproduction_note": (
                "Not assessed. Verifying a specific published number requires the "
                "same dataset and split, the same model weights, the same metric "
                "implementation and comparable hardware."
            ),
        },
        "inference": {
            "provider": "Nebius Token Factory",
            "role": "model inference only; it never executes repository code",
            "base_url": settings.nebius_base_url,
            "model": "nvidia/<nemotron-model-resolved-at-run-time>",
        },
        "retrieval": {
            "provider": "Tavily",
            "role": (
                "finding the paper's pages, official documentation, and "
                "troubleshooting material for observed errors"
            ),
            "configured": True,
        },
        "execution_environment": {
            "interpreter": "Python 3.11.x",
            "runner": {
                "backend": "docker",
                "isolation": (
                    "disposable non-root container, cap-drop ALL, "
                    "no-new-privileges, read-only root filesystem"
                ),
                "image": settings.runner_image,
                "image_digest": "sha256:<recorded per run>",
                "credentials_passed_to_container": False,
            },
            "limits": {
                "max_runtime_seconds": 900,
                "step_timeout_seconds": 420,
                "container_cpus": 2.0,
                "container_memory_mb": 4096,
                "container_pids_limit": 256,
                "max_log_bytes": 1048576,
                "max_workspace_mb": 2048,
                "max_repairs": 2,
                "gpu": False,
            },
        },
        "plan": {
            "objective": (
                "Install the repository and run its documented retrieval "
                "evaluation on a small sample of Natural Questions."
            ),
            "blockers": [
                "The BEIR Natural Questions corpus is not included in the "
                "repository and must be downloaded and indexed first.",
                "Experiment-phase containers have no network, by design.",
            ],
            "rejected_commands": [
                {
                    "command": "bash scripts/download_nq.sh",
                    "reason": (
                        "program 'bash' is not allowed; ReproLab executes single "
                        "validated program invocations, never shell scripts"
                    ),
                }
            ],
        },
        "evidence": [
            {
                "evidence_id": "ev-01",
                "kind": "bootstrap",
                "purpose": "ReproLab bootstrap: create an isolated virtualenv",
                "command": "python -m venv /workspace/.venv",
                "argv": ["python", "-m", "venv", "/workspace/.venv"],
                "phase": "setup",
                "network": "bridge (dependency acquisition)",
                "workdir": ".",
                "status": "succeeded",
                "exit_code": 0,
                "duration_ms": 5600,
                "started_at": "2026-01-01T00:00:10+00:00",
                "finished_at": "2026-01-01T00:00:16+00:00",
                "log_bytes": 180,
                "log_truncated": False,
                "timed_out": False,
                "repair_attempt": None,
                "log_file": "logs/ev-01.log",
            },
            {
                "evidence_id": "ev-02",
                "kind": "setup",
                "purpose": "Install the cloned repository itself, per its README",
                "command": "pip install .",
                "argv": ["pip", "install", "."],
                "phase": "setup",
                "network": "bridge (dependency acquisition)",
                "workdir": ".",
                "status": "succeeded",
                "exit_code": 0,
                "duration_ms": 17600,
                "started_at": "2026-01-01T00:00:16+00:00",
                "finished_at": "2026-01-01T00:00:34+00:00",
                "log_bytes": 4200,
                "log_truncated": False,
                "timed_out": False,
                "repair_attempt": None,
                "log_file": "logs/ev-02.log",
            },
            {
                "evidence_id": "ev-03",
                "kind": "experiment",
                "purpose": "Documented smoke test from the repository's test suite",
                "command": "pytest tests/core/test_retrieve.py -q",
                "argv": ["pytest", "tests/core/test_retrieve.py", "-q"],
                "phase": "experiment",
                "network": "none (fully isolated)",
                "workdir": ".",
                "status": "succeeded",
                "exit_code": 0,
                "duration_ms": 1200,
                "started_at": "2026-01-01T00:00:34+00:00",
                "finished_at": "2026-01-01T00:00:36+00:00",
                "log_bytes": 320,
                "log_truncated": False,
                "timed_out": False,
                "repair_attempt": None,
                "log_file": "logs/ev-03.log",
            },
            {
                "evidence_id": "ev-04",
                "kind": "experiment",
                "purpose": "Documented evaluation on Natural Questions",
                "command": "python examples/retrieve_nq.py",
                "argv": ["python", "examples/retrieve_nq.py"],
                "phase": "experiment",
                "network": "none (fully isolated)",
                "workdir": ".",
                "status": "failed",
                "exit_code": 1,
                "duration_ms": 900,
                "started_at": "2026-01-01T00:00:36+00:00",
                "finished_at": "2026-01-01T00:00:37+00:00",
                "log_bytes": 980,
                "log_truncated": False,
                "timed_out": False,
                "repair_attempt": None,
                "log_file": "logs/ev-04.log",
            },
        ],
        "measurements": {
            "verified": [
                {
                    "name": "pytest_passed",
                    "value": 6.0,
                    "raw_value": "6",
                    "unit": "tests",
                    "parser": "pytest_summary",
                    "provenance": "6 passed in 0.11s",
                    "source_file": "ev-03.log",
                }
            ],
            "discarded_claims": [
                {
                    "name": "queries_per_second",
                    "reason": (
                        "quoted line is not present verbatim in the execution log"
                    ),
                    "claimed_line": "Throughput: 1184 QPS on NQ",
                }
            ],
            "note": (
                "Values are parsed by ReproLab from the recorded logs. A value "
                "the model claimed but which could not be located verbatim in a "
                "log is discarded and listed under discarded_claims."
            ),
        },
        "artifacts": [],
        "sources": [
            {
                "kind": "paper",
                "title": (
                    "BM25S: Orders of magnitude faster lexical search via eager "
                    "sparse scoring"
                ),
                "url": "https://arxiv.org/abs/2407.03618",
                "retrieved_via": "paper_fetch:http",
                "snippet": "(abstract text retrieved at run time)",
            },
            {
                "kind": "repository",
                "title": "xhluca/bm25s",
                "url": "https://github.com/xhluca/bm25s",
                "retrieved_via": "repository_clone",
                "snippet": "Shallow clone of the default branch.",
            },
            {
                "kind": "documentation",
                "title": "BM25S project homepage",
                "url": "https://bm25s.github.io",
                "retrieved_via": "tavily_search",
                "snippet": "(documentation excerpt retrieved at run time)",
            },
        ],
        "implementation": {
            "modified": False,
            "patches": [],
            "diff": "",
            "note": (
                "This run used the repository as published, with no code "
                "modifications."
            ),
        },
        "assessment": {
            "headline": (
                "The code installs and its retrieval tests pass, but the paper's "
                "throughput claim could not be tested without the BEIR corpus."
            ),
            "what_was_attempted": (
                "ReproLab cloned the repository, installed it from source in a "
                "disposable container, ran part of its test suite, and then "
                "attempted the documented Natural Questions retrieval example."
            ),
            "what_the_paper_reports": (
                "The paper reports large throughput gains in queries per second "
                "over other Python BM25 implementations, measured on several "
                "BEIR datasets in a single-threaded setting."
            ),
            "what_reprolab_measured": (
                "Six retrieval tests passed (evidence ev-03). No throughput "
                "number was measured, because the evaluation script exited "
                "before producing one."
            ),
            "differences_from_paper": [
                "Dataset: the paper uses full BEIR corpora; this run had no corpus at all.",
                "Hardware: 2 CPU cores in a container, versus the paper's benchmark machine.",
                "Scope: part of the test suite, not the published benchmark harness.",
            ],
            "remains_unverified": [
                "Every quantitative claim in the paper, including the headline "
                "queries-per-second figures.",
                "Any comparison against Elasticsearch or rank-bm25.",
            ],
            "limitations": [
                "Experiment-phase containers have no network, so datasets must "
                "be acquired during the setup phase or not at all.",
                "ReproLab v1 is CPU-only and caps runs at 15 minutes.",
            ],
            "missing_resources": [
                "BEIR Natural Questions corpus (not bundled with the repository).",
                "A pre-built BM25S index for that corpus.",
            ],
        },
        "agent_actions": [
            {
                "tool": "search_research_sources",
                "ok": True,
                "duration_ms": 1800,
                "summary": "Tavily research search returned 5 result(s)",
                "input": {"tool": "search_research_sources", "query": "(example)"},
            },
            {
                "tool": "read_repository_file",
                "ok": True,
                "duration_ms": 4,
                "summary": "read README.md (14000 characters)",
                "input": {"tool": "read_repository_file", "path": "README.md"},
            },
        ],
        "repair_attempts": 0,
    }


def seed_example(db: Database, settings: Settings) -> str:
    """Insert (or refresh) the labelled example run."""
    manifest = _manifest(settings)
    markdown = build_markdown_report(manifest)
    existing = db.get_run(EXAMPLE_RUN_ID)
    if existing is None:
        db.create_run(
            paper_url=manifest["run"]["paper_url"],
            repo_url=manifest["run"]["repository_url"],
            repo_owner="xhluca",
            repo_name="bm25s",
            goal=manifest["run"]["goal"],
            task_type="small_experiment",
            runtime_budget_s=900,
            runner_backend="docker",
            is_example=True,
            status=STATUS_BLOCKED,
            phase="done",
            run_id=EXAMPLE_RUN_ID,
        )
    db.update_run(
        EXAMPLE_RUN_ID,
        status=STATUS_BLOCKED,
        phase="done",
        outcome=STATUS_BLOCKED,
        outcome_detail=manifest["status"]["detail"],
        repo_commit=manifest["run"]["repository_commit"],
        report_md=markdown,
        manifest_json=json.dumps(manifest, ensure_ascii=False),
        model_used=manifest["inference"]["model"],
        finished_at=manifest["run"]["finished_at"],
    )
    return EXAMPLE_RUN_ID
