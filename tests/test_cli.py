"""CLI tests for pdum-criu."""

from __future__ import annotations

import pytest
from typer.testing import CliRunner

from pdum.criu import __version__, cli

runner = CliRunner()


def test_version_command_displays_version() -> None:
    """Version command should print the package version."""
    result = runner.invoke(cli.app, ["version"])
    assert result.exit_code == 0
    assert "pdum-criu" in result.stdout
    assert __version__ in result.stdout


def test_shell_group_missing_subcommand_shows_help() -> None:
    """Invoking shell without subcommand should show help text."""
    result = runner.invoke(cli.app, ["shell"])
    assert result.exit_code == 0
    assert "Usage" in result.stdout


@pytest.mark.parametrize(
    ("subcommand", "expected_snippet"),
    [
        ("freeze", "Freeze action"),
        ("thaw", "Thaw action"),
        ("beam", "Beam action"),
    ],
)
def test_shell_subcommands_print_placeholders(subcommand: str, expected_snippet: str) -> None:
    """Each placeholder command should emit its informative message."""
    result = runner.invoke(cli.app, ["shell", subcommand])
    assert result.exit_code == 0
    assert expected_snippet in result.stdout


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

    result = runner.invoke(cli.app, ["doctor"])
    assert result.exit_code == 0
    assert "All doctor checks passed" in result.stdout


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

    result = runner.invoke(cli.app, ["doctor"])
    assert result.exit_code == 0
    assert "✗ CRIU" in result.stdout
    assert "Resolve the failed checks" in result.stdout
