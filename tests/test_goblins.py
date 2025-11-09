from __future__ import annotations

import asyncio
import io
import json
from pathlib import Path
from types import SimpleNamespace
from typing import cast

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

    def fake_run(cmd, check, **kwargs):
        recorded_command["cmd"] = cmd
        recorded_command["check"] = check
        return DummyRun(0)

    monkeypatch.setattr(goblins.subprocess, "run", fake_run)

    log_path = goblins.freeze(1234, tmp_path, leave_running=False, verbosity=2)

    assert log_path.name.startswith("goblin-freeze.1234")
    assert "/usr/bin/criu" in recorded_command["cmd"]
    assert "--leave-running" not in recorded_command["cmd"]
    assert "--shell-job" in recorded_command["cmd"]
    assert recorded_command["check"] is False

    meta_path = tmp_path / ".pdum_goblin_meta.json"
    assert meta_path.exists()
    meta = json.loads(meta_path.read_text())
    assert meta["pipe_ids"]["stdin"] == "pipe:[1]"


def test_freeze_without_shell_job(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(goblins.utils, "ensure_linux", lambda: None)
    monkeypatch.setattr(goblins.utils, "ensure_sudo", lambda **_: None)
    monkeypatch.setattr(goblins.utils, "ensure_criu", lambda **_: "/usr/bin/criu")
    monkeypatch.setattr(goblins.utils, "resolve_command", lambda name: f"/usr/bin/{name}")
    pipe_map = {"stdin": "pipe:[7]", "stdout": "pipe:[8]", "stderr": "pipe:[9]"}
    monkeypatch.setattr(goblins, "_collect_pipe_ids_from_proc", lambda *_: pipe_map)
    monkeypatch.setattr(goblins, "_pipe_ids_from_images", lambda *_: pipe_map)

    recorded = {}

    def fake_run(cmd, check, **kwargs):
        recorded["cmd"] = cmd
        return DummyRun(0)

    monkeypatch.setattr(goblins.subprocess, "run", fake_run)

    goblins.freeze(777, tmp_path, shell_job=False)

    assert "--shell-job" not in recorded["cmd"]


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

    async def fake_create_subprocess_exec(*cmd, **kwargs):  # pragma: no cover - simple helper
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
    monkeypatch.setattr(goblins.utils, "ensure_sudo_closefrom", lambda: True)

    called = {}

    class FakeProc:
        def __init__(self, pid: int):
            self.pid = pid
            self._returncode = None

        def poll(self):
            return self._returncode

        def wait(self, timeout=None):
            self._returncode = 0
            return 0

        def terminate(self):
            self._returncode = -15

        def kill(self):
            self._returncode = -9

    def fake_popen(cmd, pass_fds, **kwargs):
        called["cmd"] = cmd
        called["pass_fds"] = pass_fds
        pidfile = images_dir / "goblin-thaw.12345.pid"
        pidfile.write_text("7777")
        return FakeProc(pid=5555)

    monkeypatch.setattr(goblins.time, "time", lambda: 12345.0)
    monkeypatch.setattr(goblins.subprocess, "Popen", fake_popen)

    proc = goblins.thaw(images_dir)

    assert proc.helper_pid == 5555
    assert proc.log_path == images_dir / "goblin-thaw.12345.log"
    assert proc.pidfile == images_dir / "goblin-thaw.12345.pid"
    assert "--shell-job" in called["cmd"]
    assert any("--inherit-fd" in arg for arg in called["cmd"] if isinstance(arg, str))
    assert proc.read_pidfile() == 7777
    proc.stdin.close()
    proc.stdout.close()
    proc.stderr.close()


def test_thaw_detach_requires_no_shell_job(monkeypatch, tmp_path: Path) -> None:
    images_dir = tmp_path / "img"
    images_dir.mkdir()
    _write_meta(images_dir)

    monkeypatch.setattr(goblins.utils, "resolve_command", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(goblins.utils, "ensure_criu", lambda **_: "/usr/bin/criu")
    monkeypatch.setattr(goblins.utils, "ensure_criu_ns", lambda **_: "/usr/sbin/criu-ns")

    with pytest.raises(ValueError):
        goblins.thaw(images_dir, detach=True)


def test_thaw_detach_appends_flag(monkeypatch, tmp_path: Path) -> None:
    images_dir = tmp_path / "img"
    images_dir.mkdir()
    _write_meta(images_dir)

    monkeypatch.setattr(goblins.utils, "resolve_command", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(goblins.utils, "ensure_criu", lambda **_: "/usr/bin/criu")
    monkeypatch.setattr(goblins.utils, "ensure_criu_ns", lambda **_: "/usr/sbin/criu-ns")
    monkeypatch.setattr(goblins.utils, "ensure_sudo_closefrom", lambda: True)

    class FakeProc:
        def __init__(self):
            self.pid = 999

        def poll(self):
            return 0

        def wait(self, timeout=None):
            return 0

    captured = {}

    def fake_popen(cmd, pass_fds, **kwargs):
        captured["cmd"] = cmd
        (images_dir / "goblin-thaw.1.pid").write_text("1000")
        return FakeProc()

    monkeypatch.setattr(goblins.time, "time", lambda: 1.0)
    monkeypatch.setattr(goblins.subprocess, "Popen", fake_popen)

    goblins.thaw(images_dir, shell_job=False, detach=True)
    assert "-d" in captured["cmd"]


def test_thaw_without_shell_job(monkeypatch, tmp_path: Path) -> None:
    images_dir = tmp_path / "img2"
    images_dir.mkdir()
    _write_meta(images_dir)

    monkeypatch.setattr(goblins.utils, "resolve_command", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(goblins.utils, "ensure_criu", lambda **_: "/usr/bin/criu")
    monkeypatch.setattr(goblins.utils, "ensure_criu_ns", lambda **_: "/usr/sbin/criu-ns")
    monkeypatch.setattr(goblins.utils, "ensure_sudo_closefrom", lambda: True)
    pipe_map = {"stdin": "pipe:[21]", "stdout": "pipe:[22]", "stderr": "pipe:[23]"}
    monkeypatch.setattr(goblins, "_pipe_ids_from_images", lambda *_: pipe_map)

    class FakePipes:
        def __init__(self):
            self.child_stdio_fds = []
            self.inherit_args = []
            self.parent_stdin_fd = 3
            self.parent_stdout_fd = 4
            self.parent_stderr_fd = 5

        def close_parent_ends(self):
            pass

        def close_child_fds(self):
            pass

        def build_sync_streams(self):
            return io.BytesIO(), io.BytesIO(), io.BytesIO()

    monkeypatch.setattr(goblins, "_prepare_stdio_pipes", lambda *_: FakePipes())

    class FakeProc:
        def __init__(self):
            self.pid = 6000
            self._returncode = None

        def poll(self):
            return self._returncode

        def wait(self, timeout=None):
            self._returncode = 0
            return 0

        def terminate(self):
            self._returncode = -15

        def kill(self):
            self._returncode = -9

    captured = {}

    def fake_popen(cmd, pass_fds, **kwargs):
        captured["cmd"] = cmd
        (images_dir / "goblin-thaw.999.pid").write_text("6500")
        return FakeProc()

    monkeypatch.setattr(goblins.time, "time", lambda: 999.0)
    monkeypatch.setattr(goblins.subprocess, "Popen", fake_popen)

    proc = goblins.thaw(images_dir, shell_job=False)
    assert "--shell-job" not in captured["cmd"]
    assert proc.read_pidfile() == 6500


def test_thaw_allows_custom_log_and_pidfile(monkeypatch, tmp_path: Path) -> None:
    images_dir = tmp_path / "img"
    images_dir.mkdir()
    _write_meta(images_dir)

    log_override = tmp_path / "logs" / "restore.log"
    pid_override = tmp_path / "state" / "restore.pid"

    monkeypatch.setattr(goblins.utils, "resolve_command", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(goblins.utils, "ensure_criu", lambda **_: "/usr/bin/criu")
    monkeypatch.setattr(goblins.utils, "ensure_criu_ns", lambda **_: "/usr/sbin/criu-ns")
    monkeypatch.setattr(goblins.utils, "ensure_sudo_closefrom", lambda: True)

    class FakePipes:
        def __init__(self):
            self.child_stdio_fds = []
            self.inherit_args = []
            self.parent_stdin_fd = 3
            self.parent_stdout_fd = 4
            self.parent_stderr_fd = 5

        def close_parent_ends(self):
            pass

        def close_child_fds(self):
            pass

        def build_sync_streams(self):
            return io.BytesIO(), io.BytesIO(), io.BytesIO()

    monkeypatch.setattr(goblins, "_prepare_stdio_pipes", lambda *_: FakePipes())

    class FakeProc:
        def __init__(self):
            self.pid = 4321

        def poll(self):
            return 0

        def wait(self, timeout=None):
            return 0

    def fake_popen(cmd, pass_fds, **kwargs):
        pid_override.write_text("9001")
        return FakeProc()

    monkeypatch.setattr(goblins.subprocess, "Popen", fake_popen)

    proc = goblins.thaw(
        images_dir,
        shell_job=False,
        log_path=log_override,
        pidfile=pid_override,
    )
    assert proc.log_path == log_override.resolve()
    assert proc.pidfile == pid_override.resolve()
    assert proc.read_pidfile() == 9001


def test_thaw_async_success(monkeypatch, tmp_path: Path) -> None:
    images_dir = tmp_path / "img"
    images_dir.mkdir()
    _write_meta(images_dir)

    monkeypatch.setattr(goblins.utils, "resolve_command", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(goblins.utils, "ensure_criu", lambda **_: "/usr/bin/criu")
    monkeypatch.setattr(goblins.utils, "ensure_criu_ns", lambda **_: "/usr/sbin/criu-ns")
    monkeypatch.setattr(goblins.time, "time", lambda: 54321.0)
    monkeypatch.setattr(goblins.utils, "ensure_sudo_closefrom", lambda: True)
    (images_dir / "goblin-thaw.54321.pid").write_text("6666")

    class FakePipes:
        def __init__(self):
            self.child_stdio_fds = []
            self.inherit_args = []
            self.parent_stdin_fd = 3
            self.parent_stdout_fd = 4
            self.parent_stderr_fd = 5

        def close_parent_ends(self):
            pass

        def close_child_fds(self):
            pass

    monkeypatch.setattr(goblins, "_prepare_stdio_pipes", lambda *_: FakePipes())

    class FakeAsyncProc:
        def __init__(self):
            self.pid = 4242
            self.returncode = None

        def terminate(self):
            self.returncode = -15

        def kill(self):
            self.returncode = -9

        async def wait(self):
            self.returncode = 0
            return 0

    async def fake_launch(context, pipes):
        return FakeAsyncProc()

    monkeypatch.setattr(goblins, "_launch_criu_restore_async", fake_launch)

    async def fake_writer(fd):
        return f"writer-{fd}"

    async def fake_reader(fd):
        return f"reader-{fd}"

    monkeypatch.setattr(goblins, "_make_writer_from_fd", fake_writer)
    monkeypatch.setattr(goblins, "_make_reader_from_fd", fake_reader)

    async def _exercise():
        proc = await goblins.thaw_async(images_dir)
        assert proc.helper_pid == 4242
        assert proc.stdin.startswith("writer-")
        assert proc.pidfile == images_dir / "goblin-thaw.54321.pid"
        assert await proc.read_pidfile() == 6666

    asyncio.run(_exercise())


@pytest.mark.asyncio
async def test_async_goblin_process_read_pidfile(tmp_path: Path) -> None:
    pidfile = tmp_path / "pid"
    pidfile.write_text("4242\n", encoding="utf-8")
    proc = goblins.AsyncGoblinProcess(
        helper_pid=111,
        stdin=cast(asyncio.StreamWriter, object()),
        stdout=cast(asyncio.StreamReader, object()),
        stderr=cast(asyncio.StreamReader, object()),
        images_dir=tmp_path,
        log_path=tmp_path / "log",
        pidfile=pidfile,
    )
    assert await proc.read_pidfile() == 4242


@pytest.mark.asyncio
async def test_read_file_async_reads_entire_file(tmp_path: Path) -> None:
    payload = b"\x00hello goblin\xffmore-bytes"
    blob = tmp_path / "blob.bin"
    blob.write_bytes(payload)
    data = await goblins._read_file_async(blob, chunk_size=4)
    assert data == payload
