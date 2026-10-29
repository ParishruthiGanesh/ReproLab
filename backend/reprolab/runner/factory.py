"""Runner selection.

Docker is the only backend. When it is unavailable ReproLab reports the run as
blocked and explains the minimum setup required, rather than executing
untrusted repository code on the host.
"""

from __future__ import annotations

from ..config import Settings
from .docker_runner import DockerRunner, RunnerUnavailable

SUPPORTED_BACKENDS = ("docker",)


def build_runner(settings: Settings) -> DockerRunner:
    backend = (settings.runner_backend or "docker").lower()
    if backend not in SUPPORTED_BACKENDS:
        raise RunnerUnavailable(
            f"REPROLAB_RUNNER_BACKEND={backend!r} is not supported. ReproLab only "
            "executes repository code in Docker containers; it will not run "
            "untrusted code on the host. Set REPROLAB_RUNNER_BACKEND=docker."
        )
    return DockerRunner(
        image=settings.runner_image,
        limits=settings.limits,
        ca_bundle=settings.runner_ca_bundle,
    )
