"""Tests for executable resolution helpers."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

from pdum.criu import utils


def _make_executable(directory: Path, name: str, contents: str) -> Path:
    path = directory / name
    path.write_text(contents, encoding="utf-8")
    path.chmod(0o755)
    return path


def _spawn_marker_process(label: str) -> subprocess.Popen[bytes]:
    script = (
        "import time\n"
        f"label = '{label}'\n"
        "time.sleep(30)\n"
    )
    return subprocess.Popen([sys.executable, "-u", "-c", script], stdout=subprocess.PIPE, stderr=subprocess.PIPE)


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
    warnings: list[str] = []
    monkeypatch.setattr(utils, "logger", SimpleNamespace(warning=lambda msg: warnings.append(msg)))

    assert utils.ensure_sudo()
    assert warnings == []


def test_ensure_sudo_failure_prints_message(monkeypatch) -> None:
    """Non-zero sudo exit codes should emit a helpful message."""

    monkeypatch.setattr(utils, "resolve_command", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(
        utils.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(returncode=1),
    )
    warnings: list[str] = []
    monkeypatch.setattr(utils, "logger", SimpleNamespace(warning=lambda msg: warnings.append(msg)))

    assert not utils.ensure_sudo()
    assert any("Password-less sudo" in msg for msg in warnings)


def test_ensure_sudo_missing_command(monkeypatch) -> None:
    """Missing sudo binaries should emit an informative error."""

    def _raise(_name: str) -> str:
        raise FileNotFoundError("missing sudo")

    monkeypatch.setattr(utils, "resolve_command", _raise)
    warnings: list[str] = []
    monkeypatch.setattr(utils, "logger", SimpleNamespace(warning=lambda msg: warnings.append(msg)))

    assert not utils.ensure_sudo()
    assert any("Unable to locate sudo" in msg for msg in warnings)


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
    warnings: list[str] = []
    monkeypatch.setattr(utils, "logger", SimpleNamespace(warning=lambda msg: warnings.append(msg)))

    ensure_fn = getattr(utils, func_name)
    assert ensure_fn() == expected_path
    assert warnings == []


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
    warnings: list[str] = []
    monkeypatch.setattr(utils, "logger", SimpleNamespace(warning=lambda msg: warnings.append(msg)))

    ensure_fn = getattr(utils, func_name)
    assert ensure_fn() is None
    assert any(expected_snippet in msg for msg in warnings)


def _configure_fake_pgrep(monkeypatch, stdout: str, returncode: int = 0) -> None:
    def fake_ensure_pgrep(**kwargs):
        assert kwargs == {"verbose": False, "raise_": True}
        return "/usr/bin/pgrep"

    monkeypatch.setattr(utils, "ensure_pgrep", fake_ensure_pgrep)
    monkeypatch.setattr(
        utils.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=returncode,
            stdout=stdout,
            stderr="",
        ),
    )


def test_psgrep_unique(monkeypatch) -> None:
    """psgrep should return a single PID when ensure_unique is True."""
    _configure_fake_pgrep(monkeypatch, stdout="123\n")
    pid = utils.psgrep("foo bar")
    assert pid == 123


def test_psgrep_multiple_matches_raises(monkeypatch) -> None:
    """Multiple matches should raise when ensure_unique is True."""
    _configure_fake_pgrep(monkeypatch, stdout="123\n456\n")
    with pytest.raises(RuntimeError):
        utils.psgrep("foo")


def test_psgrep_multiple_matches_allowed(monkeypatch) -> None:
    """Setting ensure_unique=False should return all matching PIDs."""
    _configure_fake_pgrep(monkeypatch, stdout="123\n456\n")
    pids = utils.psgrep("foo", ensure_unique=False)
    assert pids == [123, 456]


def test_psgrep_no_matches(monkeypatch) -> None:
    """pgrep returning no matches should raise a runtime error."""
    _configure_fake_pgrep(monkeypatch, stdout="", returncode=1)
    with pytest.raises(RuntimeError):
        utils.psgrep("foo")


def test_psgrep_rejects_empty_query() -> None:
    """Empty queries should be rejected early."""
    with pytest.raises(ValueError):
        utils.psgrep("")


@pytest.mark.skipif(shutil.which("pgrep") is None, reason="pgrep not installed")
def test_psgrep_real_process(tmp_path: Path) -> None:
    label = f"psgrep-{uuid.uuid4().hex}"
    proc = _spawn_marker_process(label)
    try:
        pid = utils.psgrep(label)
        assert pid == proc.pid
        resolved = utils.resolve_target_pid(None, label)
        assert resolved == proc.pid
    finally:
        proc.terminate()
        proc.wait(timeout=2)


def test_tail_file_roundtrip(tmp_path: Path) -> None:
    log_path = tmp_path / "tail.log"
    log_path.write_text("one\ntwo\nthree\n", encoding="utf-8")
    assert utils.tail_file(log_path, lines=2) == "two\nthree"
    assert utils.tail_file(tmp_path / "missing.log") == ""


def test_spawn_directory_cleanup(tmp_path: Path) -> None:
    target = tmp_path / "artifact"
    target.mkdir()
    watcher = subprocess.Popen([sys.executable, "-u", "-c", "import time; time.sleep(0.5)"])
    utils.spawn_directory_cleanup(target, watcher.pid)
    watcher.wait(timeout=2)
    deadline = time.time() + 5
    while time.time() < deadline and target.exists():
        time.sleep(0.1)
    assert not target.exists()


def _reset_sudo_closefrom_cache() -> None:
    utils._SUDO_CLOSEFROM_SUPPORTED = None
    utils._SUDO_CLOSEFROM_ERROR = None


def test_ensure_sudo_closefrom_with_override_success(monkeypatch, tmp_path: Path) -> None:
    script = _make_executable(tmp_path, "sudo-success.sh", "#!/usr/bin/env bash\nexit 0\n")
    monkeypatch.setenv("PDUM_CRIU_SUDO", os.fspath(script))
    _reset_sudo_closefrom_cache()
    utils.ensure_sudo_closefrom()
    assert utils._SUDO_CLOSEFROM_SUPPORTED is True


def test_ensure_sudo_closefrom_with_override_failure(monkeypatch, tmp_path: Path) -> None:
    script = _make_executable(tmp_path, "sudo-fail.sh", "#!/usr/bin/env bash\necho 'denied' >&2\nexit 1\n")
    monkeypatch.setenv("PDUM_CRIU_SUDO", os.fspath(script))
    _reset_sudo_closefrom_cache()
    with pytest.raises(RuntimeError):
        utils.ensure_sudo_closefrom()
