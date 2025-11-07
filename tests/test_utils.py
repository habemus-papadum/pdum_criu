"""Tests for executable resolution helpers."""

from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from pdum.criu import utils


def _make_executable(directory: Path, name: str, contents: str) -> Path:
    path = directory / name
    path.write_text(contents, encoding="utf-8")
    path.chmod(0o755)
    return path


def test_resolve_command_returns_absolute_path() -> None:
    """Default resolution should yield an absolute, executable path."""
    path = utils.resolve_command("true")
    assert os.path.isabs(path)
    assert os.access(path, os.X_OK)


def test_resolve_command_honors_env_override(monkeypatch, tmp_path: Path) -> None:
    """Environment overrides should win over PATH lookups."""
    custom_exe = tmp_path / "custom-true"
    custom_exe.write_text("#!/usr/bin/env sh\nexit 0\n", encoding="utf-8")
    custom_exe.chmod(0o755)

    monkeypatch.setenv("PDUM_CRIU_TRUE", os.fspath(custom_exe))
    assert utils.resolve_command("true") == os.fspath(custom_exe)


def test_resolve_command_supports_hyphenated_names(monkeypatch, tmp_path: Path) -> None:
    """Hyphenated executable names should map to the sanitized env var."""
    custom_exe = tmp_path / "criu-ns"
    custom_exe.write_text("#!/usr/bin/env sh\nexit 0\n", encoding="utf-8")
    custom_exe.chmod(0o755)

    monkeypatch.setenv("PDUM_CRIU_CRIU_NS", os.fspath(custom_exe))
    assert utils.resolve_command("criu-ns") == os.fspath(custom_exe)


def test_resolve_command_missing_binary() -> None:
    """Missing executables should surface as FileNotFoundError."""
    with pytest.raises(FileNotFoundError):
        utils.resolve_command("pdum-criu-definitely-missing")


def test_resolve_command_empty_value() -> None:
    """Empty executable names should be rejected."""
    with pytest.raises(ValueError):
        utils.resolve_command("")


def test_ensure_sudo_success(monkeypatch) -> None:
    """Sudo check should pass when subprocess reports success."""

    monkeypatch.setattr(utils, "resolve_command", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(
        utils.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(returncode=0),
    )
    printed: list[str] = []
    monkeypatch.setattr(utils, "rich_print", lambda message: printed.append(message))

    assert utils.ensure_sudo()
    assert printed == []


def test_ensure_sudo_failure_prints_message(monkeypatch) -> None:
    """Non-zero sudo exit codes should emit a helpful message."""

    monkeypatch.setattr(utils, "resolve_command", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(
        utils.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(returncode=1),
    )
    printed: list[str] = []
    monkeypatch.setattr(utils, "rich_print", lambda message: printed.append(message))

    assert not utils.ensure_sudo()
    assert any("Password-less sudo" in msg for msg in printed)


def test_ensure_sudo_missing_command(monkeypatch) -> None:
    """Missing sudo binaries should emit an informative error."""

    def _raise(_name: str) -> str:
        raise FileNotFoundError("missing sudo")

    monkeypatch.setattr(utils, "resolve_command", _raise)
    printed: list[str] = []
    monkeypatch.setattr(utils, "rich_print", lambda message: printed.append(message))

    assert not utils.ensure_sudo()
    assert any("Unable to locate sudo" in msg for msg in printed)


@pytest.mark.parametrize(
    ("func_name", "executable"),
    [
        ("ensure_criu", "criu"),
        ("ensure_criu_ns", "criu-ns"),
        ("ensure_pgrep", "pgrep"),
    ],
)
def test_ensure_tools_success(monkeypatch, func_name: str, executable: str) -> None:
    """Successful ensure helpers should return the resolved path."""

    expected_path = f"/opt/{executable}"
    monkeypatch.setattr(utils, "resolve_command", lambda name: expected_path if name == executable else name)
    printed: list[str] = []
    monkeypatch.setattr(utils, "rich_print", lambda message: printed.append(message))

    ensure_fn = getattr(utils, func_name)
    assert ensure_fn() == expected_path
    assert printed == []


@pytest.mark.parametrize(
    ("func_name", "expected_snippet"),
    [
        ("ensure_criu", "apt update && sudo apt install -y criu"),
        ("ensure_criu_ns", "sudo apt install -y criu"),
        ("ensure_pgrep", "sudo apt install -y procps"),
    ],
)
def test_ensure_tools_failure(monkeypatch, func_name: str, expected_snippet: str) -> None:
    """Ensure helpers should guide users toward Ubuntu install instructions."""

    def _raise(_name: str) -> str:
        raise FileNotFoundError("missing binary")

    monkeypatch.setattr(utils, "resolve_command", _raise)
    printed: list[str] = []
    monkeypatch.setattr(utils, "rich_print", lambda message: printed.append(message))

    ensure_fn = getattr(utils, func_name)
    assert ensure_fn() is None
    assert any(expected_snippet in msg for msg in printed)
