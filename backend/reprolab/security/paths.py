"""Workspace path validation.

Repository contents, model-proposed file reads and model-proposed patch
destinations are all untrusted. Every path that reaches the filesystem is
resolved and proven to stay inside the run's disposable workspace.
"""

from __future__ import annotations

import os
from pathlib import Path

MAX_PATH_LENGTH = 512

# Writing to any of these would change how later commands behave.
FORBIDDEN_BASENAMES = frozenset(
    {
        ".git",
        ".gitmodules",
        ".gitattributes",
        ".bashrc",
        ".bash_profile",
        ".profile",
        ".netrc",
        "_netrc",
        ".npmrc",
        ".pypirc",
        ".ssh",
        "authorized_keys",
        "sitecustomize.py",
        "usercustomize.py",
        "conftest.py",
    }
)

FORBIDDEN_SUFFIXES = frozenset({".pth", ".so", ".dylib", ".dll"})


class PathNotAllowed(ValueError):
    """Raised when a path escapes the workspace or targets a sensitive file."""


def resolve_in_workspace(workspace: Path, relative: str) -> Path:
    """Resolve ``relative`` inside ``workspace``, rejecting any escape.

    Rejects absolute paths, ``..`` traversal, symlinks that leave the
    workspace, and NUL bytes.
    """
    if not isinstance(relative, str) or not relative.strip():
        raise PathNotAllowed("path must be a non-empty string")
    if "\x00" in relative:
        raise PathNotAllowed("path contains a NUL byte")
    if len(relative) > MAX_PATH_LENGTH:
        raise PathNotAllowed(f"path is longer than {MAX_PATH_LENGTH} characters")

    candidate = relative.strip().replace("\\", "/")
    if candidate.startswith("/") or candidate.startswith("~"):
        raise PathNotAllowed("absolute and home-relative paths are not allowed")
    # Windows-style drive letters, e.g. "C:/windows".
    if len(candidate) >= 2 and candidate[1] == ":":
        raise PathNotAllowed("drive-qualified paths are not allowed")

    parts = [p for p in candidate.split("/") if p not in ("", ".")]
    if any(p == ".." for p in parts):
        raise PathNotAllowed("'..' path traversal is not allowed")
    if not parts:
        raise PathNotAllowed("path resolves to the workspace root")

    workspace_root = workspace.resolve()
    target = (workspace_root / Path(*parts)).resolve()
    try:
        target.relative_to(workspace_root)
    except ValueError as exc:
        raise PathNotAllowed(
            f"path {relative!r} resolves outside the workspace"
        ) from exc

    # A symlink inside the repo could point out of the workspace; check each
    # existing ancestor component.
    probe = workspace_root
    for part in parts:
        probe = probe / part
        if probe.is_symlink():
            resolved = probe.resolve()
            try:
                resolved.relative_to(workspace_root)
            except ValueError as exc:
                raise PathNotAllowed(
                    f"path {relative!r} crosses a symlink leaving the workspace"
                ) from exc
    return target


def validate_patch_target(workspace: Path, relative: str) -> Path:
    """Resolve a patch destination, additionally refusing sensitive files."""
    target = resolve_in_workspace(workspace, relative)
    name = target.name
    if name in FORBIDDEN_BASENAMES:
        raise PathNotAllowed(f"patching {name!r} is not allowed")
    if target.suffix.lower() in FORBIDDEN_SUFFIXES:
        raise PathNotAllowed(f"patching {target.suffix!r} files is not allowed")
    for parent in target.parents:
        if parent.name in FORBIDDEN_BASENAMES:
            raise PathNotAllowed(f"patching inside {parent.name!r} is not allowed")
        if parent == workspace.resolve():
            break
    return target


def directory_size_bytes(path: Path, *, limit_bytes: int | None = None) -> int:
    """Sum the on-disk size of ``path``, stopping early once ``limit_bytes`` is
    exceeded. Symlinks are not followed, so a link cannot inflate the total."""
    total = 0
    for root, dirnames, filenames in os.walk(path, followlinks=False):
        for filename in filenames:
            full = Path(root) / filename
            try:
                if full.is_symlink():
                    continue
                total += full.stat().st_size
            except OSError:
                continue
            if limit_bytes is not None and total > limit_bytes:
                return total
    return total
