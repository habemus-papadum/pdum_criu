"""Live async CRIU tests."""

from __future__ import annotations

import os
import signal
from pathlib import Path

import pytest

from pdum.criu import goblins

from .test_live_criu import (
    _read_log_tail_as_root,
    _require_live_prereqs,
    _spawn_echo_goblin,
    _spawn_goblin,
    _terminate,
)


@pytest.mark.asyncio
async def test_goblin_freeze_async_live(tmp_path: Path) -> None:
    _require_live_prereqs()

    proc = _spawn_goblin()
    images_dir = tmp_path / "goblin-live-async"

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
    images_dir = tmp_path / "goblin-thaw-live"

    try:
        try:
            await goblins.freeze_async(proc.pid, images_dir, leave_running=False)
        except RuntimeError as exc:
            log_tail = _read_log_tail_as_root(images_dir / f"goblin-freeze.{proc.pid}.log")
            pytest.skip(f"CRIU freeze failed in this environment: {exc}\nLog tail:\n{log_tail}")

        proc.wait(timeout=5)

        try:
            thawed = await goblins.thaw_async(images_dir)
        except RuntimeError as exc:
            if "closefrom_override" in str(exc):
                pytest.skip(str(exc))
            raise

        ready = await thawed.stdout.readline()
        assert b"ready" in ready.lower()

        message = b"ping from thaw\n"
        thawed.stdin.write(message)
        await thawed.stdin.drain()
        response = await thawed.stdout.readline()
        assert message.strip().upper() in response.strip().upper()

        os.kill(thawed.pid, signal.SIGTERM)
        await thawed.close()
    finally:
        _terminate(proc)
