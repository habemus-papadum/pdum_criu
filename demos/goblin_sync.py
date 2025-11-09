#!/usr/bin/env python3
"""Minimal synchronous goblin freeze/thaw demo."""

from __future__ import annotations

import argparse
import os
import select
import subprocess
import sys
import time
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


def _write_line(writer, text: str) -> None:
    data = (text.rstrip("\n") + "\n").encode("utf-8")
    writer.write(data)
    writer.flush()


def _read_line(reader, *, timeout: float = 5.0) -> str:
    fd = reader.fileno()
    deadline = time.time() + timeout
    while True:
        remaining = deadline - time.time()
        if remaining <= 0:
            raise TimeoutError("timed out waiting for goblin output")
        ready, _, _ = select.select([fd], [], [], remaining)
        if not ready:
            continue
        line = reader.readline()
        if not line:
            return ""
        return line.decode("utf-8", errors="replace").rstrip("\n")


def _drain(reader) -> None:
    if reader.closed:
        return
    fd = reader.fileno()
    while True:
        ready, _, _ = select.select([fd], [], [], 0)
        if not ready:
            break
        chunk = reader.readline()
        if not chunk:
            break
        print(f"[stderr] {chunk.decode('utf-8', errors='replace').rstrip()}")


def _launch_goblin(python: str) -> subprocess.Popen[bytes]:
    proc = subprocess.Popen(
        [python, "-u", "-c", GOBLIN_PAYLOAD],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    if proc.stdout is None or proc.stdin is None or proc.stderr is None:
        raise RuntimeError("failed to capture goblin stdio pipes")
    banner = _read_line(proc.stdout)
    print(f"Original goblin says: {banner}")
    return proc


def _wait_for_pid(pid: int, timeout: float) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            os.kill(pid, 0)
        except OSError:
            print(f"Process {pid} exited")
            return
    time.sleep(0.1)
    print(f"Process {pid} still running after {timeout:.1f}s")


def _wait_for_pidfile(pidfile: Path, timeout: float = 5.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            int(pidfile.read_text().strip())
            return
        except (FileNotFoundError, ValueError):
            time.sleep(0.02)
    raise TimeoutError(f"timed out waiting for pidfile {pidfile}")


def demo(images_dir: Path, python: str, cleanup: bool) -> None:
    print(f"Using images directory {images_dir}")
    images_dir.mkdir(parents=True, exist_ok=True)

    proc = _launch_goblin(python)
    assert proc.stdin and proc.stdout

    _write_line(proc.stdin, "hello before freeze")
    print(f"Original response: {_read_line(proc.stdout)}")

    log_path = goblins.freeze(proc.pid, images_dir, leave_running=True, verbosity=4, shell_job=False)
    print(f"Goblin frozen into {images_dir} (log {log_path})")

    thawed = goblins.thaw(images_dir, shell_job=False, detach=True)
    _wait_for_pidfile(thawed.pidfile)
    thawed_pid = thawed.read_pidfile()
    print(f"Thawed goblin PID={thawed_pid} (original PID={proc.pid}, restore helper PID={thawed.helper_pid})")

    _write_line(proc.stdin, "original still alive")
    print(f"Original response: {_read_line(proc.stdout)}")

    _write_line(thawed.stdin, "hello from thawed client")
    print(f"Thawed response: {_read_line(thawed.stdout)}")

    _write_line(proc.stdin, "orig second ping")
    _write_line(thawed.stdin, "thawed second ping")
    print(f"Original second response: {_read_line(proc.stdout)}")
    print(f"Thawed second response: {_read_line(thawed.stdout)}")

    proc.stdin.close()
    thawed.stdin.close()

    try:
        print(f"Original exit message: {_read_line(proc.stdout, timeout=2)}")
    except TimeoutError:
        print("Original goblin did not exit on cue")
    try:
        print(f"Thawed exit message: {_read_line(thawed.stdout, timeout=2)}")
    except TimeoutError:
        print("Thawed goblin did not exit on cue")

    thawed.close()
    if thawed.helper_pid is not None:
        print(f"Waiting for criu-ns helper PID {thawed.helper_pid} to exit")
        _wait_for_pid(thawed.helper_pid, timeout=5)
    proc.wait(timeout=5)
    _drain(proc.stderr)
    print("Demo complete.")

    if cleanup:
        print(f"Removing {images_dir}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Synchronous goblin freeze/thaw sanity test.",
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
    cleanup = args.cleanup
    demo(images_dir, args.python, cleanup)


if __name__ == "__main__":
    main()
