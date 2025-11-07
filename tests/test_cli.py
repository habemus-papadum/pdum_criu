"""CLI tests for pdum-criu."""

from __future__ import annotations

import re

import pytest
from typer.testing import CliRunner

from pdum.criu import __version__, cli

runner = CliRunner()

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def _strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text)


def test_version_command_displays_version() -> None:
    """Version command should print the package version."""
    result = runner.invoke(cli.app, ["version"])
    assert result.exit_code == 0
    plain = _strip_ansi(result.stdout)
    assert "pdum-criu" in plain
    assert __version__ in plain


def test_shell_group_missing_subcommand_shows_help() -> None:
    """Invoking shell without subcommand should show help text."""
    result = runner.invoke(cli.app, ["shell"])
    assert result.exit_code == 0
    assert "Usage" in result.stdout


@pytest.mark.parametrize("subcommand", ["freeze", "thaw", "beam"])
def test_shell_subcommands_help(subcommand: str) -> None:
    """Each shell subcommand should provide helpful usage output."""
    result = runner.invoke(cli.app, ["shell", subcommand, "--help"])
    assert result.exit_code == 0
    assert "Usage" in result.stdout


def test_doctor_requires_linux(monkeypatch) -> None:
    """Doctor should exit when run on non-Linux platforms."""
    monkeypatch.setattr(cli.sys, "platform", "darwin")
    result = runner.invoke(cli.app, ["doctor"])
    assert result.exit_code == 1
    assert "only supports Linux" in result.stdout


def test_doctor_success(monkeypatch) -> None:
    """Doctor should report passing checks when everything succeeds."""

    def _make_checker(value):
        def _checker(*, verbose):
            assert verbose is True
            return value
        return _checker

    monkeypatch.setattr(cli.utils, "ensure_sudo", _make_checker(True))
    monkeypatch.setattr(cli.utils, "ensure_criu", _make_checker("/usr/bin/criu"))
    monkeypatch.setattr(cli.utils, "ensure_pgrep", _make_checker("/usr/bin/pgrep"))
    monkeypatch.setattr(cli.utils, "check_sudo_closefrom", lambda: (True, None))

    result = runner.invoke(cli.app, ["doctor"])
    assert result.exit_code == 0
    assert "All doctor checks passed" in result.stdout
    assert "closefrom_override" in result.stdout


def test_doctor_failure(monkeypatch) -> None:
    """Doctor should flag failures when a checker returns falsy."""

    def _good_checker(*, verbose):
        assert verbose is True
        return "/usr/bin/tool"

    def _bad_checker(*, verbose):
        assert verbose is True
        return None

    monkeypatch.setattr(cli.utils, "ensure_sudo", _good_checker)
    monkeypatch.setattr(cli.utils, "ensure_criu", _bad_checker)
    monkeypatch.setattr(cli.utils, "ensure_pgrep", _good_checker)
    monkeypatch.setattr(cli.utils, "check_sudo_closefrom", lambda: (False, "closefrom missing"))

    result = runner.invoke(cli.app, ["doctor"])
    assert result.exit_code == 0
    assert "✗ CRIU" in result.stdout
    assert "✗ sudo closefrom_override" in result.stdout
    assert "closefrom missing" in result.stdout
    assert "Resolve the failed checks" in result.stdout
