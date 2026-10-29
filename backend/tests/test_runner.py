"""Isolated runner: real containers, real limits, real cancellation."""

from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

from reprolab.config import Limits
from reprolab.runner.commands import CommandPhase, internal_command, parse_command
from reprolab.runner.docker_runner import DockerRunner, RunnerUnavailable
from reprolab.runner.factory import build_runner
from reprolab.runner.workspace import Workspace

from conftest import requires_docker

pytestmark = [pytest.mark.docker, pytest.mark.slow]


@pytest.fixture
def runner() -> DockerRunner:
    return DockerRunner(
        image="python:3.11-slim",
        limits=Limits(
            step_timeout_seconds=60,
            container_memory_mb=512,
            container_cpus=1.0,
            max_log_bytes=4096,
            max_runtime_seconds=120,
        ),
    )


@pytest.fixture
def workspace(tmp_path: Path) -> Workspace:
    ws = Workspace.create(tmp_path, "test")
    ws.repo_dir.mkdir(parents=True, exist_ok=True)
    ws.repo_dir.chmod(0o777)
    return ws


def _write(workspace: Workspace, name: str, body: str) -> None:
    path = workspace.repo_dir / name
    path.write_text(body)
    path.chmod(0o666)


@requires_docker
class TestIsolation:
    def test_experiment_phase_has_no_network(self, runner, workspace, tmp_path) -> None:
        _write(
            workspace,
            "net.py",
            "import socket\n"
            "try:\n"
            "    socket.create_connection(('pypi.org', 443), timeout=5)\n"
            "    print('NETWORK REACHABLE')\n"
            "except Exception as exc:\n"
            "    print('no network:', type(exc).__name__)\n",
        )
        result = runner.run(
            "t", parse_command("python net.py", phase=CommandPhase.EXPERIMENT),
            workspace=workspace.root, log_path=tmp_path / "a.log", timeout_s=45,
        )
        assert "NETWORK REACHABLE" not in result.stdout
        assert "no network" in result.stdout

    def test_container_runs_as_non_root(self, runner, workspace, tmp_path) -> None:
        _write(workspace, "who.py", "import os\nprint('uid', os.getuid())\n")
        result = runner.run(
            "t", parse_command("python who.py", phase=CommandPhase.EXPERIMENT),
            workspace=workspace.root, log_path=tmp_path / "b.log", timeout_s=45,
        )
        assert "uid 0" not in result.stdout
        assert "uid 1000" in result.stdout

    def test_no_application_credentials_reach_the_container(
        self, runner, workspace, tmp_path, monkeypatch
    ) -> None:
        monkeypatch.setenv("NEBIUS_API_KEY", "nebius-secret-should-never-appear")
        monkeypatch.setenv("TAVILY_API_KEY", "tvly-secret-should-never-appear")
        result = runner.run(
            "t",
            internal_command(["env"], phase=CommandPhase.EXPERIMENT, purpose="env dump"),
            workspace=workspace.root, log_path=tmp_path / "c.log", timeout_s=45,
        )
        assert "NEBIUS_API_KEY" not in result.stdout
        assert "TAVILY_API_KEY" not in result.stdout
        assert "should-never-appear" not in result.stdout

    def test_no_docker_socket_and_read_only_root(self, runner, workspace, tmp_path) -> None:
        _write(
            workspace,
            "esc.py",
            "import os\n"
            "print('socket', os.path.exists('/var/run/docker.sock'))\n"
            "try:\n"
            "    open('/etc/x', 'w')\n"
            "    print('ROOTFS WRITABLE')\n"
            "except OSError as exc:\n"
            "    print('rootfs read-only:', type(exc).__name__)\n",
        )
        result = runner.run(
            "t", parse_command("python esc.py", phase=CommandPhase.EXPERIMENT),
            workspace=workspace.root, log_path=tmp_path / "d.log", timeout_s=45,
        )
        assert "socket False" in result.stdout
        assert "ROOTFS WRITABLE" not in result.stdout


@requires_docker
class TestLimits:
    def test_timeout_terminates_the_container(self, runner, workspace, tmp_path) -> None:
        _write(workspace, "spin.py", "import time\nprint('start', flush=True)\ntime.sleep(600)\n")
        started = time.monotonic()
        result = runner.run(
            "t", parse_command("python spin.py", phase=CommandPhase.EXPERIMENT),
            workspace=workspace.root, log_path=tmp_path / "e.log", timeout_s=8,
        )
        elapsed = time.monotonic() - started
        assert result.timed_out is True
        assert result.ok is False
        assert elapsed < 40, "the container should be killed promptly"

    def test_log_size_cap_truncates_and_stops_the_container(
        self, runner, workspace, tmp_path
    ) -> None:
        _write(workspace, "flood.py", "for _ in range(10**7):\n    print('x' * 200)\n")
        started = time.monotonic()
        result = runner.run(
            "t", parse_command("python flood.py", phase=CommandPhase.EXPERIMENT),
            workspace=workspace.root, log_path=tmp_path / "f.log", timeout_s=45,
        )
        elapsed = time.monotonic() - started
        assert result.truncated is True
        assert result.log_bytes <= runner.limits.max_log_bytes
        # A flood must not hold the reader open until the timeout.
        assert elapsed < 30

    def test_memory_cap_kills_a_runaway(self, runner, workspace, tmp_path) -> None:
        _write(
            workspace, "hog.py",
            "blocks = []\nwhile True:\n    blocks.append(bytearray(10 * 1024 * 1024))\n",
        )
        result = runner.run(
            "t", parse_command("python hog.py", phase=CommandPhase.EXPERIMENT),
            workspace=workspace.root, log_path=tmp_path / "g.log", timeout_s=45,
        )
        assert result.ok is False
        assert result.timed_out is False
        assert result.exit_code == 137


@requires_docker
class TestCancellation:
    def test_cancel_check_terminates_execution(self, runner, workspace, tmp_path) -> None:
        _write(workspace, "spin.py", "import time\nprint('start', flush=True)\ntime.sleep(600)\n")
        flag = {"cancelled": False}
        threading.Timer(4.0, lambda: flag.__setitem__("cancelled", True)).start()
        started = time.monotonic()
        result = runner.run(
            "t", parse_command("python spin.py", phase=CommandPhase.EXPERIMENT),
            workspace=workspace.root, log_path=tmp_path / "h.log", timeout_s=120,
            cancel_check=lambda: flag["cancelled"],
        )
        elapsed = time.monotonic() - started
        assert result.cancelled is True
        assert elapsed < 40, "cancellation must actually terminate the container"

    def test_cancel_method_kills_a_running_container(
        self, runner, workspace, tmp_path
    ) -> None:
        _write(workspace, "spin.py", "import time\nprint('start', flush=True)\ntime.sleep(600)\n")
        outcome: dict[str, object] = {}

        def execute() -> None:
            outcome["result"] = runner.run(
                "cancelme",
                parse_command("python spin.py", phase=CommandPhase.EXPERIMENT),
                workspace=workspace.root, log_path=tmp_path / "i.log", timeout_s=120,
            )

        thread = threading.Thread(target=execute, daemon=True)
        thread.start()
        time.sleep(6)
        assert runner.cancel("cancelme") is True
        thread.join(timeout=45)
        assert not thread.is_alive()
        assert outcome["result"].ok is False  # type: ignore[union-attr]


class TestBackendSelection:
    def test_unsupported_backend_refuses_rather_than_running_on_the_host(
        self, tmp_settings
    ) -> None:
        from dataclasses import replace

        settings = replace(tmp_settings, runner_backend="local_subprocess")
        with pytest.raises(RunnerUnavailable, match="will not run"):
            build_runner(settings)

    def test_missing_docker_binary_explains_the_minimum_setup(self) -> None:
        runner = DockerRunner(
            image="python:3.11-slim", limits=Limits(), docker_binary="definitely-not-docker"
        )
        with pytest.raises(RunnerUnavailable, match="Install Docker"):
            runner.preflight()
