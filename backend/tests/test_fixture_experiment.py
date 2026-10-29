"""Deterministic local fixture.

This exercises the whole execution path -- validated command, isolated
container, recorded evidence, parsed metric -- against a synthetic repository
checked into this test suite.

It is a FIXTURE, not a research reproduction. The fixture computes a fixed
number with no randomness, no network and no dataset. A passing test here says
ReproLab's machinery works; it says nothing about any paper.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

from reprolab.agent.metrics import deterministic_metrics, verify_candidates
from reprolab.agent.schemas import MetricCandidate
from reprolab.config import Limits
from reprolab.runner.commands import CommandPhase, internal_command, parse_command
from reprolab.runner.docker_runner import DockerRunner
from reprolab.runner.workspace import Workspace

from conftest import FIXTURE_REPO, requires_docker

pytestmark = [pytest.mark.docker, pytest.mark.slow]

EXPECTED_SCORE_LINE = "fixture_score: 0.6000"


@pytest.fixture
def fixture_workspace(tmp_path: Path) -> Workspace:
    workspace = Workspace.create(tmp_path, "fixture")
    shutil.copytree(FIXTURE_REPO, workspace.repo_dir)
    for path in [workspace.repo_dir, *workspace.repo_dir.rglob("*")]:
        path.chmod(0o777 if path.is_dir() else 0o666)
    return workspace


@pytest.fixture
def runner() -> DockerRunner:
    return DockerRunner(
        image="python:3.11-slim",
        limits=Limits(step_timeout_seconds=120, container_memory_mb=1024,
                      max_log_bytes=262144, max_runtime_seconds=300),
        # Behind a TLS-intercepting proxy the install phase needs the same
        # trust bundle production uses.
        ca_bundle=os.environ.get("REPROLAB_RUNNER_CA_BUNDLE", ""),
    )


@requires_docker
class TestFixtureIsClearlyLabelled:
    def test_fixture_readme_disclaims_research_status(self) -> None:
        readme = (FIXTURE_REPO / "README.md").read_text()
        assert "NOT a research reproduction" in readme
        assert "not** a paper" in readme
        assert "is **not** evidence that any paper reproduces" in readme


@requires_docker
class TestFixtureExperiment:
    def test_experiment_runs_offline_and_is_byte_identical(
        self, runner, fixture_workspace, tmp_path
    ) -> None:
        command = parse_command(
            "python experiment.py",
            phase=CommandPhase.EXPERIMENT,
            purpose="documented fixture experiment",
        )
        first = runner.run(
            "fx", command, workspace=fixture_workspace.root,
            log_path=tmp_path / "1.log", timeout_s=60,
        )
        second = runner.run(
            "fx", command, workspace=fixture_workspace.root,
            log_path=tmp_path / "2.log", timeout_s=60,
        )
        assert first.exit_code == 0 and second.exit_code == 0
        # No network was available, and the output is deterministic.
        assert "none (fully isolated)" in first.network
        body_one = [l for l in first.stdout.splitlines() if l.startswith("fixture")]
        body_two = [l for l in second.stdout.splitlines() if l.startswith("fixture")]
        assert body_one == body_two
        assert any(EXPECTED_SCORE_LINE in line for line in body_one)

    def test_metric_is_parsed_with_provenance_from_the_real_log(
        self, runner, fixture_workspace, tmp_path
    ) -> None:
        log_path = tmp_path / "exp.log"
        runner.run(
            "fx", parse_command("python experiment.py", phase=CommandPhase.EXPERIMENT),
            workspace=fixture_workspace.root, log_path=log_path, timeout_s=60,
        )
        log_text = log_path.read_text()
        score_line = next(
            line.strip() for line in log_text.splitlines() if "fixture_score" in line
        )
        verified, rejected = verify_candidates(
            [
                MetricCandidate(
                    name="fixture_score", raw_value="0.6000", unit="",
                    evidence_line=score_line,
                )
            ],
            log_text,
            source_file="ev-01.log",
        )
        assert not rejected
        assert verified[0].value == pytest.approx(0.6)
        assert verified[0].provenance == score_line
        assert verified[0].parser == "model_located_reprolab_verified"

    def test_fabricated_metric_against_the_fixture_log_is_discarded(
        self, runner, fixture_workspace, tmp_path
    ) -> None:
        log_path = tmp_path / "exp2.log"
        runner.run(
            "fx", parse_command("python experiment.py", phase=CommandPhase.EXPERIMENT),
            workspace=fixture_workspace.root, log_path=log_path, timeout_s=60,
        )
        verified, rejected = verify_candidates(
            [
                MetricCandidate(
                    name="accuracy", raw_value="0.99", unit="",
                    evidence_line="Final accuracy: 0.99 on the test split",
                )
            ],
            log_path.read_text(),
        )
        assert verified == []
        assert rejected and "verbatim" in rejected[0]["reason"]

    def test_documented_smoke_test_passes_and_yields_a_pytest_metric(
        self, runner, fixture_workspace, tmp_path
    ) -> None:
        bootstrap = runner.run(
            "fx",
            internal_command(
                ["python", "-m", "venv", "/workspace/.venv"],
                phase=CommandPhase.SETUP, purpose="bootstrap",
            ),
            workspace=fixture_workspace.root, log_path=tmp_path / "boot.log",
            timeout_s=120,
        )
        assert bootstrap.exit_code == 0
        install = runner.run(
            "fx", parse_command("pip install pytest", phase=CommandPhase.SETUP),
            workspace=fixture_workspace.root, log_path=tmp_path / "pip.log",
            timeout_s=180,
        )
        if install.exit_code != 0:
            # Distinguish "this machine cannot reach PyPI" from a real defect:
            # only the former is a legitimate skip.
            network_failure = any(
                marker in install.stdout
                for marker in ("CERTIFICATE_VERIFY_FAILED", "Max retries exceeded",
                               "Temporary failure in name resolution",
                               "No matching distribution found")
            )
            if network_failure:
                pytest.skip(
                    "the dependency-install phase cannot reach PyPI from this "
                    "machine; set REPROLAB_RUNNER_CA_BUNDLE if a TLS-intercepting "
                    "proxy is in use"
                )
            pytest.fail(f"pip install failed unexpectedly: {install.stdout[-600:]}")

        log_path = tmp_path / "pytest.log"
        result = runner.run(
            "fx",
            parse_command("pytest test_fixture.py -q", phase=CommandPhase.EXPERIMENT),
            workspace=fixture_workspace.root, log_path=log_path, timeout_s=120,
        )
        assert result.exit_code == 0, result.stdout[-600:]
        metrics = deterministic_metrics(log_path.read_text(), source_file="ev-04.log")
        passed = next(m for m in metrics if m.name == "pytest_passed")
        assert passed.value == 2.0
        assert "2 passed" in passed.provenance


@requires_docker
class TestFixtureFailure:
    def test_missing_dependency_fails_with_a_real_diagnosable_error(
        self, runner, fixture_workspace, tmp_path
    ) -> None:
        log_path = tmp_path / "fail.log"
        result = runner.run(
            "fx", parse_command("python failing.py", phase=CommandPhase.EXPERIMENT),
            workspace=fixture_workspace.root, log_path=log_path, timeout_s=60,
        )
        assert result.exit_code != 0
        assert result.ok is False
        assert "ModuleNotFoundError" in result.stdout
        assert "unreachable" not in result.stdout
