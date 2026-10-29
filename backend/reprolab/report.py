"""Report and manifest generation.

Two artifacts come out of every run:

* a Markdown report for a human, and
* a JSON run manifest -- the machine-readable record of what was executed.

Both are built from recorded evidence. Every claimed measurement carries an
evidence id (``ev-NN``) that points at a real step with a real exit code and a
real log file. The report states what the paper reports, what ReproLab
measured, how the two differ, and what remains unverified -- as separate
sections, never merged.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from .config import Settings
from .security.redact import redact

REPORT_SCHEMA_VERSION = "1.0"

_STATUS_MEANING = {
    "Completed": "Every planned command ran and exited successfully.",
    "Failed": "A command failed for a reason ReproLab could not repair within its bounds.",
    "Blocked": "A required resource could not be obtained here, so the experiment was not attempted or could not continue. This is not a failure of the code.",
    "Cancelled": "A user stopped the run; execution was terminated.",
    "Not attempted": "The run did not reach execution.",
}


def build_manifest(
    *,
    run: dict[str, Any],
    status: str,
    outcome_detail: str,
    outcomes: dict[str, object],
    steps: list[dict[str, Any]],
    sources: list[dict[str, Any]],
    patches: list[dict[str, Any]],
    metrics: list[dict[str, Any]],
    rejected_metrics: list[dict[str, Any]],
    artifacts: list[dict[str, object]],
    tool_calls: list[dict[str, Any]],
    assessment: dict[str, Any],
    settings: Settings,
    runner_info: dict[str, object],
    modified_implementation: bool,
    diff: str,
    blocked_category: str = "",
) -> dict[str, Any]:
    """Assemble the JSON run manifest."""
    environment = {}
    if run.get("environment_json"):
        try:
            environment = json.loads(run["environment_json"])
        except json.JSONDecodeError:
            environment = {}
    plan = {}
    if run.get("plan_json"):
        try:
            plan = json.loads(run["plan_json"])
        except json.JSONDecodeError:
            plan = {}

    evidence = [
        {
            "evidence_id": step["id"],
            "kind": step["kind"],
            "purpose": step["purpose"],
            "command": step["command"],
            "argv": json.loads(step["argv_json"]) if step.get("argv_json") else [],
            "phase": step["phase"],
            "network": step["network"],
            "workdir": step["workdir"],
            "status": step["status"],
            "exit_code": step["exit_code"],
            "duration_ms": step["duration_ms"],
            "started_at": step["started_at"],
            "finished_at": step["finished_at"],
            "log_bytes": step["log_bytes"],
            "log_truncated": bool(step["truncated"]),
            "timed_out": bool(step["timed_out"]),
            "repair_attempt": step["repair_index"],
            "log_file": f"logs/{step['id']}.log",
        }
        for step in steps
    ]

    return {
        "schema_version": REPORT_SCHEMA_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "is_example": bool(run.get("is_example")),
        "run": {
            "id": run.get("id"),
            "created_at": run.get("created_at"),
            "started_at": run.get("started_at"),
            "finished_at": run.get("finished_at"),
            "task_type": run.get("task_type"),
            "goal": run.get("goal"),
            "runtime_budget_seconds": run.get("runtime_budget_s"),
            "paper_url": run.get("paper_url"),
            "repository_url": run.get("repo_url"),
            "repository_commit": run.get("repo_commit") or "unknown",
        },
        "status": {
            "value": status,
            "meaning": _STATUS_MEANING.get(status, ""),
            "detail": outcome_detail,
            "blocked_category": blocked_category,
        },
        "outcomes": outcomes,
        "inference": {
            "provider": "Nebius Token Factory",
            "role": "model inference only; it never executes repository code",
            "base_url": settings.nebius_base_url,
            "model": run.get("model_used") or "",
        },
        "retrieval": {
            "provider": "Tavily",
            "role": "finding the paper's pages, official documentation, and "
            "troubleshooting material for observed errors",
            "configured": settings.has_tavily,
        },
        "execution_environment": {
            "interpreter": environment.get("interpreter", "unknown"),
            "runner": runner_info or environment.get("runner", {}),
            "limits": {
                "max_runtime_seconds": settings.limits.max_runtime_seconds,
                "step_timeout_seconds": settings.limits.step_timeout_seconds,
                "container_cpus": settings.limits.container_cpus,
                "container_memory_mb": settings.limits.container_memory_mb,
                "container_pids_limit": settings.limits.container_pids_limit,
                "max_log_bytes": settings.limits.max_log_bytes,
                "max_workspace_mb": settings.limits.max_workspace_mb,
                "max_repairs": settings.limits.max_repairs,
                "gpu": False,
            },
        },
        "plan": plan,
        "evidence": evidence,
        "measurements": {
            "verified": metrics,
            "discarded_claims": rejected_metrics,
            "note": (
                "Values are parsed by ReproLab from the recorded logs. A value "
                "the model claimed but which could not be located verbatim in a "
                "log is discarded and listed under discarded_claims."
            ),
        },
        "artifacts": artifacts,
        "sources": [
            {
                "kind": s["kind"],
                "title": s["title"],
                "url": s["url"],
                "retrieved_via": s["retrieved_via"],
                "snippet": s["snippet"][:400],
            }
            for s in sources
        ],
        "implementation": {
            "modified": modified_implementation,
            "patches": [
                {
                    "attempt": p["attempt"],
                    "path": p["path"],
                    "rationale": p["rationale"],
                    "applied": bool(p["applied"]),
                    "diff": p["diff"],
                }
                for p in patches
            ],
            "diff": diff,
            "note": (
                "This run used a MODIFIED implementation: the diff above was "
                "applied inside the disposable workspace."
                if modified_implementation
                else "This run used the repository as published, with no code "
                "modifications."
            ),
        },
        "assessment": assessment,
        "agent_actions": [
            {
                "tool": t["tool"],
                "ok": bool(t["ok"]),
                "duration_ms": t["duration_ms"],
                "summary": t["summary"],
                "input": _safe_json(t["input_json"]),
            }
            for t in tool_calls
        ],
        "repair_attempts": run.get("repair_attempts") or 0,
    }


def _safe_json(text: str | None) -> Any:
    if not text:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return text[:500]


def _bullets(items: list[Any], empty: str) -> str:
    cleaned = [str(i).strip() for i in (items or []) if str(i).strip()]
    if not cleaned:
        return f"_{empty}_\n"
    return "\n".join(f"- {item}" for item in cleaned) + "\n"


def _checkmark(value: object) -> str:
    return "yes" if value else "no"


def build_markdown_report(manifest: dict[str, Any]) -> str:
    """Render the human-readable report."""
    run = manifest["run"]
    status = manifest["status"]
    outcomes = manifest["outcomes"]
    assessment = manifest.get("assessment") or {}
    lines: list[str] = []
    add = lines.append

    if manifest.get("is_example"):
        add(
            "> **EXAMPLE REPORT — NOT A REAL RUN.** This document is a worked "
            "illustration of ReproLab's output format. It is not evidence of a "
            "reproduction and is excluded from run history statistics.\n"
        )

    add(f"# Reproducibility report: {run['repository_url'].split('/')[-1]}\n")
    add(f"**Overall outcome: {status['value']}**\n")
    if status.get("meaning"):
        add(f"_{status['meaning']}_\n")
    if status.get("detail"):
        add(f"{status['detail']}\n")
    if assessment.get("headline"):
        add(f"\n{assessment['headline']}\n")

    add("\n## What was attempted\n")
    add(
        assessment.get("what_was_attempted")
        or "No narrative was generated for this run.\n"
    )
    add(f"\n- Task type: `{run['task_type']}`")
    add(f"\n- Goal: {run.get('goal') or '_none given_'}")
    add(f"\n- Paper: {run['paper_url']}")
    add(f"\n- Repository: {run['repository_url']}")
    add(f"\n- Repository commit: `{run['repository_commit']}`")
    add(f"\n- Runtime budget: {run['runtime_budget_seconds']} s\n")

    add("\n## Outcome ladder\n")
    add(
        "These are deliberately separate. A step being reached never implies "
        "the one below it.\n\n"
    )
    add("| Outcome | Reached |\n|---|---|\n")
    add(f"| Repository inspected | {_checkmark(outcomes.get('repository_inspected'))} |\n")
    add(f"| Environment installed | {_checkmark(outcomes.get('environment_installed'))} |\n")
    add(f"| Smoke test passed | {_checkmark(outcomes.get('smoke_test_passed'))} |\n")
    add(f"| Small experiment completed | {_checkmark(outcomes.get('experiment_completed'))} |\n")
    add(
        f"| **A specific reported result reproduced under matching conditions** "
        f"| **{_checkmark(outcomes.get('specific_result_reproduced'))}** |\n"
    )
    if outcomes.get("reproduction_note"):
        add(f"\n{outcomes['reproduction_note']}\n")

    env = manifest["execution_environment"]
    runner = env.get("runner") or {}
    add("\n## Environment\n")
    add(f"- Interpreter: `{env.get('interpreter', 'unknown')}`\n")
    add(f"- Runner backend: `{runner.get('backend', 'unknown')}`\n")
    if runner.get("image"):
        add(f"- Container image: `{runner['image']}`\n")
    if runner.get("image_digest"):
        add(f"- Image digest: `{runner['image_digest']}`\n")
    add(f"- Isolation: {runner.get('isolation', 'n/a')}\n")
    limits = env["limits"]
    add(
        f"- Limits: {limits['container_cpus']} CPU, "
        f"{limits['container_memory_mb']} MB memory, no GPU, "
        f"{limits['step_timeout_seconds']} s per command, "
        f"{limits['max_repairs']} repair attempts\n"
    )
    add("- Application credentials passed to experiment containers: none\n")

    add("\n## Exact commands and exit codes\n")
    evidence = manifest.get("evidence") or []
    if not evidence:
        add("_No commands were executed._\n")
    else:
        add("| Evidence | Phase | Network | Command | Exit | Status | Duration |\n")
        add("|---|---|---|---|---|---|---|\n")
        for item in evidence:
            exit_code = "—" if item["exit_code"] is None else item["exit_code"]
            duration = f"{(item['duration_ms'] or 0) / 1000:.1f} s"
            add(
                f"| `{item['evidence_id']}` | {item['phase']} | "
                f"{item['network']} | `{item['command']}` | {exit_code} | "
                f"{item['status']} | {duration} |\n"
            )
        truncated = [i["evidence_id"] for i in evidence if i["log_truncated"]]
        if truncated:
            add(
                f"\n_Log output was truncated at ReproLab's size limit for: "
                f"{', '.join(truncated)}._\n"
            )

    add("\n## Measured outputs and their evidence\n")
    measurements = manifest["measurements"]
    verified = measurements.get("verified") or []
    if not verified:
        add(
            "**No reliable metric was extracted from this run's output.** "
            "ReproLab only reports numbers it can locate verbatim in a recorded "
            "log or output file; it does not infer metrics from narrative text.\n"
        )
    else:
        add("| Metric | Value | Unit | Evidence | Parsed from | Parser |\n")
        add("|---|---|---|---|---|---|\n")
        for metric in verified:
            add(
                f"| {metric['name']} | {metric['raw_value']} | "
                f"{metric['unit'] or '—'} | `{metric.get('source_file', '')}` | "
                f"`{str(metric['provenance'])[:90]}` | {metric['parser']} |\n"
            )
    discarded = measurements.get("discarded_claims") or []
    if discarded:
        add("\n**Discarded measurement claims** (could not be verified against the logs):\n\n")
        for item in discarded:
            add(f"- `{item.get('name')}`: {item.get('reason')}\n")

    add("\n## What the paper reports\n")
    add(
        assessment.get("what_the_paper_reports")
        or "_Not summarised for this run._\n"
    )
    add("\n## What ReproLab actually measured\n")
    add(
        assessment.get("what_reprolab_measured")
        or "_No measurement was obtained._\n"
    )
    add("\n## Differences from the paper's conditions\n")
    add(
        _bullets(
            assessment.get("differences_from_paper"),
            "No differences were recorded. Treat this as incomplete rather than "
            "as agreement with the paper.",
        )
    )
    add("\n## What remains unverified\n")
    add(
        _bullets(
            assessment.get("remains_unverified"),
            "Not stated. Assume the paper's quantitative claims are unverified.",
        )
    )

    add("\n## Missing resources and limitations\n")
    add("**Missing resources**\n\n")
    add(_bullets(assessment.get("missing_resources"), "None recorded."))
    add("\n**Limitations**\n\n")
    add(_bullets(assessment.get("limitations"), "None recorded."))
    plan = manifest.get("plan") or {}
    if plan.get("blockers"):
        add("\n**Blockers identified during planning**\n\n")
        add(_bullets(plan["blockers"], "None."))
    if plan.get("rejected_commands"):
        add("\n**Commands rejected by ReproLab's validator**\n\n")
        for item in plan["rejected_commands"]:
            add(f"- `{item.get('command')}` — {item.get('reason')}\n")

    implementation = manifest["implementation"]
    add("\n## Original versus modified implementation\n")
    add(f"{implementation['note']}\n")
    if implementation.get("patches"):
        for patch in implementation["patches"]:
            add(
                f"\n**Repair attempt {patch['attempt']} — `{patch['path']}`**\n\n"
                f"{patch['rationale']}\n\n"
            )
    if implementation.get("diff"):
        add("\n```diff\n")
        add(implementation["diff"][:20000])
        add("\n```\n")

    add("\n## Sources consulted\n")
    sources = manifest.get("sources") or []
    if not sources:
        add("_No external sources were recorded._\n")
    else:
        for source in sources:
            label = source["title"] or source["url"] or "(untitled)"
            via = f" — retrieved via `{source['retrieved_via']}`" if source["retrieved_via"] else ""
            if source["url"]:
                add(f"- [{label}]({source['url']}) ({source['kind']}){via}\n")
            else:
                add(f"- {label} ({source['kind']}){via}\n")

    artifacts = [a for a in (manifest.get("artifacts") or []) if a.get("collected")]
    add("\n## Collected artifacts\n")
    if artifacts:
        for artifact in artifacts:
            add(f"- `{artifact['name']}` ({artifact['bytes']} bytes)\n")
    else:
        add("_No output artifacts were produced in the run's `outputs/` directory._\n")

    inference = manifest["inference"]
    retrieval = manifest["retrieval"]
    add("\n## How this report was produced\n")
    add(
        f"- Planning, diagnosis and this narrative: `{inference['model'] or 'unavailable'}` "
        f"served by {inference['provider']} ({inference['role']}).\n"
    )
    add(f"- Retrieval: {retrieval['provider']} — {retrieval['role']}.\n")
    add(
        "- Execution: a separate, disposable container. Statuses, exit codes, "
        "evidence ids and metric values in this report are computed by ReproLab "
        "from recorded execution, not supplied by the model.\n"
    )
    add(f"- Run id: `{run['id']}`\n")
    add(f"- Report generated: {manifest['generated_at']}\n")

    return redact("".join(lines))
