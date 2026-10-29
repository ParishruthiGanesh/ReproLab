"""Isolated execution of approved commands.

Token Factory provides model inference only. Repository code never runs there
and never runs inside the ReproLab process: it runs here, in a disposable
container with no application credentials, dropped capabilities and hard
resource caps.

Two isolation profiles:
  * ``setup``      -- outbound network, so dependencies can be acquired.
  * ``experiment`` -- ``--network none``; nothing can phone home.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from ..config import Limits
from ..security.redact import redact
from .commands import CommandPhase, ValidatedCommand

CONTAINER_WORKSPACE = "/workspace"
CONTAINER_REPO = "/workspace/repo"
CONTAINER_VENV = "/workspace/.venv"
# Matching uid/gid are created on the host workspace; the image's root user is
# never used.
RUN_UID = 1000
RUN_GID = 1000


class RunnerUnavailable(RuntimeError):
    """Raised when no isolated runner backend is usable.

    ReproLab refuses to run untrusted repository code on the host instead of
    falling back silently.
    """


@dataclass
class StepResult:
    exit_code: int | None
    stdout: str
    duration_ms: int
    timed_out: bool
    cancelled: bool
    truncated: bool
    log_bytes: int
    network: str
    container_name: str
    oom_killed: bool = False
    error: str = ""

    @property
    def ok(self) -> bool:
        return self.exit_code == 0 and not self.timed_out and not self.cancelled


@dataclass
class _Process:
    popen: subprocess.Popen
    container_name: str


class DockerRunner:
    """Runs one validated command at a time inside a throwaway container."""

    backend = "docker"

    def __init__(
        self,
        *,
        image: str,
        limits: Limits,
        ca_bundle: str = "",
        docker_binary: str = "docker",
    ) -> None:
        self.image = image
        self.limits = limits
        self.ca_bundle = ca_bundle
        self.docker = docker_binary
        self._lock = threading.Lock()
        self._active: dict[str, _Process] = {}

    # -- availability -------------------------------------------------------
    def preflight(self) -> None:
        """Verify Docker is usable, or raise with the minimum setup needed."""
        if shutil.which(self.docker) is None:
            raise RunnerUnavailable(
                "Docker CLI not found. ReproLab needs Docker to isolate untrusted "
                "repository code and will not run it on the host. Install Docker "
                "Engine or Docker Desktop, then restart the backend."
            )
        try:
            proc = subprocess.run(
                [self.docker, "info", "--format", "{{.ServerVersion}}"],
                capture_output=True,
                text=True,
                timeout=30,
            )
        except (subprocess.SubprocessError, OSError) as exc:
            raise RunnerUnavailable(f"cannot talk to the Docker daemon: {exc}") from exc
        if proc.returncode != 0:
            raise RunnerUnavailable(
                "Docker daemon is not reachable: "
                + (proc.stderr or proc.stdout).strip()[:300]
                + "\nStart the daemon (e.g. `sudo dockerd` or Docker Desktop) and retry."
            )

    def ensure_image(self, timeout: int = 900) -> str:
        """Pull the runner image if it is not present locally."""
        inspect = subprocess.run(
            [self.docker, "image", "inspect", self.image],
            capture_output=True,
            text=True,
            timeout=60,
        )
        if inspect.returncode == 0:
            return "present"
        pull = subprocess.run(
            [self.docker, "pull", self.image],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        if pull.returncode != 0:
            raise RunnerUnavailable(
                f"cannot pull runner image {self.image!r}: "
                + (pull.stderr or pull.stdout).strip()[:300]
            )
        return "pulled"

    def image_digest(self) -> str:
        proc = subprocess.run(
            [self.docker, "image", "inspect", self.image, "--format", "{{.Id}}"],
            capture_output=True,
            text=True,
            timeout=60,
        )
        return proc.stdout.strip() if proc.returncode == 0 else "unknown"

    def runtime_info(self) -> dict[str, object]:
        version = subprocess.run(
            [self.docker, "version", "--format", "{{.Server.Version}}"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        return {
            "backend": "docker",
            "isolation": "disposable non-root container, cap-drop ALL, "
            "no-new-privileges, read-only root filesystem",
            "docker_server_version": version.stdout.strip() or "unknown",
            "image": self.image,
            "image_digest": self.image_digest(),
            "cpus": self.limits.container_cpus,
            "memory_mb": self.limits.container_memory_mb,
            "pids_limit": self.limits.container_pids_limit,
            "step_timeout_seconds": self.limits.step_timeout_seconds,
            "max_log_bytes": self.limits.max_log_bytes,
            "credentials_passed_to_container": False,
        }

    # -- command construction ----------------------------------------------
    def _docker_argv(
        self,
        command: ValidatedCommand,
        *,
        workspace: Path,
        container_name: str,
        timeout_s: int,
    ) -> list[str]:
        network = "bridge" if command.phase is CommandPhase.SETUP else "none"
        mem = f"{self.limits.container_memory_mb}m"
        workdir = f"{CONTAINER_REPO}/{command.workdir}".rstrip("/.") or CONTAINER_REPO

        argv = [
            self.docker,
            "run",
            "--rm",
            "--name", container_name,
            "--network", network,
            # Never root inside the container.
            "--user", f"{RUN_UID}:{RUN_GID}",
            # No capabilities, no privilege escalation, no privileged mode.
            "--cap-drop", "ALL",
            "--security-opt", "no-new-privileges:true",
            # Resource caps enforced by the kernel, not by the model.
            "--cpus", str(self.limits.container_cpus),
            "--memory", mem,
            "--memory-swap", mem,
            "--pids-limit", str(self.limits.container_pids_limit),
            "--ulimit", f"nofile=1024:2048",
            "--ulimit", f"cpu={timeout_s}:{timeout_s + 30}",
            "--ulimit", "fsize=2147483648",
            # Root filesystem is read-only; only the workspace and /tmp are writable.
            "--read-only",
            "--tmpfs", "/tmp:rw,noexec,nosuid,size=512m,mode=1777",
            "--tmpfs", "/run:rw,noexec,nosuid,size=16m",
            # The disposable workspace. No host filesystem beyond this, and no
            # Docker socket -- the container cannot start containers.
            "-v", f"{workspace.resolve()}:{CONTAINER_WORKSPACE}:rw",
            "-w", workdir,
            "--init",
            "--log-driver", "none",
            "--attach", "stdout",
            "--attach", "stderr",
        ]
        # Only non-sensitive environment. No NEBIUS_API_KEY, no TAVILY_API_KEY,
        # no proxy credentials -- nothing from the orchestrator's environment.
        env = {
            "HOME": f"{CONTAINER_WORKSPACE}/.home",
            "PATH": f"{CONTAINER_VENV}/bin:/usr/local/bin:/usr/bin:/bin",
            "PYTHONUNBUFFERED": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONHASHSEED": "0",
            "TMPDIR": "/tmp",
            "VIRTUAL_ENV": CONTAINER_VENV,
            "PIP_DISABLE_PIP_VERSION_CHECK": "1",
            "PIP_NO_INPUT": "1",
            "REPROLAB_SANDBOX": "1",
            "OMP_NUM_THREADS": str(max(1, int(self.limits.container_cpus))),
        }
        if command.phase is CommandPhase.SETUP and self.ca_bundle:
            bundle = Path(self.ca_bundle)
            if bundle.is_file():
                argv += ["-v", f"{bundle.resolve()}:/etc/reprolab-ca.crt:ro"]
                env["SSL_CERT_FILE"] = "/etc/reprolab-ca.crt"
                env["REQUESTS_CA_BUNDLE"] = "/etc/reprolab-ca.crt"
                env["PIP_CERT"] = "/etc/reprolab-ca.crt"
        for key, value in env.items():
            argv += ["-e", f"{key}={value}"]
        argv.append(self.image)
        argv.extend(command.argv)
        return argv

    # -- execution ----------------------------------------------------------
    def run(
        self,
        run_id: str,
        command: ValidatedCommand,
        *,
        workspace: Path,
        log_path: Path,
        timeout_s: int | None = None,
        on_output=None,
        cancel_check=None,
    ) -> StepResult:
        """Execute ``command`` in a fresh container and capture its output.

        Limits are enforced here: wall-clock timeout kills the container, the
        log cap stops reading and kills the container, and ``cancel_check``
        is polled so a cancel request actually terminates execution.
        """
        timeout_s = int(timeout_s or self.limits.step_timeout_seconds)
        timeout_s = max(5, min(timeout_s, self.limits.max_runtime_seconds))
        container_name = f"reprolab-{run_id[:16]}-{uuid.uuid4().hex[:8]}"
        argv = self._docker_argv(
            command, workspace=workspace, container_name=container_name,
            timeout_s=timeout_s,
        )
        network = "bridge (dependency acquisition)" if (
            command.phase is CommandPhase.SETUP
        ) else "none (fully isolated)"

        log_path.parent.mkdir(parents=True, exist_ok=True)
        started = time.monotonic()
        captured: list[str] = []
        total_bytes = 0
        truncated = False
        timed_out = False
        cancelled = False

        # Docker's own environment: the daemon socket, nothing from our app.
        docker_env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": "/tmp",
        }
        if os.environ.get("DOCKER_HOST"):
            docker_env["DOCKER_HOST"] = os.environ["DOCKER_HOST"]

        try:
            popen = subprocess.Popen(
                argv,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                env=docker_env,
                start_new_session=True,
            )
        except OSError as exc:
            return StepResult(
                exit_code=None,
                stdout="",
                duration_ms=0,
                timed_out=False,
                cancelled=False,
                truncated=False,
                log_bytes=0,
                network=network,
                container_name=container_name,
                error=f"cannot start container: {exc}",
            )

        with self._lock:
            self._active[run_id] = _Process(popen, container_name)

        def _watchdog() -> None:
            nonlocal timed_out, cancelled
            deadline = started + timeout_s
            while popen.poll() is None:
                now = time.monotonic()
                if now >= deadline:
                    timed_out = True
                    self._kill_container(container_name)
                    return
                if cancel_check is not None:
                    try:
                        if cancel_check():
                            cancelled = True
                            self._kill_container(container_name)
                            return
                    except Exception:  # pragma: no cover - defensive
                        pass
                time.sleep(0.4)

        watchdog = threading.Thread(target=_watchdog, daemon=True)
        watchdog.start()

        with log_path.open("w", encoding="utf-8") as log_file:
            header = (
                f"$ {command.display}\n"
                f"# phase={command.phase.value} network={network} "
                f"workdir={command.workdir} timeout={timeout_s}s\n"
            )
            log_file.write(header)
            captured.append(header)
            assert popen.stdout is not None
            for line in popen.stdout:
                if truncated:
                    # Cap already hit: keep draining so the attach stream does
                    # not block the container's termination, but discard.
                    continue
                remaining = self.limits.max_log_bytes - total_bytes
                if remaining <= 0:
                    truncated = True
                    self._kill_container_async(container_name)
                    continue
                line = redact(line)
                chunk = line[:remaining]
                total_bytes += len(chunk.encode("utf-8", errors="replace"))
                log_file.write(chunk)
                log_file.flush()
                captured.append(chunk)
                if on_output is not None:
                    try:
                        on_output(chunk)
                    except Exception:  # pragma: no cover - defensive
                        pass
            if truncated:
                note = (
                    f"\n[output truncated at {self.limits.max_log_bytes} bytes; "
                    "container terminated by ReproLab's log-size limit]\n"
                )
                log_file.write(note)
                captured.append(note)

        try:
            exit_code = popen.wait(timeout=30)
        except subprocess.TimeoutExpired:  # pragma: no cover - defensive
            self._kill_container(container_name)
            popen.kill()
            exit_code = popen.wait(timeout=10)
        watchdog.join(timeout=2)

        with self._lock:
            self._active.pop(run_id, None)

        duration_ms = int((time.monotonic() - started) * 1000)
        # A container killed from outside reports 137 (SIGKILL).
        oom = exit_code == 137 and not timed_out and not cancelled and not truncated
        if timed_out:
            note = f"\n[terminated by ReproLab: exceeded the {timeout_s}s step timeout]\n"
            captured.append(note)
            with log_path.open("a", encoding="utf-8") as log_file:
                log_file.write(note)
        if cancelled:
            note = "\n[terminated by ReproLab: cancelled by the user]\n"
            captured.append(note)
            with log_path.open("a", encoding="utf-8") as log_file:
                log_file.write(note)
        if oom:
            note = (
                f"\n[container exit 137: killed, most likely the "
                f"{self.limits.container_memory_mb} MB memory limit]\n"
            )
            captured.append(note)
            with log_path.open("a", encoding="utf-8") as log_file:
                log_file.write(note)

        return StepResult(
            exit_code=exit_code,
            stdout="".join(captured),
            duration_ms=duration_ms,
            timed_out=timed_out,
            cancelled=cancelled,
            truncated=truncated,
            log_bytes=total_bytes,
            network=network,
            container_name=container_name,
            oom_killed=oom,
        )

    # -- cancellation -------------------------------------------------------
    def _kill_container(self, container_name: str) -> None:
        """Best-effort SIGKILL of a container.

        Never raises and never blocks the caller for long: a container that is
        flooding stdout will not accept `docker kill` until its attach stream
        is drained, so a slow kill must not stall the reader.
        """
        try:
            subprocess.run(
                [self.docker, "kill", "--signal", "KILL", container_name],
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
        except (subprocess.SubprocessError, OSError):
            pass

    def _kill_container_async(self, container_name: str) -> None:
        threading.Thread(
            target=self._kill_container, args=(container_name,), daemon=True
        ).start()

    def cancel(self, run_id: str) -> bool:
        """Terminate whatever container this run is executing.

        Returns True if something was killed.
        """
        with self._lock:
            active = self._active.get(run_id)
        if active is None:
            return False
        self._kill_container(active.container_name)
        try:
            active.popen.wait(timeout=15)
        except subprocess.TimeoutExpired:  # pragma: no cover - defensive
            active.popen.kill()
        return True

    def cleanup_orphans(self) -> int:
        """Remove any containers left behind by a crash."""
        proc = subprocess.run(
            [self.docker, "ps", "-aq", "--filter", "name=reprolab-"],
            capture_output=True,
            text=True,
            timeout=60,
        )
        ids = [i for i in proc.stdout.split() if i]
        if ids:
            subprocess.run(
                [self.docker, "rm", "-f", *ids], capture_output=True, timeout=120
            )
        return len(ids)
