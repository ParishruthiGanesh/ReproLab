"""Runtime configuration, read once from the environment.

Every limit here is enforced by application code. The model is told about the
limits so its plans are realistic, but it is never trusted to respect them.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

_REPO_ROOT = Path(__file__).resolve().parents[2]


def _load_env() -> None:
    for candidate in (_REPO_ROOT / ".env", Path.cwd() / ".env"):
        if candidate.is_file():
            load_dotenv(candidate, override=False)


def _str(name: str, default: str = "") -> str:
    return (os.environ.get(name) or default).strip()


def _int(name: str, default: int) -> int:
    raw = _str(name)
    try:
        return int(raw) if raw else default
    except ValueError:
        return default


def _float(name: str, default: float) -> float:
    raw = _str(name)
    try:
        return float(raw) if raw else default
    except ValueError:
        return default


def _bool(name: str, default: bool = False) -> bool:
    raw = _str(name).lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Limits:
    """Hard execution limits. Enforced in application code."""

    max_runtime_seconds: int = 900
    step_timeout_seconds: int = 420
    container_cpus: float = 2.0
    container_memory_mb: int = 4096
    container_pids_limit: int = 256
    max_log_bytes: int = 1_048_576
    max_workspace_mb: int = 2048
    max_download_bytes: int = 20_971_520
    max_repairs: int = 2
    # Bounds on the agent loop itself.
    max_research_tool_calls: int = 10
    max_plan_commands: int = 8
    max_structured_retries: int = 2


@dataclass(frozen=True)
class Settings:
    nebius_api_key: str = ""
    nebius_base_url: str = "https://api.tokenfactory.nebius.com/v1"
    nebius_model: str = "nvidia/Nemotron-3_5-Lightning"
    nebius_model_autoselect: bool = True
    tavily_api_key: str = ""
    tavily_base_url: str = "https://api.tavily.com"

    runner_backend: str = "docker"
    runner_image: str = "python:3.11-slim"
    runner_ca_bundle: str = ""

    allow_arbitrary_repos: bool = False
    api_token: str = ""

    data_dir: Path = field(default_factory=lambda: _REPO_ROOT / "data")
    host: str = "127.0.0.1"
    port: int = 8000
    cors_origins: tuple[str, ...] = (
        "http://localhost:3000",
        "http://127.0.0.1:3000",
    )

    limits: Limits = field(default_factory=Limits)

    # -- derived paths -------------------------------------------------------
    @property
    def db_path(self) -> Path:
        return self.data_dir / "reprolab.sqlite3"

    @property
    def workspaces_dir(self) -> Path:
        return self.data_dir / "workspaces"

    @property
    def artifacts_dir(self) -> Path:
        return self.data_dir / "artifacts"

    # -- capability reporting ------------------------------------------------
    @property
    def has_nebius(self) -> bool:
        return bool(self.nebius_api_key) and not self.nebius_api_key.startswith(
            "replace-with"
        )

    @property
    def has_tavily(self) -> bool:
        return bool(self.tavily_api_key) and not self.tavily_api_key.startswith(
            "replace-with"
        )

    def ensure_dirs(self) -> None:
        for path in (self.data_dir, self.workspaces_dir, self.artifacts_dir):
            path.mkdir(parents=True, exist_ok=True)


def load_settings() -> Settings:
    _load_env()
    data_dir = Path(_str("REPROLAB_DATA_DIR", str(_REPO_ROOT / "data")))
    if not data_dir.is_absolute():
        data_dir = (_REPO_ROOT / data_dir).resolve()
    # localhost and 127.0.0.1 are distinct browser origins; allowing only one
    # breaks the UI depending on which the user typed.
    origins = tuple(
        o.strip()
        for o in _str(
            "REPROLAB_CORS_ORIGINS",
            "http://localhost:3000,http://127.0.0.1:3000",
        ).split(",")
        if o.strip()
    )
    limits = Limits(
        max_runtime_seconds=_int("REPROLAB_MAX_RUNTIME_SECONDS", 900),
        step_timeout_seconds=_int("REPROLAB_STEP_TIMEOUT_SECONDS", 420),
        container_cpus=_float("REPROLAB_CONTAINER_CPUS", 2.0),
        container_memory_mb=_int("REPROLAB_CONTAINER_MEMORY_MB", 4096),
        container_pids_limit=_int("REPROLAB_CONTAINER_PIDS_LIMIT", 256),
        max_log_bytes=_int("REPROLAB_MAX_LOG_BYTES", 1_048_576),
        max_workspace_mb=_int("REPROLAB_MAX_WORKSPACE_MB", 2048),
        max_download_bytes=_int("REPROLAB_MAX_DOWNLOAD_BYTES", 20_971_520),
        max_repairs=_int("REPROLAB_MAX_REPAIRS", 2),
    )
    base_url = _str("NEBIUS_BASE_URL", "https://api.tokenfactory.nebius.com/v1")
    return Settings(
        nebius_api_key=_str("NEBIUS_API_KEY"),
        nebius_base_url=base_url.rstrip("/"),
        nebius_model=_str("NEBIUS_MODEL", "nvidia/Nemotron-3_5-Lightning"),
        nebius_model_autoselect=_bool("NEBIUS_MODEL_AUTOSELECT", True),
        tavily_api_key=_str("TAVILY_API_KEY"),
        tavily_base_url=_str("TAVILY_BASE_URL", "https://api.tavily.com").rstrip("/"),
        runner_backend=_str("REPROLAB_RUNNER_BACKEND", "docker").lower(),
        runner_image=_str("REPROLAB_RUNNER_IMAGE", "python:3.11-slim"),
        runner_ca_bundle=_str("REPROLAB_RUNNER_CA_BUNDLE"),
        allow_arbitrary_repos=_bool("REPROLAB_ALLOW_ARBITRARY_REPOS", False),
        api_token=_str("REPROLAB_API_TOKEN"),
        data_dir=data_dir,
        host=_str("REPROLAB_HOST", "127.0.0.1"),
        port=_int("REPROLAB_PORT", 8000),
        cors_origins=origins,
        limits=limits,
    )


_settings: Settings | None = None


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = load_settings()
    return _settings


def reset_settings_cache() -> None:
    """Test helper: force the next ``get_settings()`` to re-read the env."""
    global _settings
    _settings = None
