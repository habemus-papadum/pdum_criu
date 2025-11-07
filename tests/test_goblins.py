from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from pdum.criu import goblins


class DummyRun(SimpleNamespace):
    def __init__(self, returncode: int = 0):  # pragma: no cover - simple helper
        super().__init__(returncode=returncode)


def test_freeze_success(monkeypatch, tmp_path: Path) -> None:
    called = {}

    monkeypatch.setattr(goblins.utils, "ensure_linux", lambda: called.setdefault("linux", True))
    monkeypatch.setattr(goblins.utils, "ensure_sudo", lambda **_: called.setdefault("sudo", True))
    monkeypatch.setattr(goblins.utils, "ensure_criu", lambda **_: "/usr/bin/criu")
    monkeypatch.setattr(goblins.utils, "resolve_command", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(goblins.utils, "tail_file", lambda *_args, **_kwargs: "")

    pipe_map = {"stdin": "pipe:[1]", "stdout": "pipe:[2]", "stderr": "pipe:[3]"}
    monkeypatch.setattr(goblins, "_pipe_ids_from_images", lambda *_: pipe_map)
    monkeypatch.setattr(goblins, "_collect_pipe_ids_from_proc", lambda *_: pipe_map)

    recorded_command = {}

    def fake_run(cmd, check):
        recorded_command["cmd"] = cmd
        recorded_command["check"] = check
        return DummyRun(0)

    monkeypatch.setattr(goblins.subprocess, "run", fake_run)

    log_path = goblins.freeze(1234, tmp_path, leave_running=False, verbosity=2)

    assert log_path.name.startswith("goblin-freeze.1234")
    assert "/usr/bin/criu" in recorded_command["cmd"]
    assert "--leave-running" not in recorded_command["cmd"]
    assert recorded_command["check"] is False

    meta_path = tmp_path / ".pdum_goblin_meta.json"
    assert meta_path.exists()
    meta = json.loads(meta_path.read_text())
    assert meta["pipe_ids"]["stdin"] == "pipe:[1]"


def test_freeze_failure_raises(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(goblins.utils, "ensure_linux", lambda: None)
    monkeypatch.setattr(goblins.utils, "ensure_sudo", lambda **_: None)
    monkeypatch.setattr(goblins.utils, "ensure_criu", lambda **_: "/usr/bin/criu")
    monkeypatch.setattr(goblins.utils, "resolve_command", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(goblins.utils, "tail_file", lambda *_args, **_kwargs: "boom")
    monkeypatch.setattr(goblins.subprocess, "run", lambda *_, **__: DummyRun(1))

    with pytest.raises(RuntimeError):
        goblins.freeze(2222, tmp_path)


def test_freeze_async_success(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(goblins.utils, "ensure_linux", lambda: None)
    monkeypatch.setattr(goblins.utils, "ensure_sudo", lambda **_: None)
    monkeypatch.setattr(goblins.utils, "ensure_criu", lambda **_: "/usr/bin/criu")
    monkeypatch.setattr(goblins.utils, "resolve_command", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(goblins.utils, "tail_file", lambda *_args, **_kwargs: "")

    async def fake_create_subprocess_exec(*cmd):  # pragma: no cover - simple helper
        class _Proc:
            async def wait(self) -> int:
                return 0

        nonlocal recorded
        recorded = list(cmd)
        return _Proc()

    pipe_map = {"stdin": "pipe:[4]", "stdout": "pipe:[5]", "stderr": "pipe:[6]"}
    monkeypatch.setattr(goblins, "_pipe_ids_from_images", lambda *_: pipe_map)
    monkeypatch.setattr(goblins, "_collect_pipe_ids_from_proc", lambda *_: pipe_map)

    recorded: list[str] = []
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)

    log_path = asyncio.run(goblins.freeze_async(4444, tmp_path, leave_running=True))
    assert log_path.name.startswith("goblin-freeze.4444")
    assert recorded[:2] == ["/usr/bin/sudo", "-n"]

    meta_path = tmp_path / ".pdum_goblin_meta.json"
    assert meta_path.exists()


def _write_meta(images_dir: Path, pipe_ids: dict[str, str] | None = None) -> None:
    meta = {"pipe_ids": pipe_ids or {"stdin": "pipe:[11]", "stdout": "pipe:[12]", "stderr": "pipe:[13]"}}
    (images_dir / ".pdum_goblin_meta.json").write_text(json.dumps(meta))


def test_thaw_success(monkeypatch, tmp_path: Path) -> None:
    images_dir = tmp_path / "img"
    images_dir.mkdir()
    _write_meta(images_dir)

    monkeypatch.setattr(goblins.utils, "resolve_command", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(goblins.utils, "ensure_criu", lambda **_: "/usr/bin/criu")
    monkeypatch.setattr(goblins.utils, "ensure_criu_ns", lambda **_: "/usr/sbin/criu-ns")
    pipe_map = {"stdin": "pipe:[11]", "stdout": "pipe:[12]", "stderr": "pipe:[13]"}
    monkeypatch.setattr(goblins, "_pipe_ids_from_images", lambda *_: pipe_map)
    monkeypatch.setattr(goblins, "_ensure_sudo_closefrom_supported", lambda *_: None)

    called = {}

    def fake_run(cmd, check, pass_fds):
        called["cmd"] = cmd
        called["pass_fds"] = pass_fds
        pidfile = images_dir / "goblin-thaw.12345.pid"
        pidfile.write_text("7777")
        return DummyRun(0)

    monkeypatch.setattr(goblins.time, "time", lambda: 12345.0)
    monkeypatch.setattr(goblins.subprocess, "run", fake_run)

    proc = goblins.thaw(images_dir)

    assert proc.pid == 7777
    assert any("--inherit-fd" in arg for arg in called["cmd"] if isinstance(arg, str))
    proc.stdin.close()
    proc.stdout.close()
    proc.stderr.close()


def test_thaw_async_success(monkeypatch, tmp_path: Path) -> None:
    images_dir = tmp_path / "img"
    images_dir.mkdir()
    _write_meta(images_dir)

    monkeypatch.setattr(goblins.utils, "resolve_command", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(goblins.utils, "ensure_criu", lambda **_: "/usr/bin/criu")
    monkeypatch.setattr(goblins.utils, "ensure_criu_ns", lambda **_: "/usr/sbin/criu-ns")
    monkeypatch.setattr(goblins.time, "time", lambda: 54321.0)
    monkeypatch.setattr(goblins, "_ensure_sudo_closefrom_supported", lambda *_: None)

    def fake_run(cmd, check, pass_fds):
        (images_dir / "goblin-thaw.54321.pid").write_text("6666")
        return DummyRun(0)

    monkeypatch.setattr(goblins.subprocess, "run", fake_run)

    async def fake_writer(fd):
        os.close(fd)
        return f"writer-{fd}"

    async def fake_reader(fd):
        os.close(fd)
        return f"reader-{fd}"

    monkeypatch.setattr(goblins, "_make_writer_from_fd", fake_writer)
    monkeypatch.setattr(goblins, "_make_reader_from_fd", fake_reader)

    proc = asyncio.run(goblins.thaw_async(images_dir))
    assert proc.pid == 6666
    assert proc.stdin.startswith("writer-")
