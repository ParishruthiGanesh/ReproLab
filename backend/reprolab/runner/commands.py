"""Command validation.

The model proposes commands as text. Nothing it proposes is ever handed to a
shell. Every command is parsed into an argv list, the program is checked
against an allowlist, and every argument is checked for shell metacharacters
and path escapes. Only commands that survive this, *and* are then approved by
a human, are executed -- and only inside a disposable container.
"""

from __future__ import annotations

import shlex
from dataclasses import dataclass
from enum import Enum

MAX_ARGV_LENGTH = 64
MAX_ARG_LENGTH = 512

# Characters that would be meaningful to a shell. We never invoke a shell, but
# refusing them means a quoted payload cannot become code if any downstream
# consumer ever did.
SHELL_METACHARACTERS = set(";|&$`\n\r<>(){}*?!\\\"'")

# Programs the runner will execute. Anything else is rejected outright.
ALLOWED_PROGRAMS = frozenset(
    {
        "python",
        "python3",
        "pip",
        "pip3",
        "pytest",
        "ls",
        "cat",
        "head",
        "tail",
        "wc",
        "find",
        "env",
        "git",
    }
)

# Subcommands allowed for programs that can otherwise mutate state or reach
# the network.
ALLOWED_GIT_SUBCOMMANDS = frozenset(
    {"rev-parse", "log", "status", "ls-files", "show", "describe", "diff", "config"}
)
ALLOWED_PIP_SUBCOMMANDS = frozenset({"install", "list", "show", "freeze", "download"})

# `python -m <module>`: modules that would subvert the sandbox are refused.
FORBIDDEN_PYTHON_MODULES = frozenset({"http.server", "socketserver", "ftplib", "venv"})

# Arguments that would break isolation or escalate privileges.
FORBIDDEN_ARG_TOKENS = frozenset(
    {
        "--trusted-host",
        "--extra-index-url",
        "--index-url",
        "-i",
        "--editable",
        "--pre",
        "--break-system-packages",
        "--no-build-isolation",
    }
)


class CommandPhase(str, Enum):
    """Which isolation profile a command runs under.

    ``SETUP`` gets outbound network so dependencies can be acquired.
    ``EXPERIMENT`` runs with ``--network none``.
    """

    SETUP = "setup"
    EXPERIMENT = "experiment"


class CommandNotAllowed(ValueError):
    """Raised when a proposed command fails validation."""


@dataclass(frozen=True)
class ValidatedCommand:
    """A command that passed validation. ``argv`` is what the runner executes."""

    argv: tuple[str, ...]
    phase: CommandPhase
    purpose: str
    workdir: str = "."

    @property
    def display(self) -> str:
        return shlex.join(self.argv)

    def as_dict(self) -> dict[str, object]:
        return {
            "argv": list(self.argv),
            "command": self.display,
            "phase": self.phase.value,
            "purpose": self.purpose,
            "workdir": self.workdir,
        }


def _check_arg(arg: str) -> None:
    if not isinstance(arg, str):
        raise CommandNotAllowed("command arguments must be strings")
    if len(arg) > MAX_ARG_LENGTH:
        raise CommandNotAllowed(f"argument longer than {MAX_ARG_LENGTH} characters")
    if "\x00" in arg:
        raise CommandNotAllowed("argument contains a NUL byte")
    bad = SHELL_METACHARACTERS.intersection(arg)
    if bad:
        raise CommandNotAllowed(
            "argument contains shell metacharacter(s) "
            + " ".join(sorted(repr(c) for c in bad))
        )
    if arg.startswith("/") and not arg.startswith("/tmp/"):
        raise CommandNotAllowed(
            f"absolute path {arg!r} is not allowed; use workspace-relative paths"
        )
    if ".." in arg.split("/"):
        raise CommandNotAllowed(f"'..' traversal in argument {arg!r} is not allowed")
    if arg.lower() in FORBIDDEN_ARG_TOKENS:
        raise CommandNotAllowed(f"argument {arg!r} is not allowed")


def _validate_program_specific(argv: list[str]) -> None:
    program = argv[0]
    rest = argv[1:]
    if program == "git":
        if not rest or rest[0] not in ALLOWED_GIT_SUBCOMMANDS:
            raise CommandNotAllowed(
                "only read-only git subcommands are allowed: "
                + ", ".join(sorted(ALLOWED_GIT_SUBCOMMANDS))
            )
    elif program in {"pip", "pip3"}:
        subcommands = [a for a in rest if not a.startswith("-")]
        if not subcommands or subcommands[0] not in ALLOWED_PIP_SUBCOMMANDS:
            raise CommandNotAllowed(
                "only these pip subcommands are allowed: "
                + ", ".join(sorted(ALLOWED_PIP_SUBCOMMANDS))
            )
    elif program in {"python", "python3"}:
        if "-c" in rest:
            raise CommandNotAllowed(
                "python -c inline code is not allowed; commands must reference a "
                "file in the repository or use 'python -m <module>'"
            )
        if "-m" in rest:
            index = rest.index("-m")
            if index + 1 >= len(rest):
                raise CommandNotAllowed("python -m requires a module name")
            module = rest[index + 1]
            if module in FORBIDDEN_PYTHON_MODULES:
                raise CommandNotAllowed(f"python module {module!r} is not allowed")
            if module in {"pip"}:
                tail = [a for a in rest[index + 2 :] if not a.startswith("-")]
                if not tail or tail[0] not in ALLOWED_PIP_SUBCOMMANDS:
                    raise CommandNotAllowed(
                        "only these pip subcommands are allowed: "
                        + ", ".join(sorted(ALLOWED_PIP_SUBCOMMANDS))
                    )


def parse_command(
    raw: str | list[str] | tuple[str, ...],
    *,
    phase: CommandPhase,
    purpose: str = "",
    workdir: str = ".",
) -> ValidatedCommand:
    """Validate an untrusted command proposal into a :class:`ValidatedCommand`.

    Raises :class:`CommandNotAllowed` for anything not provably safe.
    """
    if isinstance(raw, (list, tuple)):
        argv = [str(a) for a in raw]
        if not argv:
            raise CommandNotAllowed("command is empty")
    else:
        if not isinstance(raw, str) or not raw.strip():
            raise CommandNotAllowed("command is empty")
        text = raw.strip()
        # Reject shell constructs before tokenising, so the error names the
        # real problem rather than a quoting artefact.
        for token in ("&&", "||", ";", "|", "$(", "`", ">", "<", "\n"):
            if token in text:
                raise CommandNotAllowed(
                    f"shell construct {token!r} is not allowed; propose one "
                    "command per step"
                )
        try:
            argv = shlex.split(text)
        except ValueError as exc:
            raise CommandNotAllowed(f"cannot parse command: {exc}") from exc
        if not argv:
            raise CommandNotAllowed("command is empty")

    if len(argv) > MAX_ARGV_LENGTH:
        raise CommandNotAllowed(f"command has more than {MAX_ARGV_LENGTH} arguments")

    program = argv[0]
    if "/" in program:
        raise CommandNotAllowed(
            f"program must be a bare name on PATH, not {program!r}"
        )
    if program not in ALLOWED_PROGRAMS:
        raise CommandNotAllowed(
            f"program {program!r} is not allowed. Allowed programs: "
            + ", ".join(sorted(ALLOWED_PROGRAMS))
        )
    for arg in argv[1:]:
        _check_arg(arg)
    _validate_program_specific(argv)

    if not isinstance(workdir, str) or not workdir.strip():
        workdir = "."
    workdir = workdir.strip()
    if workdir.startswith("/") or ".." in workdir.split("/"):
        raise CommandNotAllowed(f"workdir {workdir!r} must stay inside the repository")

    return ValidatedCommand(
        argv=tuple(argv),
        phase=phase,
        purpose=(purpose or "").strip()[:300],
        workdir=workdir,
    )


def requires_network(command: ValidatedCommand) -> bool:
    """Whether the command's phase grants outbound network."""
    return command.phase is CommandPhase.SETUP


def internal_command(
    argv: list[str],
    *,
    phase: CommandPhase,
    purpose: str,
    workdir: str = ".",
) -> ValidatedCommand:
    """Build a command from ReproLab's own source, not from model output.

    Used for the fixed bootstrap steps (creating the run's virtualenv,
    recording the environment). The argv is a literal in this repository, so
    it does not pass through the untrusted-input allowlist -- but it is still
    executed in the same isolated container as everything else.
    """
    if not argv:
        raise CommandNotAllowed("internal command is empty")
    return ValidatedCommand(
        argv=tuple(str(a) for a in argv),
        phase=phase,
        purpose=purpose,
        workdir=workdir,
    )
