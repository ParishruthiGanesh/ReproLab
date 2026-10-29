"""Disposable per-run workspaces.

A workspace holds exactly one shallow clone of one repository plus whatever
the run writes. It is created fresh, size-capped, and deleted when the run is
pruned. Nothing outside the workspace is ever written.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from ..security.paths import PathNotAllowed, directory_size_bytes, resolve_in_workspace
from ..security.urls import normalize_github_repo

# Files worth showing the agent when it inspects a repository.
INTERESTING_FILENAMES = (
    "README.md",
    "README.rst",
    "README.txt",
    "readme.md",
    "requirements.txt",
    "requirements-dev.txt",
    "pyproject.toml",
    "setup.py",
    "setup.cfg",
    "environment.yml",
    "environment.yaml",
    "Makefile",
    "LICENSE",
    "LICENSE.txt",
    "CITATION.cff",
    "Dockerfile",
    "run.sh",
    "train.py",
    "eval.py",
    "evaluate.py",
    "main.py",
    "demo.py",
    "example.py",
    "inference.py",
    "benchmark.py",
)

SKIP_DIRS = frozenset(
    {
        ".git",
        ".github",
        "__pycache__",
        ".venv",
        "venv",
        "node_modules",
        ".pytest_cache",
        ".mypy_cache",
        ".idea",
        ".home",
        ".local",
        ".ipynb_checkpoints",
    }
)

MAX_FILE_READ_BYTES = 120_000
MAX_TREE_ENTRIES = 400


class WorkspaceError(RuntimeError):
    """Raised when a workspace cannot be prepared."""


@dataclass
class CloneResult:
    path: Path
    commit: str
    ref: str
    size_bytes: int
    default_branch: str


class Workspace:
    """A disposable directory containing one repository clone."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)

    # -- lifecycle ----------------------------------------------------------
    @classmethod
    def create(cls, base_dir: Path, run_id: str) -> "Workspace":
        root = Path(base_dir) / run_id
        if root.exists():
            shutil.rmtree(root, ignore_errors=True)
        root.mkdir(parents=True)
        # Writable by the container's non-root uid.
        (root / ".home").mkdir(exist_ok=True)
        (root / "outputs").mkdir(exist_ok=True)
        os.chmod(root, 0o777)
        os.chmod(root / ".home", 0o777)
        os.chmod(root / "outputs", 0o777)
        return cls(root)

    def destroy(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)

    @property
    def repo_dir(self) -> Path:
        return self.root / "repo"

    @property
    def outputs_dir(self) -> Path:
        return self.root / "outputs"

    def size_bytes(self, limit_bytes: int | None = None) -> int:
        return directory_size_bytes(self.root, limit_bytes=limit_bytes)

    # -- cloning ------------------------------------------------------------
    def clone(
        self,
        repo_url: str,
        *,
        ref: str | None = None,
        max_bytes: int,
        timeout: int = 300,
    ) -> CloneResult:
        """Shallow-clone a public GitHub repository into the workspace.

        Cloning fetches data; it never runs repository code. Hooks are
        disabled, submodules are not fetched, and the result is size-checked.
        """
        _owner, _repo, canonical = normalize_github_repo(repo_url)
        target = self.repo_dir
        if target.exists():
            shutil.rmtree(target, ignore_errors=True)

        cmd = [
            "git",
            "-c", "core.hooksPath=/dev/null",
            "-c", "protocol.ext.allow=never",
            "-c", "protocol.file.allow=never",
            "-c", "advice.detachedHead=false",
            "clone",
            "--depth", "1",
            "--no-tags",
            "--single-branch",
            "--config", "submodule.recurse=false",
        ]
        if ref:
            cmd += ["--branch", ref]
        cmd += [canonical, str(target)]

        env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": str(self.root / ".home"),
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_ASKPASS": "/bin/true",
            "GIT_CONFIG_NOSYSTEM": "1",
            # Preserve TLS trust in proxied environments.
            "GIT_SSL_CAINFO": os.environ.get("GIT_SSL_CAINFO", ""),
            "https_proxy": os.environ.get("https_proxy", ""),
            "http_proxy": os.environ.get("http_proxy", ""),
            "no_proxy": os.environ.get("no_proxy", ""),
        }
        env = {k: v for k, v in env.items() if v}

        try:
            proc = subprocess.run(
                cmd, capture_output=True, text=True, timeout=timeout, env=env
            )
        except subprocess.TimeoutExpired as exc:
            shutil.rmtree(target, ignore_errors=True)
            raise WorkspaceError(f"git clone timed out after {timeout}s") from exc
        if proc.returncode != 0:
            shutil.rmtree(target, ignore_errors=True)
            detail = (proc.stderr or proc.stdout or "").strip()[-600:]
            raise WorkspaceError(f"git clone failed: {detail}")

        size = directory_size_bytes(target, limit_bytes=max_bytes)
        if size > max_bytes:
            shutil.rmtree(target, ignore_errors=True)
            raise WorkspaceError(
                f"repository is larger than the {max_bytes // (1024 * 1024)} MB limit "
                f"({size // (1024 * 1024)} MB fetched before the check aborted)"
            )

        commit = self._git_output(target, ["rev-parse", "HEAD"]) or "unknown"
        branch = (
            self._git_output(target, ["rev-parse", "--abbrev-ref", "HEAD"]) or "unknown"
        )
        # Let the container's non-root uid write inside the clone.
        for path in [target, *target.rglob("*")]:
            try:
                if path.is_symlink():
                    continue
                os.chmod(path, 0o777 if path.is_dir() else 0o666)
            except OSError:
                continue
        return CloneResult(
            path=target,
            commit=commit,
            ref=ref or branch,
            size_bytes=size,
            default_branch=branch,
        )

    @staticmethod
    def _git_output(repo: Path, args: list[str]) -> str:
        try:
            proc = subprocess.run(
                ["git", "-C", str(repo), *args],
                capture_output=True,
                text=True,
                timeout=30,
                env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": "/tmp"},
            )
        except (subprocess.SubprocessError, OSError):
            return ""
        return proc.stdout.strip() if proc.returncode == 0 else ""

    def git_diff(self) -> str:
        """Diff of agent modifications against the cloned commit."""
        return self._git_output(self.repo_dir, ["diff"])

    def has_modifications(self) -> bool:
        status = self._git_output(self.repo_dir, ["status", "--porcelain"])
        return bool(status.strip())

    # -- inspection ---------------------------------------------------------
    def tree(self, max_entries: int = MAX_TREE_ENTRIES) -> list[str]:
        """Repository-relative paths, directories first, noise filtered out."""
        if not self.repo_dir.is_dir():
            return []
        entries: list[str] = []
        for current, dirnames, filenames in os.walk(self.repo_dir, followlinks=False):
            dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS)
            rel_dir = Path(current).relative_to(self.repo_dir)
            depth = len(rel_dir.parts)
            if depth > 3:
                dirnames[:] = []
                continue
            for filename in sorted(filenames):
                rel = (rel_dir / filename).as_posix()
                entries.append(rel)
                if len(entries) >= max_entries:
                    return entries
        return entries

    def read_file(self, relative: str, max_bytes: int = MAX_FILE_READ_BYTES) -> str:
        """Read a repository file, refusing anything outside the workspace."""
        target = resolve_in_workspace(self.repo_dir, relative)
        if not target.is_file():
            raise PathNotAllowed(f"{relative!r} is not a file in the repository")
        data = target.read_bytes()[:max_bytes]
        text = data.decode("utf-8", errors="replace")
        if target.stat().st_size > max_bytes:
            text += f"\n\n[truncated at {max_bytes} bytes]"
        return text

    def interesting_files(self) -> list[str]:
        present = []
        for name in INTERESTING_FILENAMES:
            candidate = self.repo_dir / name
            if candidate.is_file():
                present.append(name)
        return present

    def collect_artifacts(self, dest: Path, max_bytes: int) -> list[dict[str, object]]:
        """Copy files the run produced in ``outputs/`` into ``dest``."""
        dest.mkdir(parents=True, exist_ok=True)
        collected: list[dict[str, object]] = []
        total = 0
        if not self.outputs_dir.is_dir():
            return collected
        for path in sorted(self.outputs_dir.rglob("*")):
            if not path.is_file() or path.is_symlink():
                continue
            size = path.stat().st_size
            if total + size > max_bytes:
                collected.append(
                    {
                        "name": path.relative_to(self.outputs_dir).as_posix(),
                        "bytes": size,
                        "collected": False,
                        "reason": "artifact size budget exhausted",
                    }
                )
                continue
            rel = path.relative_to(self.outputs_dir)
            out = dest / rel
            out.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, out)
            total += size
            collected.append(
                {"name": rel.as_posix(), "bytes": size, "collected": True, "reason": ""}
            )
        return collected
