"""The bounded agent loop.

Two phases, separated by a human approval gate:

  research_and_plan()  inspect the goal and URLs, retrieve the paper, search
                       with Tavily, read repository files, then produce a
                       validated :class:`ExperimentPlan`.
                       --> stops at `awaiting_approval`

  execute()            run the approved commands in the isolated runner,
                       inspect real exit codes and logs, diagnose failures,
                       attempt at most ``max_repairs`` bounded repairs, collect
                       artifacts and write the report.

Bounds are enforced here, in application code: number of research tool calls,
number of plan commands, per-step timeout, total runtime, repair count. The
model is informed of them; it is not trusted to honour them.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from ..config import Settings
from ..curated import RepoNotPermitted, assert_executable, hints_for
from ..db import (
    Database,
    PHASE_AWAITING_APPROVAL,
    PHASE_DONE,
    PHASE_REPAIRING,
    PHASE_REPORTING,
    PHASE_RUNNING,
    STATUS_BLOCKED,
    STATUS_CANCELLED,
    STATUS_COMPLETED,
    STATUS_FAILED,
    STATUS_NOT_ATTEMPTED,
    utcnow,
)
from ..providers.nebius import NebiusError, StructuredResponseError
from ..providers.paper import fetch_paper
from ..report import build_manifest, build_markdown_report
from ..runner.commands import (
    ALLOWED_PROGRAMS,
    CommandNotAllowed,
    CommandPhase,
    ValidatedCommand,
    internal_command,
    parse_command,
)
from ..runner.docker_runner import RunnerUnavailable, StepResult
from ..runner.workspace import Workspace, WorkspaceError
from ..security.paths import PathNotAllowed, validate_patch_target
from ..security.redact import redact
from .metrics import VerifiedMetric, dedupe, deterministic_metrics, verify_candidates
from .prompts import (
    METRIC_SYSTEM_PROMPT,
    REPORT_SYSTEM_PROMPT,
    diagnosis_system_prompt,
    plan_system_prompt,
    research_system_prompt,
)
from .schemas import (
    ExperimentPlan,
    FailureDiagnosis,
    MetricExtraction,
    PlannedCommand,
    ReportAssessment,
    ToolAction,
)
from .tools import ToolBox

MAX_LOG_EXCERPT = 6000


class RunCancelled(RuntimeError):
    """Raised internally when the user cancels a run."""


@dataclass
class Outcomes:
    """The outcome ladder, kept deliberately separate.

    A successful install or a small inference run never implies the step above
    it. Only ``specific_result_reproduced`` speaks to the paper's claim, and
    ReproLab sets it true only under matching conditions -- which the v1 runner
    cannot establish, so it carries an explanation instead.
    """

    repository_inspected: bool = False
    environment_installed: bool = False
    smoke_test_passed: bool = False
    experiment_completed: bool = False
    specific_result_reproduced: bool = False
    reproduction_note: str = (
        "Not assessed. Verifying a specific published number requires the same "
        "dataset and split, the same model weights, the same metric "
        "implementation and comparable hardware. ReproLab v1 runs small CPU-only "
        "checks and does not establish those conditions."
    )

    def as_dict(self) -> dict[str, object]:
        return {
            "repository_inspected": self.repository_inspected,
            "environment_installed": self.environment_installed,
            "smoke_test_passed": self.smoke_test_passed,
            "experiment_completed": self.experiment_completed,
            "specific_result_reproduced": self.specific_result_reproduced,
            "reproduction_note": self.reproduction_note,
        }


class AgentRun:
    """Drives one run through research, approval, execution and reporting."""

    def __init__(
        self,
        *,
        db: Database,
        settings: Settings,
        run_row: Any,
        nebius: Any | None,
        tavily: Any | None,
        runner: Any | None,
        log_sink: Callable[[str, str], None] | None = None,
    ) -> None:
        self.db = db
        self.settings = settings
        self.limits = settings.limits
        self.run = dict(run_row)
        self.run_id = self.run["id"]
        self.nebius = nebius
        self.tavily = tavily
        self.runner = runner
        self.log_sink = log_sink
        self.workspace = Workspace(settings.workspaces_dir / self.run_id)
        self.logs_dir = settings.artifacts_dir / self.run_id / "logs"
        self.outcomes = Outcomes()
        self.started_monotonic = time.monotonic()
        self._allowed_programs = ", ".join(sorted(ALLOWED_PROGRAMS))

    # -- helpers ------------------------------------------------------------
    def event(self, kind: str, title: str, detail: str = "", data: Any = None) -> None:
        self.db.add_event(self.run_id, kind, title, detail, data)

    def _check_cancel(self) -> None:
        if self.db.cancel_requested(self.run_id):
            raise RunCancelled("cancelled by the user")

    def _cancelled(self) -> bool:
        return self.db.cancel_requested(self.run_id)

    def _elapsed(self) -> float:
        return time.monotonic() - self.started_monotonic

    def _remaining_budget(self) -> int:
        budget = min(int(self.run["runtime_budget_s"]), self.limits.max_runtime_seconds)
        return max(0, int(budget - self._elapsed()))

    def _limits_text(self) -> str:
        budget = min(int(self.run["runtime_budget_s"]), self.limits.max_runtime_seconds)
        return (
            f"- total runtime budget for this run: {budget} seconds\n"
            f"- per-command timeout: {self.limits.step_timeout_seconds} seconds\n"
            f"- CPU: {self.limits.container_cpus} cores, no GPU\n"
            f"- memory: {self.limits.container_memory_mb} MB\n"
            f"- maximum captured log output per command: "
            f"{self.limits.max_log_bytes // 1024} KB\n"
            f"- maximum workspace size: {self.limits.max_workspace_mb} MB\n"
            f"- at most {self.limits.max_plan_commands} setup commands and "
            f"{self.limits.max_plan_commands} experiment commands\n"
            f"- at most {self.limits.max_repairs} repair attempts after a failure"
        )

    # =======================================================================
    # Phase 1: research and planning
    # =======================================================================
    def research_and_plan(self) -> None:
        try:
            self._research_and_plan()
        except RunCancelled:
            self._finish(STATUS_CANCELLED, "Cancelled before execution began.")
        except RunnerUnavailable as exc:
            self._finish(
                STATUS_BLOCKED,
                f"No isolated runner is available, so no repository code was "
                f"executed. {exc}",
            )
        except RepoNotPermitted as exc:
            self._finish(STATUS_BLOCKED, str(exc))
        except (WorkspaceError, NebiusError) as exc:
            self._finish(STATUS_FAILED, f"{type(exc).__name__}: {exc}")
        except Exception as exc:  # noqa: BLE001
            self.event("error", "Unexpected error during planning", str(exc)[:2000])
            self._finish(STATUS_FAILED, f"Unexpected error during planning: {exc}")

    def _research_and_plan(self) -> None:
        self.event(
            "phase",
            "Analysis started",
            f"Task: {self.run['task_type'].replace('_', ' ')}. "
            f"Runtime budget {self.run['runtime_budget_s']}s.",
        )

        # Execution policy is checked before anything is downloaded, so a
        # non-curated repository is reported honestly rather than half-run.
        assert_executable(
            self.run["repo_url"], allow_arbitrary=self.settings.allow_arbitrary_repos
        )
        if self.runner is not None:
            self.runner.preflight()
            self.event("runner", "Runner available", "", self.runner.runtime_info())

        self._check_cancel()

        # --- clone -----------------------------------------------------------
        self.event("clone", "Cloning repository", self.run["repo_url"])
        self.workspace = Workspace.create(self.settings.workspaces_dir, self.run_id)
        clone = self.workspace.clone(
            self.run["repo_url"],
            max_bytes=self.limits.max_workspace_mb * 1024 * 1024,
            timeout=min(300, max(60, self._remaining_budget())),
        )
        self.db.update_run(self.run_id, repo_commit=clone.commit)
        self.run["repo_commit"] = clone.commit
        self.outcomes.repository_inspected = True
        self.event(
            "clone",
            "Repository cloned",
            f"commit {clone.commit[:12]} on {clone.ref}, "
            f"{clone.size_bytes // 1024} KB",
            {"commit": clone.commit, "ref": clone.ref, "size_bytes": clone.size_bytes},
        )
        self.db.add_source(
            self.run_id,
            "repository",
            f"{self.run['repo_owner']}/{self.run['repo_name']} @ {clone.commit[:12]}",
            self.run["repo_url"],
            f"Shallow clone of {clone.ref} at commit {clone.commit}.",
            "repository_clone",
        )
        self._check_cancel()

        # --- paper -----------------------------------------------------------
        paper = fetch_paper(
            self.run["paper_url"],
            max_bytes=self.limits.max_download_bytes,
            tavily=self.tavily,
            goal=self.run["goal"],
        )
        if paper.ok:
            self.db.add_source(
                self.run_id,
                "paper",
                paper.title or self.run["paper_url"],
                paper.final_url,
                paper.text[:1500],
                f"paper_fetch:{paper.source}",
            )
            self.event(
                "source",
                "Paper retrieved",
                f"{len(paper.text)} characters via {paper.source}",
            )
        else:
            self.event(
                "source",
                "Paper could not be retrieved",
                paper.error or "no readable text was returned",
            )

        # --- toolbox ---------------------------------------------------------
        toolbox = ToolBox(
            db=self.db,
            run_id=self.run_id,
            workspace=self.workspace,
            tavily=self.tavily,
            max_download_bytes=self.limits.max_download_bytes,
            on_source=lambda kind, title, url, snippet, via: self.event(
                "source", f"Source added: {title[:90] or url[:90]}", url, {"via": via}
            ),
        )
        toolbox.note_url(self.run["paper_url"])

        hints = hints_for(self.run["repo_url"], self.run["paper_url"])

        # --- seed retrieval with Tavily --------------------------------------
        if self.tavily is not None:
            seed = toolbox.call(
                "search_research_sources",
                query=(
                    f"{self.run['repo_owner']}/{self.run['repo_name']} official "
                    f"documentation installation and evaluation instructions; "
                    f"paper {self.run['paper_url']}; "
                    f"{self.run['goal'] or 'reproduce the published experiment'}"
                ),
            )
            self.event(
                "tool",
                "Tavily research search",
                seed.summary,
                {"ok": seed.ok, "remaining": toolbox.remaining},
            )
        else:
            self.event(
                "tool",
                "Tavily search skipped",
                "TAVILY_API_KEY is not configured, so no external documentation "
                "was retrieved for this run.",
            )

        self._check_cancel()

        if self.nebius is None:
            raise NebiusError(
                "NEBIUS_API_KEY is not configured, so no model is available to "
                "plan the experiment."
            )

        # --- bounded research loop -------------------------------------------
        transcript = self._initial_transcript(paper, hints, toolbox)
        system = research_system_prompt(
            limits_text=self._limits_text(), allowed_programs=self._allowed_programs
        )
        max_calls = self.limits.max_research_tool_calls
        for step in range(1, max_calls + 1):
            self._check_cancel()
            if self._remaining_budget() <= 30:
                self.event(
                    "budget",
                    "Research stopped early",
                    "The runtime budget for this run is nearly exhausted.",
                )
                break
            try:
                action = self.nebius.structured(
                    ToolAction,
                    [{"role": "system", "content": system}, *transcript],
                    max_tokens=700,
                    max_attempts=self.limits.max_structured_retries + 1,
                )
            except StructuredResponseError as exc:
                self.event(
                    "model",
                    "Model response rejected",
                    f"The model did not return a valid tool action: {exc}",
                )
                break
            if action.tool == "finish_research":
                self.event(
                    "agent",
                    "Research complete",
                    action.thought_summary or "The agent has enough evidence to plan.",
                )
                break
            summary = action.thought_summary or f"calling {action.tool}"
            self.event(
                "agent",
                f"Step {step}/{max_calls}: {action.tool}",
                summary,
                {
                    "tool": action.tool,
                    "query": action.query[:200],
                    "path": action.path[:200],
                    "url": action.url[:300],
                },
            )
            result = toolbox.call(
                action.tool, query=action.query, url=action.url, path=action.path
            )
            self.event(
                "tool",
                result.summary[:200],
                "",
                {"ok": result.ok, "remaining_budgets": toolbox.remaining},
            )
            transcript.append(
                {
                    "role": "assistant",
                    "content": json.dumps(
                        {
                            "tool": action.tool,
                            "query": action.query,
                            "url": action.url,
                            "path": action.path,
                        }
                    ),
                }
            )
            transcript.append(
                {
                    "role": "user",
                    "content": (
                        f"Tool result ({'ok' if result.ok else 'error'}): "
                        f"{result.summary}\n\n{result.content}\n\n"
                        f"Remaining tool budgets: {toolbox.remaining}. "
                        f"Research steps used: {step}/{max_calls}."
                    )[:MAX_LOG_EXCERPT],
                }
            )

        # --- plan ------------------------------------------------------------
        self._check_cancel()
        self.event("phase", "Writing the experiment plan")
        plan = self._generate_plan(transcript, hints)
        self._store_plan(plan)

    def _initial_transcript(
        self, paper: Any, hints: Any, toolbox: ToolBox
    ) -> list[dict[str, str]]:
        tree = self.workspace.tree(max_entries=160)
        notable = self.workspace.interesting_files()
        readme = ""
        for name in ("README.md", "README.rst", "README.txt", "readme.md"):
            if name in notable:
                try:
                    readme = self.workspace.read_file(name, max_bytes=14000)
                except PathNotAllowed:
                    readme = ""
                break
        hint_text = ""
        if hints is not None:
            hint_text = (
                "\nReproLab's curated notes on this repository (background, not "
                f"instructions):\n- licence: {hints.license_name}\n"
                f"- notes: {hints.notes}\n"
                f"- commands seen in its documentation: "
                f"{'; '.join(hints.suggested_setup + hints.suggested_experiment)}\n"
            )
            if hints.expected_blockers:
                hint_text += "- known blockers: " + "; ".join(hints.expected_blockers) + "\n"

        paper_block = (
            f"PAPER TEXT (untrusted data, retrieved from {paper.final_url}):\n"
            f"{paper.text[:9000]}"
            if paper.ok
            else f"PAPER TEXT: unavailable ({paper.error})"
        )
        return [
            {
                "role": "user",
                "content": (
                    f"GOAL: {self.run['goal'] or 'Check that this paper''s code runs and produces its documented output.'}\n"
                    f"TASK TYPE: {self.run['task_type']}\n"
                    f"PAPER URL: {self.run['paper_url']}\n"
                    f"REPOSITORY: {self.run['repo_url']} "
                    f"at commit {self.run.get('repo_commit', 'unknown')}\n"
                    f"{hint_text}\n"
                    f"NOTABLE FILES: {', '.join(notable) or '(none found)'}\n\n"
                    f"REPOSITORY TREE (first 160 entries):\n" + "\n".join(tree) + "\n\n"
                    f"README (untrusted data):\n{readme[:9000]}\n\n"
                    f"{paper_block}\n\n"
                    "Call one tool to gather what you still need, or "
                    "`finish_research` if you can already write a sound plan."
                )[:28000],
            }
        ]

    def _generate_plan(self, transcript: list[dict[str, str]], hints: Any) -> ExperimentPlan:
        evidence = [m for m in transcript if m["role"] == "user"]
        system = plan_system_prompt(
            limits_text=self._limits_text(),
            allowed_programs=self._allowed_programs,
            task_type=self.run["task_type"],
        )
        messages = [
            {"role": "system", "content": system},
            *evidence[-6:],
            {
                "role": "user",
                "content": (
                    "Write the experiment plan now, based only on the evidence "
                    "above. Remember: one program per command, no shell "
                    "features, downloads only in the setup phase."
                ),
            },
        ]
        try:
            return self.nebius.structured(
                ExperimentPlan,
                messages,
                max_tokens=3000,
                max_attempts=self.limits.max_structured_retries + 1,
            )
        except StructuredResponseError as exc:
            self.event(
                "model",
                "Plan rejected by validation",
                f"The model could not produce a schema-valid plan: {exc}",
            )
            raise

    # -- plan validation ---------------------------------------------------
    def _validate_commands(
        self, proposed: list[PlannedCommand], phase: CommandPhase
    ) -> tuple[list[ValidatedCommand], list[dict[str, str]]]:
        """Re-validate every model-proposed command. Rejections are recorded."""
        accepted: list[ValidatedCommand] = []
        rejected: list[dict[str, str]] = []
        for item in proposed[: self.limits.max_plan_commands]:
            declared = (
                CommandPhase.SETUP if item.phase == "setup" else CommandPhase.EXPERIMENT
            )
            # The phase the plan section implies wins over the field, so a
            # download cannot be smuggled into the networkless phase.
            effective = phase if phase is CommandPhase.EXPERIMENT else declared
            try:
                accepted.append(
                    parse_command(
                        item.command,
                        phase=effective,
                        purpose=item.purpose,
                        workdir=item.workdir,
                    )
                )
            except CommandNotAllowed as exc:
                rejected.append({"command": item.command[:300], "reason": str(exc)})
        return accepted, rejected

    def _store_plan(self, plan: ExperimentPlan) -> None:
        setup, setup_rejected = self._validate_commands(
            plan.setup_commands, CommandPhase.SETUP
        )
        experiment, experiment_rejected = self._validate_commands(
            plan.experiment_commands, CommandPhase.EXPERIMENT
        )
        rejected = setup_rejected + experiment_rejected
        for item in rejected:
            self.event(
                "validation",
                "Proposed command rejected",
                f"{item['command']} -- {item['reason']}",
            )

        budget = min(int(self.run["runtime_budget_s"]), self.limits.max_runtime_seconds)
        over_budget = plan.estimated_runtime_seconds > budget

        blockers = list(plan.blockers)
        if over_budget:
            blockers.append(
                f"The plan estimates {plan.estimated_runtime_seconds}s, which "
                f"exceeds this run's {budget}s budget."
            )
        if rejected:
            blockers.append(
                f"{len(rejected)} proposed command(s) were rejected by ReproLab's "
                "command validator and will not be executed."
            )

        is_blocked = bool(plan.is_blocked or over_budget or not experiment and not setup)
        blocked_reason = plan.blocked_reason
        if not blocked_reason and over_budget:
            blocked_reason = (
                "The planned experiment does not fit inside the runtime budget."
            )
        if not blocked_reason and not setup and not experiment:
            blocked_reason = (
                "No command in the plan passed ReproLab's command validator, so "
                "there is nothing safe to execute."
            )

        payload = {
            "objective": plan.objective,
            "paper_claim_summary": plan.paper_claim_summary,
            "what_this_run_can_show": plan.what_this_run_can_show,
            "required_resources": [r.model_dump() for r in plan.required_resources],
            "setup_commands": [c.as_dict() for c in setup],
            "experiment_commands": [c.as_dict() for c in experiment],
            "rejected_commands": rejected,
            "expected_outputs": plan.expected_outputs,
            "blockers": blockers,
            "estimated_runtime_seconds": plan.estimated_runtime_seconds,
            "runtime_budget_seconds": budget,
            "is_blocked": is_blocked,
            "blocked_reason": blocked_reason,
            "confidence": plan.confidence,
            "limits": {
                "step_timeout_seconds": self.limits.step_timeout_seconds,
                "container_cpus": self.limits.container_cpus,
                "container_memory_mb": self.limits.container_memory_mb,
                "max_log_bytes": self.limits.max_log_bytes,
                "max_repairs": self.limits.max_repairs,
                "gpu": False,
            },
            "model": self.nebius.model if self.nebius else "",
        }
        self.db.update_run(
            self.run_id,
            plan_json=json.dumps(payload, ensure_ascii=False),
            model_used=self.nebius.model if self.nebius else "",
        )
        self.event(
            "plan",
            "Experiment plan ready",
            f"{len(setup)} setup command(s), {len(experiment)} experiment "
            f"command(s), {len(blockers)} blocker(s).",
            {"is_blocked": is_blocked},
        )

        if is_blocked:
            self.event(
                "phase",
                "Reported as blocked without executing code",
                blocked_reason,
            )
            self._report_and_finish(
                STATUS_BLOCKED,
                blocked_reason
                or "The experiment cannot proceed with the resources available.",
            )
            return

        self.db.update_run(
            self.run_id,
            phase=PHASE_AWAITING_APPROVAL,
            status="Awaiting approval",
        )
        self.event(
            "approval",
            "Waiting for approval",
            "No downloaded code has been executed yet. Review the commands and "
            "limits, then approve to run them in the isolated runner.",
        )

    # =======================================================================
    # Phase 2: execution
    # =======================================================================
    def execute(self) -> None:
        try:
            self._execute()
        except RunCancelled:
            self.event("cancel", "Run cancelled", "Execution was terminated.")
            self._report_and_finish(STATUS_CANCELLED, "Cancelled by the user.")
        except RunnerUnavailable as exc:
            self._report_and_finish(
                STATUS_BLOCKED,
                f"No isolated runner is available, so no repository code was "
                f"executed. {exc}",
            )
        except Exception as exc:  # noqa: BLE001
            self.event("error", "Unexpected error during execution", str(exc)[:2000])
            self._report_and_finish(STATUS_FAILED, f"Unexpected error: {exc}")

    def _execute(self) -> None:
        plan = json.loads(self.run.get("plan_json") or "{}")
        self.started_monotonic = time.monotonic()
        self.db.update_run(self.run_id, phase=PHASE_RUNNING, status="Active")
        self.event(
            "phase",
            "Execution approved -- starting isolated runner",
            "Repository code runs in a disposable container: non-root, no "
            "application credentials, no network during the experiment phase.",
        )
        if self.runner is None:
            raise RunnerUnavailable("no runner was constructed for this run")
        self.runner.preflight()
        self.runner.ensure_image()
        self.logs_dir.mkdir(parents=True, exist_ok=True)

        # --- bootstrap (ReproLab's own commands, not the model's) -----------
        bootstrap = internal_command(
            ["python", "-m", "venv", "/workspace/.venv"],
            phase=CommandPhase.SETUP,
            purpose="ReproLab bootstrap: create an isolated virtualenv for this run",
        )
        result = self._run_step(bootstrap, kind="bootstrap")
        if not result.ok:
            self._report_and_finish(
                STATUS_FAILED,
                "Could not create a virtualenv inside the runner, so no "
                "dependencies could be installed.",
            )
            return

        version = internal_command(
            ["python", "--version"],
            phase=CommandPhase.SETUP,
            purpose="ReproLab bootstrap: record the interpreter version",
        )
        version_result = self._run_step(version, kind="bootstrap")
        interpreter = ""
        for line in version_result.stdout.splitlines():
            if line.strip().startswith("Python "):
                interpreter = line.strip()
                break
        self.db.update_run(
            self.run_id,
            environment_json=json.dumps(
                {
                    "interpreter": interpreter or "unknown",
                    "runner": self.runner.runtime_info(),
                    "repo_commit": self.run.get("repo_commit") or "unknown",
                }
            ),
        )

        # --- setup phase ----------------------------------------------------
        setup_ok = True
        for spec in plan.get("setup_commands", []):
            self._check_cancel()
            command = self._rehydrate(spec, CommandPhase.SETUP)
            result = self._run_step(command, kind="setup")
            if not result.ok:
                setup_ok = False
                handled = self._handle_failure(command, result, kind="setup")
                if handled == "blocked":
                    return
                if handled == "repaired":
                    setup_ok = True
                    continue
                self._report_and_finish(
                    STATUS_FAILED,
                    f"Dependency installation failed: `{command.display}` exited "
                    f"with code {result.exit_code}.",
                )
                return
        if setup_ok:
            self.outcomes.environment_installed = True
            self.event("outcome", "Environment installed", "All setup commands succeeded.")

        # --- experiment phase -----------------------------------------------
        experiment_specs = plan.get("experiment_commands", [])
        if not experiment_specs:
            self._report_and_finish(
                STATUS_COMPLETED if setup_ok else STATUS_FAILED,
                "Setup check only: no experiment command was planned.",
            )
            return

        any_experiment_ran = False
        for spec in experiment_specs:
            self._check_cancel()
            if self._remaining_budget() <= 5:
                self.event(
                    "budget",
                    "Runtime budget exhausted",
                    "Remaining experiment commands were not attempted.",
                )
                self._report_and_finish(
                    STATUS_FAILED,
                    "The run exceeded its runtime budget before all experiment "
                    "commands were attempted.",
                )
                return
            command = self._rehydrate(spec, CommandPhase.EXPERIMENT)
            result = self._run_step(command, kind="experiment")
            if result.ok:
                any_experiment_ran = True
                if self._is_smoke_test(command):
                    self.outcomes.smoke_test_passed = True
                    self.event(
                        "outcome", "Smoke test passed", f"`{command.display}` exited 0."
                    )
                continue
            handled = self._handle_failure(command, result, kind="experiment")
            if handled == "blocked":
                return
            if handled == "repaired":
                any_experiment_ran = True
                continue
            self._report_and_finish(
                STATUS_FAILED,
                f"The experiment command `{command.display}` exited with code "
                f"{result.exit_code} and could not be repaired within "
                f"{self.limits.max_repairs} bounded attempt(s).",
            )
            return

        if any_experiment_ran:
            self.outcomes.experiment_completed = True
            self.event(
                "outcome",
                "Small experiment completed",
                "Every planned experiment command exited 0. This shows the code "
                "runs and produces output; it does not verify the paper's "
                "quantitative claims.",
            )
        self._report_and_finish(
            STATUS_COMPLETED, "All planned commands completed successfully."
        )

    def _is_smoke_test(self, command: ValidatedCommand) -> bool:
        joined = " ".join(command.argv).lower()
        return "pytest" in joined or "test" in joined or "import" in joined

    def _rehydrate(self, spec: dict[str, Any], phase: CommandPhase) -> ValidatedCommand:
        """Re-validate a stored command at execution time.

        The plan in the database is re-parsed rather than trusted, so a run
        cannot be made to execute something the validator would reject even if
        the stored row were tampered with.
        """
        return parse_command(
            spec.get("argv") or spec.get("command") or "",
            phase=phase,
            purpose=str(spec.get("purpose") or ""),
            workdir=str(spec.get("workdir") or "."),
        )

    # -- a single step -----------------------------------------------------
    def _run_step(
        self, command: ValidatedCommand, *, kind: str, repair_index: int | None = None
    ) -> StepResult:
        index = self.db.next_step_index(self.run_id)
        step_id = f"ev-{index:02d}"
        network = (
            "bridge (dependency acquisition)"
            if command.phase is CommandPhase.SETUP
            else "none (fully isolated)"
        )
        self.db.add_step(
            self.run_id,
            idx=index,
            kind=kind,
            purpose=command.purpose,
            command=command.display,
            argv=list(command.argv),
            phase=command.phase.value,
            workdir=command.workdir,
            network=network,
            status="running",
            repair_index=repair_index,
            step_id=step_id,
        )
        self.db.update_step(self.run_id, step_id, started_at=utcnow())
        self.event(
            "step",
            f"{step_id}: {command.display}",
            command.purpose,
            {
                "step_id": step_id,
                "kind": kind,
                "phase": command.phase.value,
                "network": network,
            },
        )

        timeout = min(self.limits.step_timeout_seconds, max(5, self._remaining_budget()))
        log_path = self.logs_dir / f"{step_id}.log"

        def on_output(chunk: str) -> None:
            if self.log_sink is not None:
                self.log_sink(step_id, chunk)

        result = self.runner.run(
            self.run_id,
            command,
            workspace=self.workspace.root,
            log_path=log_path,
            timeout_s=timeout,
            on_output=on_output,
            cancel_check=self._cancelled,
        )

        if result.cancelled:
            status = "cancelled"
        elif result.timed_out:
            status = "timeout"
        elif result.ok:
            status = "succeeded"
        else:
            status = "failed"
        self.db.update_step(
            self.run_id,
            step_id,
            status=status,
            exit_code=result.exit_code,
            finished_at=utcnow(),
            duration_ms=result.duration_ms,
            log_path=str(log_path),
            log_bytes=result.log_bytes,
            truncated=1 if result.truncated else 0,
            timed_out=1 if result.timed_out else 0,
        )
        self.event(
            "step_result",
            f"{step_id} {status}: exit {result.exit_code} "
            f"after {result.duration_ms / 1000:.1f}s",
            "",
            {
                "step_id": step_id,
                "status": status,
                "exit_code": result.exit_code,
                "duration_ms": result.duration_ms,
                "truncated": result.truncated,
                "timed_out": result.timed_out,
            },
        )
        if result.cancelled:
            raise RunCancelled("cancelled during execution")
        return result

    # -- failure handling --------------------------------------------------
    def _handle_failure(
        self, command: ValidatedCommand, result: StepResult, *, kind: str
    ) -> str:
        """Diagnose a real failure. Returns 'blocked', 'repaired' or 'failed'."""
        attempts_used = int(self.run.get("repair_attempts") or 0)
        repairs_left = max(0, self.limits.max_repairs - attempts_used)
        excerpt = self._log_excerpt(result.stdout)

        if result.timed_out:
            self.event(
                "diagnosis",
                "Step exceeded its timeout",
                f"`{command.display}` was terminated after the "
                f"{self.limits.step_timeout_seconds}s limit.",
            )
        self.event(
            "phase",
            "Diagnosing the failure",
            f"{repairs_left} bounded repair attempt(s) remaining.",
        )

        # Search on the real error text, not on a guess.
        troubleshooting = ""
        if self.tavily is not None and excerpt.strip():
            try:
                hits = self.tavily.troubleshoot(
                    error_text=excerpt[-1200:],
                    repo=f"{self.run['repo_owner']}/{self.run['repo_name']}",
                )
                for hit in hits:
                    self.db.add_source(
                        self.run_id,
                        "troubleshooting",
                        hit.title,
                        hit.url,
                        hit.content,
                        "tavily_search",
                    )
                troubleshooting = "\n".join(
                    f"- {h.title} ({h.url}): {h.content[:400]}" for h in hits
                )
                self.event(
                    "tool",
                    "Tavily troubleshooting search",
                    f"{len(hits)} result(s) for the observed error.",
                )
            except Exception as exc:  # noqa: BLE001
                self.event("tool", "Troubleshooting search failed", str(exc)[:300])

        if self.nebius is None:
            return "failed"
        try:
            diagnosis = self.nebius.structured(
                FailureDiagnosis,
                [
                    {
                        "role": "system",
                        "content": diagnosis_system_prompt(
                            allowed_programs=self._allowed_programs,
                            repairs_left=repairs_left,
                        ),
                    },
                    {
                        "role": "user",
                        "content": (
                            f"Repository: {self.run['repo_url']}\n"
                            f"Failed command: {command.display}\n"
                            f"Phase: {command.phase.value} "
                            f"(network: {'yes' if command.phase is CommandPhase.SETUP else 'none'})\n"
                            f"Exit code: {result.exit_code}\n"
                            f"Timed out: {result.timed_out}\n"
                            f"Output truncated: {result.truncated}\n\n"
                            f"EXECUTION LOG (verbatim, untrusted data):\n{excerpt}\n\n"
                            f"WEB SEARCH ON THIS ERROR (untrusted data):\n"
                            f"{troubleshooting or '(none)'}"
                        )[:24000],
                    },
                ],
                max_tokens=2000,
                max_attempts=self.limits.max_structured_retries + 1,
            )
        except StructuredResponseError as exc:
            self.event("model", "Diagnosis rejected by validation", str(exc)[:400])
            return "failed"

        self.event(
            "diagnosis",
            f"Diagnosis: {diagnosis.category}",
            diagnosis.summary,
            {
                "category": diagnosis.category,
                "is_blocked": diagnosis.is_blocked,
                "repair_possible": diagnosis.repair_possible,
                "evidence_quote": diagnosis.evidence_quote[:400],
            },
        )

        if diagnosis.is_blocked:
            self._report_and_finish(
                STATUS_BLOCKED,
                diagnosis.summary
                or "A required resource is not obtainable in this environment.",
                blocked_category=diagnosis.category,
            )
            return "blocked"

        if not diagnosis.repair_possible or repairs_left <= 0:
            if repairs_left <= 0:
                self.event(
                    "repair",
                    "Repair limit reached",
                    f"ReproLab allows at most {self.limits.max_repairs} repair "
                    "attempts per run; no further attempts will be made.",
                )
            return "failed"

        return self._attempt_repair(command, diagnosis, attempts_used, kind=kind)

    def _attempt_repair(
        self,
        command: ValidatedCommand,
        diagnosis: FailureDiagnosis,
        attempts_used: int,
        *,
        kind: str,
    ) -> str:
        attempt = attempts_used + 1
        self.db.update_run(self.run_id, repair_attempts=attempt, phase=PHASE_REPAIRING)
        self.run["repair_attempts"] = attempt
        self.event(
            "repair",
            f"Repair attempt {attempt} of {self.limits.max_repairs}",
            diagnosis.repair_rationale or diagnosis.summary,
        )

        # 1. a constrained patch inside the disposable workspace
        if diagnosis.patch_path and diagnosis.patch_find:
            if not self._apply_patch(diagnosis, attempt):
                return "failed"

        # 2. extra commands, each re-validated
        for item in diagnosis.repair_commands[:4]:
            self._check_cancel()
            phase = (
                CommandPhase.SETUP if item.phase == "setup" else CommandPhase.EXPERIMENT
            )
            try:
                repair_command = parse_command(
                    item.command,
                    phase=phase,
                    purpose=f"repair {attempt}: {item.purpose}",
                    workdir=item.workdir,
                )
            except CommandNotAllowed as exc:
                self.event(
                    "validation",
                    "Repair command rejected",
                    f"{item.command[:200]} -- {exc}",
                )
                continue
            repair_result = self._run_step(
                repair_command, kind="repair", repair_index=attempt
            )
            if not repair_result.ok:
                self.event(
                    "repair",
                    f"Repair attempt {attempt} did not succeed",
                    f"`{repair_command.display}` exited "
                    f"{repair_result.exit_code}.",
                )
                return "failed"

        # 3. retry the original command
        self.db.update_run(self.run_id, phase=PHASE_RUNNING)
        retry = self._run_step(command, kind=f"{kind}_retry", repair_index=attempt)
        if retry.ok:
            self.event(
                "repair",
                f"Repair attempt {attempt} succeeded",
                f"`{command.display}` now exits 0. The run is using a MODIFIED "
                "implementation if a patch was applied.",
            )
            return "repaired"

        remaining = self.limits.max_repairs - attempt
        if remaining > 0:
            return self._handle_failure(command, retry, kind=kind)
        self.event(
            "repair",
            "Repair budget exhausted",
            f"{self.limits.max_repairs} attempt(s) used; stopping as designed.",
        )
        return "failed"

    def _apply_patch(self, diagnosis: FailureDiagnosis, attempt: int) -> bool:
        """Apply one constrained substitution inside the workspace."""
        try:
            target = validate_patch_target(self.workspace.repo_dir, diagnosis.patch_path)
        except PathNotAllowed as exc:
            self.event("validation", "Patch rejected", f"{diagnosis.patch_path}: {exc}")
            return True  # not fatal; continue with command-only repair
        if not target.is_file():
            self.event(
                "validation",
                "Patch rejected",
                f"{diagnosis.patch_path} is not a file in the repository.",
            )
            return True
        try:
            original = target.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            self.event("validation", "Patch rejected", f"cannot read file: {exc}")
            return True
        occurrences = original.count(diagnosis.patch_find)
        if occurrences != 1:
            self.event(
                "validation",
                "Patch rejected",
                f"the text to replace appears {occurrences} time(s) in "
                f"{diagnosis.patch_path}; a patch must match exactly once.",
            )
            return True
        updated = original.replace(diagnosis.patch_find, diagnosis.patch_replace, 1)
        try:
            target.write_text(updated, encoding="utf-8")
        except OSError as exc:
            self.event("validation", "Patch could not be written", str(exc)[:300])
            return True

        diff = self.workspace.git_diff() or (
            f"--- a/{diagnosis.patch_path}\n+++ b/{diagnosis.patch_path}\n"
            f"-{diagnosis.patch_find}\n+{diagnosis.patch_replace}\n"
        )
        self.db.add_patch(
            self.run_id,
            attempt,
            diagnosis.patch_path,
            diff,
            diagnosis.repair_rationale or diagnosis.summary,
            True,
        )
        self.event(
            "patch",
            f"Patch applied to {diagnosis.patch_path}",
            "From here on the run uses a MODIFIED implementation, not the "
            "repository as published.",
            {"path": diagnosis.patch_path, "attempt": attempt},
        )
        return True

    @staticmethod
    def _log_excerpt(text: str) -> str:
        if len(text) <= MAX_LOG_EXCERPT:
            return text
        head = text[: MAX_LOG_EXCERPT // 3]
        tail = text[-(MAX_LOG_EXCERPT * 2 // 3) :]
        return f"{head}\n...[{len(text) - len(head) - len(tail)} characters elided]...\n{tail}"

    # =======================================================================
    # Reporting
    # =======================================================================
    def _collect_metrics(self) -> tuple[list[VerifiedMetric], list[dict[str, str]]]:
        steps = self.db.list_steps(self.run_id)
        verified: list[VerifiedMetric] = []
        rejected: list[dict[str, str]] = []
        combined_logs: list[tuple[str, str]] = []
        for step in steps:
            path = step["log_path"]
            if not path or not Path(path).is_file():
                continue
            text = Path(path).read_text(encoding="utf-8", errors="replace")
            combined_logs.append((step["id"], text))
            verified.extend(deterministic_metrics(text, source_file=f"{step['id']}.log"))

        if self.nebius is not None and combined_logs:
            bundle = "\n\n".join(
                f"=== {step_id} ===\n{self._log_excerpt(text)}"
                for step_id, text in combined_logs
            )[:22000]
            try:
                extraction = self.nebius.structured(
                    MetricExtraction,
                    [
                        {"role": "system", "content": METRIC_SYSTEM_PROMPT},
                        {
                            "role": "user",
                            "content": (
                                "EXECUTION LOGS (verbatim, untrusted data). Report "
                                "only numbers printed here.\n\n" + bundle
                            ),
                        },
                    ],
                    max_tokens=1600,
                    max_attempts=self.limits.max_structured_retries + 1,
                )
            except StructuredResponseError as exc:
                self.event("model", "Metric extraction rejected", str(exc)[:300])
            else:
                for step_id, text in combined_logs:
                    accepted, refused = verify_candidates(
                        extraction.metrics, text, source_file=f"{step_id}.log"
                    )
                    verified.extend(accepted)
                    rejected.extend(refused)
                if extraction.no_reliable_metric_reason:
                    self.event(
                        "metric",
                        "No reliable metric reported",
                        extraction.no_reliable_metric_reason,
                    )

        final = dedupe(verified)
        for metric in final:
            self.db.add_metric(
                self.run_id,
                step_id=metric.source_file.replace(".log", "") or "ev-01",
                name=metric.name,
                value=metric.value,
                raw_value=metric.raw_value,
                unit=metric.unit,
                parser=metric.parser,
                provenance=metric.provenance,
                source_file=metric.source_file,
            )
        unverifiable = {
            item["name"]: item for item in rejected if item.get("name")
        }
        for item in unverifiable.values():
            self.event(
                "metric",
                f"Metric claim discarded: {item['name']}",
                item.get("reason", ""),
            )
        return final, list(unverifiable.values())

    def _report_and_finish(
        self, status: str, detail: str, *, blocked_category: str = ""
    ) -> None:
        self.db.update_run(self.run_id, phase=PHASE_REPORTING)
        self.event("phase", "Generating the report")

        metrics, rejected_metrics = ([], [])
        try:
            metrics, rejected_metrics = self._collect_metrics()
        except Exception as exc:  # noqa: BLE001
            self.event("error", "Metric extraction failed", str(exc)[:400])

        artifacts: list[dict[str, object]] = []
        try:
            if self.workspace.root.is_dir():
                artifacts = self.workspace.collect_artifacts(
                    self.settings.artifacts_dir / self.run_id / "outputs",
                    max_bytes=self.limits.max_download_bytes,
                )
        except Exception as exc:  # noqa: BLE001
            self.event("error", "Artifact collection failed", str(exc)[:400])

        modified = False
        diff = ""
        try:
            if self.workspace.repo_dir.is_dir():
                modified = self.workspace.has_modifications()
                diff = self.workspace.git_diff()[:40000] if modified else ""
        except Exception:  # noqa: BLE001
            pass

        run_row = self.db.get_run(self.run_id)
        run = dict(run_row) if run_row else self.run
        steps = [dict(s) for s in self.db.list_steps(self.run_id)]
        sources = [dict(s) for s in self.db.list_sources(self.run_id)]
        patches = [dict(p) for p in self.db.list_patches(self.run_id)]
        tool_calls = [dict(t) for t in self.db.list_tool_calls(self.run_id)]

        assessment = self._write_assessment(
            status=status,
            detail=detail,
            run=run,
            steps=steps,
            metrics=metrics,
            artifacts=artifacts,
            modified=modified,
        )

        manifest = build_manifest(
            run=run,
            status=status,
            outcome_detail=detail,
            outcomes=self.outcomes.as_dict(),
            steps=steps,
            sources=sources,
            patches=patches,
            metrics=[m.as_dict() for m in metrics],
            rejected_metrics=rejected_metrics,
            artifacts=artifacts,
            tool_calls=tool_calls,
            assessment=assessment,
            settings=self.settings,
            runner_info=(
                self.runner.runtime_info() if self.runner is not None else {}
            ),
            modified_implementation=modified,
            diff=diff,
            blocked_category=blocked_category,
        )
        markdown = build_markdown_report(manifest)

        self.db.update_run(
            self.run_id,
            report_md=markdown,
            manifest_json=json.dumps(manifest, ensure_ascii=False, default=str),
        )
        self._finish(status, detail)

    def _write_assessment(
        self,
        *,
        status: str,
        detail: str,
        run: dict[str, Any],
        steps: list[dict[str, Any]],
        metrics: list[VerifiedMetric],
        artifacts: list[dict[str, object]],
        modified: bool,
    ) -> dict[str, Any]:
        """Ask the model for the narrative sections only.

        Statuses, exit codes, evidence ids and metrics are computed by
        ReproLab; the model never supplies them.
        """
        fallback = {
            "headline": f"{status}: {detail[:200]}",
            "what_was_attempted": detail,
            "what_the_paper_reports": "Not summarised: no model was available.",
            "what_reprolab_measured": (
                "; ".join(f"{m.name}={m.raw_value}{(' ' + m.unit) if m.unit else ''}" for m in metrics)
                or "No reliable metric was extracted from the execution logs."
            ),
            "differences_from_paper": [],
            "remains_unverified": [
                "The paper's quantitative claims were not verified by this run."
            ],
            "limitations": [],
            "missing_resources": [],
        }
        if self.nebius is None:
            return fallback

        plan = json.loads(run.get("plan_json") or "{}")
        evidence_lines = [
            f"- {s['id']} [{s['kind']}/{s['phase']}] `{s['command']}` -> "
            f"status={s['status']} exit={s['exit_code']} "
            f"{(s['duration_ms'] or 0) / 1000:.1f}s"
            for s in steps
        ]
        metric_lines = [
            f"- {m.name} = {m.raw_value} {m.unit} (parser={m.parser}, "
            f"evidence {m.source_file}: {m.provenance[:160]!r})"
            for m in metrics
        ] or ["- none parsed"]
        artifact_lines = [
            f"- {a.get('name')} ({a.get('bytes')} bytes)"
            for a in artifacts
            if a.get("collected")
        ] or ["- none"]

        try:
            assessment = self.nebius.structured(
                ReportAssessment,
                [
                    {"role": "system", "content": REPORT_SYSTEM_PROMPT},
                    {
                        "role": "user",
                        "content": (
                            f"RUN STATUS (computed by ReproLab, authoritative): {status}\n"
                            f"STATUS DETAIL: {detail}\n"
                            f"OUTCOME LADDER: {json.dumps(self.outcomes.as_dict())}\n"
                            f"PAPER: {run.get('paper_url')}\n"
                            f"REPOSITORY: {run.get('repo_url')} @ "
                            f"{run.get('repo_commit') or 'unknown'}\n"
                            f"TASK TYPE: {run.get('task_type')}\n"
                            f"GOAL: {run.get('goal') or '(none given)'}\n"
                            f"MODIFIED IMPLEMENTATION: {modified}\n\n"
                            f"PLAN OBJECTIVE: {plan.get('objective', '')}\n"
                            f"PLAN NOTE ON WHAT A RUN CAN SHOW: "
                            f"{plan.get('what_this_run_can_show', '')}\n"
                            f"PAPER CLAIM SUMMARY FROM PLANNING: "
                            f"{plan.get('paper_claim_summary', '')}\n"
                            f"PLAN BLOCKERS: {json.dumps(plan.get('blockers', []))}\n\n"
                            f"EXECUTED STEPS (real exit codes):\n"
                            + "\n".join(evidence_lines or ["- none"])
                            + "\n\nPARSED METRICS (ReproLab-verified against logs):\n"
                            + "\n".join(metric_lines)
                            + "\n\nCOLLECTED ARTIFACTS:\n"
                            + "\n".join(artifact_lines)
                            + "\n\nWrite the report's narrative sections. Do not "
                            "invent numbers; use only what appears above."
                        )[:24000],
                    },
                ],
                max_tokens=2600,
                max_attempts=self.limits.max_structured_retries + 1,
            )
        except (StructuredResponseError, NebiusError) as exc:
            self.event("model", "Report narrative unavailable", str(exc)[:300])
            return fallback
        return assessment.model_dump()

    def _finish(self, status: str, detail: str) -> None:
        self.db.update_run(
            self.run_id,
            status=status,
            phase=PHASE_DONE,
            outcome=status,
            outcome_detail=detail,
            finished_at=utcnow(),
        )
        self.event("done", f"Run finished: {status}", detail)
