#!/usr/bin/env python3
"""Minimal asynchronous goblin freeze/thaw demo."""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from pdum.criu import goblins

GOBLIN_PAYLOAD = r"""
import os
import sys

print(f"Goblin PID={os.getpid()} ready", flush=True)

for line in sys.stdin:
    text = line.rstrip("\n")
    if text == "":
        print(f"[{os.getpid()}] (noop)", flush=True)
        continue
    print(f"[{os.getpid()}] echo: {text}", flush=True)
    sys.stdout.flush()

print(f"[{os.getpid()}] stdin closed, exiting", flush=True)
"""


async def _write_line(writer: asyncio.StreamWriter, text: str) -> None:
    writer.write((text.rstrip("\n") + "\n").encode("utf-8"))
    await writer.drain()


async def _read_line(reader: asyncio.StreamReader, timeout: float = 5.0) -> str:
    try:
        line = await asyncio.wait_for(reader.readline(), timeout=timeout)
    except asyncio.TimeoutError as exc:  # pragma: no cover - best-effort demo
        raise TimeoutError("timed out waiting for goblin output") from exc
    return line.decode("utf-8", errors="replace").rstrip("\n")


async def _launch_goblin(python: str) -> asyncio.subprocess.Process:
    proc = await asyncio.create_subprocess_exec(
        python,
        "-u",
        "-c",
        GOBLIN_PAYLOAD,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=True,
    )
    assert proc.stdout is not None
    banner = await _read_line(proc.stdout)
    print(f"Original goblin says: {banner}")
    return proc


async def _wait_for_pidfile(pidfile: Path, timeout: float = 5.0) -> None:
    loop = asyncio.get_event_loop()
    deadline = loop.time() + timeout
    while True:
        try:
            int(pidfile.read_text().strip())
            return
        except (FileNotFoundError, ValueError):
            pass
        remaining = deadline - loop.time()
        if remaining <= 0:
            raise TimeoutError(f"timed out waiting for pidfile {pidfile}")
        await asyncio.sleep(min(0.05, remaining))


async def demo(images_dir: Path, python: str, cleanup: bool) -> None:
    print(f"Using images directory {images_dir}")
    images_dir.mkdir(parents=True, exist_ok=True)

    proc = await _launch_goblin(python)
    assert proc.stdin and proc.stdout and proc.stderr

    await _write_line(proc.stdin, "hello before freeze")
    print(f"Original response: {await _read_line(proc.stdout)}")

    await goblins.freeze_async(proc.pid, images_dir, leave_running=True, verbosity=4, shell_job=False)
    print(f"Goblin frozen into {images_dir}")

    thawed = await goblins.thaw_async(images_dir, shell_job=False, detach=True)
    await _wait_for_pidfile(thawed.pidfile)
    thawed_pid = await thawed.read_pidfile()
    print(f"Thawed goblin PID={thawed_pid} (original PID={proc.pid})")

    await _write_line(proc.stdin, "original still alive")
    print(f"Original response: {await _read_line(proc.stdout)}")

    await _write_line(thawed.stdin, "hello from thawed client")
    print(f"Thawed response: {await _read_line(thawed.stdout)}")

    await _write_line(proc.stdin, "orig second ping")
    await _write_line(thawed.stdin, "thawed second ping")
    print(f"Original second response: {await _read_line(proc.stdout)}")
    print(f"Thawed second response: {await _read_line(thawed.stdout)}")

    await _write_line(proc.stdin, "exit")
    await _write_line(thawed.stdin, "exit")
    await _close_writer(proc.stdin)
    await _close_writer(thawed.stdin)

    try:
        print(f"Original exit message: {await _read_line(proc.stdout, timeout=2)}")
    except TimeoutError:
        print("Original goblin did not exit on cue")

    try:
        print(f"Thawed exit message: {await _read_line(thawed.stdout, timeout=5)}")
    except TimeoutError:
        print("Thawed goblin did not exit on cue")

    await thawed.close()
    await proc.wait()
    print("Demo complete.")

    if cleanup:
        print(f"Removing {images_dir}")


async def _close_writer(writer: asyncio.StreamWriter) -> None:
    transport = writer.transport
    if transport and transport.can_write_eof():
        try:
            transport.write_eof()
        except (AttributeError, NotImplementedError):
            pass
    writer.close()
    wait_closed = getattr(writer, "wait_closed", None)
    if callable(wait_closed):
        try:
            await wait_closed()
        except (NotImplementedError, RuntimeError):
            pass


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Asynchronous goblin freeze/thaw sanity test.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--images-dir",
        type=Path,
        default=Path("/tmp/pdum-goblin-demo"),
        help="Directory to store CRIU images (default: /tmp/pdum-goblin-demo).",
    )
    parser.add_argument(
        "--python",
        default=sys.executable,
        help="Python interpreter used for the goblin payload.",
    )
    parser.add_argument(
        "--cleanup",
        action="store_true",
        help="Remove the images directory after the demo completes.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    images_dir = args.images_dir.expanduser().resolve()
    asyncio.run(demo(images_dir, args.python, args.cleanup))


if __name__ == "__main__":
    main()
