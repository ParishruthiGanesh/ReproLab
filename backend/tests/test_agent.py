"""Agent behaviour: repair limits, status distinction, evidence in reports.

These drive the real AgentRun with a scripted model and a fake runner, so the
control flow under test is the production one.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from reprolab.agent.loop import AgentRun
from reprolab.agent.schemas import FailureDiagnosis, MetricExtraction, ReportAssessment
from reprolab.db import (
    STATUS_BLOCKED,
    STATUS_CANCELLED,
    STATUS_COMPLETED,
    STATUS_FAILED,
    Database,
)
from reprolab.runner.docker_runner import StepResult


class FakeRunner:
    """Records commands and replays scripted results."""

    backend = "fake"

    def __init__(self, outcomes: dict[str, int], default: int = 0) -> None:
        self.outcomes = outcomes
        self.default = default
        self.commands: list[str] = []
        self.cancelled = False

    def preflight(self) -> None:
        return None

    def ensure_image(self) -> str:
        return "present"

    def runtime_info(self) -> dict[str, Any]:
        return {"backend": "fake", "isolation": "test double", "image": "none"}

    def run(self, run_id, command, *, workspace, log_path, timeout_s=None,
            on_output=None, cancel_check=None) -> StepResult:
        display = command.display
        self.commands.append(display)
        exit_code = self.default
        stdout = f"$ {display}\n"
        for needle, code in self.outcomes.items():
            if needle in display:
                exit_code = code
                break
        if exit_code != 0:
            stdout += "ModuleNotFoundError: No module named 'widget'\n"
        else:
            stdout += "13 passed in 0.15s\n"
        Path(log_path).parent.mkdir(parents=True, exist_ok=True)
        Path(log_path).write_text(stdout, encoding="utf-8")
        if on_output:
            on_output(stdout)
        return StepResult(
            exit_code=exit_code, stdout=stdout, duration_ms=12, timed_out=False,
            cancelled=False, truncated=False, log_bytes=len(stdout),
            network="none (fully isolated)", container_name="fake",
        )

    def cancel(self, run_id: str) -> bool:
        self.cancelled = True
        return True


class ScriptedModel:
    """Returns pre-built schema objects in order, per schema type."""

    def __init__(self, by_schema: dict[str, list[Any]], model: str = "nvidia/Test") -> None:
        self.by_schema = {k: list(v) for k, v in by_schema.items()}
        self.model = model
        self.calls: list[str] = []

    def structured(self, schema, messages, **kwargs):
        name = schema.__name__
        self.calls.append(name)
        queue = self.by_schema.get(name) or []
        if not queue:
            if name == "MetricExtraction":
                return MetricExtraction(metrics=[], no_reliable_metric_reason="none")
            if name == "ReportAssessment":
                return ReportAssessment(
                    headline="scripted", what_was_attempted="scripted run"
                )
            raise AssertionError(f"no scripted response for {name}")
        return queue.pop(0)

    def chat(self, *args, **kwargs) -> str:
        return "ok"


def make_run(db: Database, settings, plan: dict, task_type="small_experiment") -> dict:
    run_id = db.create_run(
        paper_url="https://arxiv.org/abs/2407.03618",
        repo_url="https://github.com/xhluca/bm25s",
        repo_owner="xhluca", repo_name="bm25s", goal="fixture",
        task_type=task_type, runtime_budget_s=120, runner_backend="fake",
    )
    db.update_run(run_id, plan_json=json.dumps(plan), repo_commit="abc123def456")
    return dict(db.get_run(run_id))


def plan_with(setup: list[str], experiment: list[str]) -> dict:
    def spec(command: str, phase: str) -> dict:
        return {
            "argv": command.split(), "command": command, "phase": phase,
            "purpose": f"documented {phase} step", "workdir": ".",
        }

    return {
        "objective": "fixture objective",
        "setup_commands": [spec(c, "setup") for c in setup],
        "experiment_commands": [spec(c, "experiment") for c in experiment],
        "blockers": [], "is_blocked": False, "blocked_reason": "",
        "limits": {"max_repairs": 2},
    }


def build_agent(db, settings, run, runner, model) -> AgentRun:
    agent = AgentRun(
        db=db, settings=settings, run_row=run, nebius=model, tavily=None, runner=runner
    )
    # The workspace is not cloned in these tests; point it at a real directory
    # so artifact collection and diff inspection behave normally.
    workspace_root = settings.workspaces_dir / run["id"]
    (workspace_root / "repo").mkdir(parents=True, exist_ok=True)
    (workspace_root / "outputs").mkdir(parents=True, exist_ok=True)
    agent.workspace.root = workspace_root
    return agent


class TestStatusDistinction:
    def test_all_commands_succeed_is_completed(self, db, tmp_settings) -> None:
        run = make_run(db, tmp_settings, plan_with(["pip install ."], ["pytest -q"]))
        runner = FakeRunner({})
        agent = build_agent(db, tmp_settings, run, runner, ScriptedModel({}))
        agent.execute()
        row = db.get_run(run["id"])
        assert row["status"] == STATUS_COMPLETED
        assert agent.outcomes.environment_installed is True
        assert agent.outcomes.experiment_completed is True
        # Success at the small-experiment level never implies reproduction.
        assert agent.outcomes.specific_result_reproduced is False

    def test_unrepairable_failure_is_failed_not_blocked(self, db, tmp_settings) -> None:
        run = make_run(db, tmp_settings, plan_with([], ["pytest -q"]))
        runner = FakeRunner({"pytest": 1})
        model = ScriptedModel(
            {
                "FailureDiagnosis": [
                    FailureDiagnosis(
                        summary="genuine code error",
                        category="code_error",
                        is_blocked=False,
                        repair_possible=False,
                        evidence_quote="ModuleNotFoundError: No module named 'widget'",
                    )
                ]
            }
        )
        agent = build_agent(db, tmp_settings, run, runner, model)
        agent.execute()
        row = db.get_run(run["id"])
        assert row["status"] == STATUS_FAILED
        assert row["status"] != STATUS_BLOCKED

    @pytest.mark.parametrize(
        "category",
        ["missing_model_weights", "missing_dataset", "unsupported_hardware"],
    )
    def test_missing_resource_is_blocked_not_failed(
        self, db, tmp_settings, category: str
    ) -> None:
        run = make_run(db, tmp_settings, plan_with([], ["python eval.py"]))
        runner = FakeRunner({"eval.py": 1})
        model = ScriptedModel(
            {
                "FailureDiagnosis": [
                    FailureDiagnosis(
                        summary=f"required resource unavailable: {category}",
                        category=category,
                        is_blocked=True,
                        repair_possible=False,
                        evidence_quote="ModuleNotFoundError: No module named 'widget'",
                    )
                ]
            }
        )
        agent = build_agent(db, tmp_settings, run, runner, model)
        agent.execute()
        row = db.get_run(run["id"])
        assert row["status"] == STATUS_BLOCKED
        assert row["status"] != STATUS_FAILED
        manifest = json.loads(row["manifest_json"])
        assert manifest["status"]["blocked_category"] == category
        # A blocked run is explicitly not a failure of the code.
        assert "not a failure of the code" in manifest["status"]["meaning"]

    def test_cancellation_is_its_own_status(self, db, tmp_settings) -> None:
        run = make_run(db, tmp_settings, plan_with([], ["pytest -q"]))
        db.update_run(run["id"], cancel_requested=1)
        agent = build_agent(db, tmp_settings, run, FakeRunner({}), ScriptedModel({}))
        agent.execute()
        assert db.get_run(run["id"])["status"] == STATUS_CANCELLED


class TestRepairLimits:
    def test_repair_is_attempted_and_can_succeed(self, db, tmp_settings) -> None:
        run = make_run(db, tmp_settings, plan_with([], ["pytest -q"]))

        class FlakyRunner(FakeRunner):
            def __init__(self) -> None:
                super().__init__({})
                self.seen = 0

            def run(self, run_id, command, **kwargs):
                if "pytest" in command.display:
                    self.seen += 1
                    if self.seen == 1:
                        self.outcomes = {"pytest": 1}
                    else:
                        self.outcomes = {}
                return super().run(run_id, command, **kwargs)

        model = ScriptedModel(
            {
                "FailureDiagnosis": [
                    FailureDiagnosis(
                        summary="missing package",
                        category="missing_dependency",
                        is_blocked=False,
                        repair_possible=True,
                        repair_rationale="install the missing package",
                        evidence_quote="ModuleNotFoundError: No module named 'widget'",
                        repair_commands=[
                            {
                                "command": "pip install widget",
                                "phase": "setup",
                                "purpose": "install missing dependency",
                                "workdir": ".",
                            }
                        ],
                    )
                ]
            }
        )
        agent = build_agent(db, tmp_settings, run, FlakyRunner(), model)
        agent.execute()
        row = db.get_run(run["id"])
        assert row["status"] == STATUS_COMPLETED
        assert row["repair_attempts"] == 1

    def test_repairs_stop_at_the_configured_limit(self, db, tmp_settings) -> None:
        """The limit is enforced by ReproLab, not requested of the model."""
        run = make_run(db, tmp_settings, plan_with([], ["pytest -q"]))
        runner = FakeRunner({"pytest": 1})  # never recovers

        def always_repairable() -> FailureDiagnosis:
            return FailureDiagnosis(
                summary="missing package",
                category="missing_dependency",
                is_blocked=False,
                repair_possible=True,
                repair_rationale="try again",
                evidence_quote="ModuleNotFoundError: No module named 'widget'",
                repair_commands=[
                    {
                        "command": "pip install widget",
                        "phase": "setup",
                        "purpose": "retry",
                        "workdir": ".",
                    }
                ],
            )

        # Supply far more diagnoses than the limit allows.
        model = ScriptedModel({"FailureDiagnosis": [always_repairable() for _ in range(10)]})
        agent = build_agent(db, tmp_settings, run, runner, model)
        agent.execute()
        row = db.get_run(run["id"])
        assert row["status"] == STATUS_FAILED
        assert row["repair_attempts"] == tmp_settings.limits.max_repairs == 2
        # Exactly two repair installs were run, no more.
        assert runner.commands.count("pip install widget") == 2

    def test_repair_commands_are_revalidated(self, db, tmp_settings) -> None:
        """A repair that proposes a forbidden command is rejected, not run."""
        run = make_run(db, tmp_settings, plan_with([], ["pytest -q"]))
        runner = FakeRunner({"pytest": 1})
        model = ScriptedModel(
            {
                "FailureDiagnosis": [
                    FailureDiagnosis(
                        summary="x", category="code_error", is_blocked=False,
                        repair_possible=True, repair_rationale="run a shell script",
                        evidence_quote="ModuleNotFoundError: No module named 'widget'",
                        repair_commands=[
                            {
                                "command": "bash fix.sh",
                                "phase": "setup",
                                "purpose": "forbidden",
                                "workdir": ".",
                            }
                        ],
                    ),
                    FailureDiagnosis(
                        summary="x", category="code_error", is_blocked=False,
                        repair_possible=False, repair_rationale="",
                        evidence_quote="ModuleNotFoundError: No module named 'widget'",
                    ),
                ]
            }
        )
        agent = build_agent(db, tmp_settings, run, runner, model)
        agent.execute()
        assert not any("bash" in c for c in runner.commands)
        events = [dict(e) for e in db.list_events(run["id"])]
        assert any(
            e["kind"] == "validation" and "rejected" in e["title"].lower()
            for e in events
        )

    def test_patch_must_match_exactly_once(self, db, tmp_settings) -> None:
        run = make_run(db, tmp_settings, plan_with([], ["pytest -q"]))
        runner = FakeRunner({"pytest": 1})
        agent = build_agent(db, tmp_settings, run, runner, ScriptedModel({}))
        target = agent.workspace.repo_dir / "mod.py"
        target.write_text("x = 1\nx = 1\n")  # ambiguous: appears twice
        diagnosis = FailureDiagnosis(
            summary="x", category="code_error", is_blocked=False,
            repair_possible=True, evidence_quote="e",
            patch_path="mod.py", patch_find="x = 1", patch_replace="x = 2",
        )
        agent._apply_patch(diagnosis, attempt=1)
        assert target.read_text() == "x = 1\nx = 1\n"  # unchanged
        assert db.list_patches(run["id"]) == []

    def test_patch_outside_workspace_is_refused(self, db, tmp_settings) -> None:
        run = make_run(db, tmp_settings, plan_with([], ["pytest -q"]))
        agent = build_agent(db, tmp_settings, run, FakeRunner({}), ScriptedModel({}))
        diagnosis = FailureDiagnosis(
            summary="x", category="code_error", is_blocked=False,
            repair_possible=True, evidence_quote="e",
            patch_path="../../../etc/passwd", patch_find="root", patch_replace="owned",
        )
        agent._apply_patch(diagnosis, attempt=1)
        assert db.list_patches(run["id"]) == []
        events = [dict(e) for e in db.list_events(run["id"])]
        assert any("Patch rejected" in e["title"] for e in events)


class TestEvidenceInReports:
    def test_every_step_gets_an_evidence_id_and_log(self, db, tmp_settings) -> None:
        run = make_run(db, tmp_settings, plan_with(["pip install ."], ["pytest -q"]))
        agent = build_agent(db, tmp_settings, run, FakeRunner({}), ScriptedModel({}))
        agent.execute()
        manifest = json.loads(db.get_run(run["id"])["manifest_json"])
        evidence = manifest["evidence"]
        assert len(evidence) >= 3
        for item in evidence:
            assert item["evidence_id"].startswith("ev-")
            assert item["log_file"] == f"logs/{item['evidence_id']}.log"
            assert item["exit_code"] is not None
            assert item["command"]

    def test_report_separates_paper_claims_from_measurements(
        self, db, tmp_settings
    ) -> None:
        run = make_run(db, tmp_settings, plan_with([], ["pytest -q"]))
        model = ScriptedModel(
            {
                "ReportAssessment": [
                    ReportAssessment(
                        headline="installed and smoke-tested",
                        what_was_attempted="ran the documented test subset",
                        what_the_paper_reports="large throughput gains on BEIR",
                        what_reprolab_measured="13 tests passed",
                        differences_from_paper=["no BEIR corpus was used"],
                        remains_unverified=["all quantitative claims"],
                    )
                ]
            }
        )
        agent = build_agent(db, tmp_settings, run, FakeRunner({}), model)
        agent.execute()
        row = db.get_run(run["id"])
        report = row["report_md"]
        assert "## What the paper reports" in report
        assert "## What ReproLab actually measured" in report
        assert "## What remains unverified" in report
        assert "## Differences from the paper's conditions" in report
        # The keystone outcome is present and negative.
        assert "A specific reported result reproduced under matching conditions" in report

    def test_metrics_carry_provenance_to_a_real_log_line(
        self, db, tmp_settings
    ) -> None:
        run = make_run(db, tmp_settings, plan_with([], ["pytest -q"]))
        agent = build_agent(db, tmp_settings, run, FakeRunner({}), ScriptedModel({}))
        agent.execute()
        metrics = [dict(m) for m in db.list_metrics(run["id"])]
        assert metrics, "the deterministic pytest parser should find a metric"
        passed = next(m for m in metrics if m["name"] == "pytest_passed")
        assert passed["value"] == 13.0
        assert passed["parser"] == "pytest_summary"
        assert "13 passed" in passed["provenance"]
        assert passed["step_id"].startswith("ev-")

    def test_hallucinated_metric_is_discarded(self, db, tmp_settings) -> None:
        from reprolab.agent.schemas import MetricCandidate

        run = make_run(db, tmp_settings, plan_with([], ["pytest -q"]))
        model = ScriptedModel(
            {
                "MetricExtraction": [
                    MetricExtraction(
                        metrics=[
                            MetricCandidate(
                                name="accuracy",
                                raw_value="0.93",
                                unit="",
                                evidence_line="Final accuracy on NQ: 0.93",
                            )
                        ]
                    )
                ]
            }
        )
        agent = build_agent(db, tmp_settings, run, FakeRunner({}), model)
        agent.execute()
        manifest = json.loads(db.get_run(run["id"])["manifest_json"])
        names = [m["name"] for m in manifest["measurements"]["verified"]]
        assert "accuracy" not in names
        discarded = manifest["measurements"]["discarded_claims"]
        assert any(d["name"] == "accuracy" for d in discarded)
        assert "verbatim" in discarded[0]["reason"]
