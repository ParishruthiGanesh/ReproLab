"""Shared test fixtures."""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from reprolab.config import Limits, Settings  # noqa: E402
from reprolab.db import Database  # noqa: E402

FIXTURE_REPO = Path(__file__).parent / "fixtures" / "fixture_repo"


def docker_available() -> bool:
    if shutil.which("docker") is None:
        return False
    try:
        proc = subprocess.run(
            ["docker", "info", "--format", "{{.ServerVersion}}"],
            capture_output=True,
            timeout=30,
        )
    except (subprocess.SubprocessError, OSError):
        return False
    return proc.returncode == 0


requires_docker = pytest.mark.skipif(
    not docker_available(),
    reason="Docker daemon is not available; isolated runner tests cannot run",
)


@pytest.fixture
def tmp_settings(tmp_path: Path) -> Settings:
    data_dir = tmp_path / "data"
    settings = Settings(
        nebius_api_key="",
        tavily_api_key="",
        data_dir=data_dir,
        runner_image="python:3.11-slim",
        limits=Limits(
            max_runtime_seconds=120,
            step_timeout_seconds=60,
            container_cpus=1.0,
            container_memory_mb=1024,
            max_log_bytes=65536,
            max_workspace_mb=256,
            max_repairs=2,
        ),
    )
    settings.ensure_dirs()
    return settings


@pytest.fixture
def db(tmp_settings: Settings) -> Database:
    return Database(tmp_settings.db_path)
