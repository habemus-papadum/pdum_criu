"""Live async CRIU tests."""

from __future__ import annotations

import asyncio
import os
import signal
import time
from pathlib import Path

import pytest

from pdum.criu import goblins

from .test_live_criu import (
    _images_dir,
    _read_log_tail_as_root,
    _require_live_prereqs,
    _spawn_echo_goblin,
    _spawn_goblin,
    _terminate,
)


async def _async_read_line(reader, timeout: float = 5.0) -> bytes:
    line = await asyncio.wait_for(reader.readline(), timeout=timeout)
    if not line:
        raise AssertionError("unexpected EOF while waiting for goblin output")
    return line


async def _async_write_line(writer, data: bytes) -> None:
    writer.write(data)
    await writer.drain()


async def _wait_for_pidfile(pidfile: Path, timeout: float = 5.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if pidfile.exists():
            return
        await asyncio.sleep(0.05)
    raise AssertionError(f"pidfile {pidfile} was not created within {timeout}s")


@pytest.mark.asyncio
async def test_goblin_freeze_async_live(tmp_path: Path) -> None:
    _require_live_prereqs()

    proc = _spawn_goblin()
    with _images_dir(tmp_path, "freeze-async") as images_dir:
        try:
            try:
                log_path = await goblins.freeze_async(proc.pid, images_dir, leave_running=False)
            except RuntimeError as exc:
                log_tail = _read_log_tail_as_root(images_dir / f"goblin-freeze.{proc.pid}.log")
                pytest.skip(f"CRIU freeze failed in this environment: {exc}\nLog tail:\n{log_tail}")

            assert log_path.exists()
            assert any(images_dir.iterdir())

            proc.wait(timeout=5)
        finally:
            _terminate(proc)


@pytest.mark.asyncio
async def test_goblin_thaw_async_live(tmp_path: Path) -> None:
    _require_live_prereqs()

    proc = _spawn_echo_goblin()
    with _images_dir(tmp_path, "thaw-async") as images_dir:
        try:
            try:
                await asyncio.wait_for(
                    goblins.freeze_async(proc.pid, images_dir, leave_running=False, shell_job=False),
                    timeout=10,
                )
            except RuntimeError as exc:
                log_tail = _read_log_tail_as_root(images_dir / f"goblin-freeze.{proc.pid}.log")
                pytest.skip(f"CRIU freeze failed in this environment: {exc}\nLog tail:\n{log_tail}")

            proc.wait(timeout=5)

            try:
                thawed = await asyncio.wait_for(
                    goblins.thaw_async(images_dir, shell_job=False, detach=True),
                    timeout=10,
                )
            except RuntimeError as exc:
                if "closefrom_override" in str(exc):
                    pytest.skip(str(exc))
                raise

            message = b"ping from thaw\n"
            await _async_write_line(thawed.stdin, message)
            response = await _async_read_line(thawed.stdout)
            assert message.strip().upper() in response.strip().upper()

            await _wait_for_pidfile(thawed.pidfile)
            os.kill(await thawed.read_pidfile(), signal.SIGTERM)
            await asyncio.wait_for(thawed.close(), timeout=5)
        finally:
            _terminate(proc)
