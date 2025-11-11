#!/usr/bin/env python3
"""Reproduce the freeze → thaw → freeze failure mode for goblins."""

from __future__ import annotations

import argparse
import select
import shutil
import subprocess
import sys
import time
from pathlib import Path

from pdum.criu import goblins

STATEFUL_GOBLIN = r"""
import os
import sys

counter = 0
print(f"[{os.getpid()}] counter goblin ready", flush=True)

for line in sys.stdin:
    payload = line.rstrip("\n")
    counter += 1
    print(f"[{os.getpid()}] counter={counter} payload={payload!r}", flush=True)

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


def _launch_goblin(python: str) -> subprocess.Popen[bytes]:
    proc = subprocess.Popen(
        [python, "-u", "-c", STATEFUL_GOBLIN],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    if proc.stdin is None or proc.stdout is None or proc.stderr is None:
        raise RuntimeError("failed to capture goblin stdio")
    banner = _read_line(proc.stdout)
    print(f"Spawned goblin PID={proc.pid}: {banner}")
    return proc


def _wait_for_pidfile(pidfile: Path, timeout: float = 5.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            int(pidfile.read_text().strip())
            return
        except (FileNotFoundError, ValueError):
            time.sleep(0.05)
    raise TimeoutError(f"timed out waiting for pidfile {pidfile}")


def _tail(path: Path, lines: int = 15) -> list[str]:
    try:
        content = path.read_text(errors="replace").splitlines()
    except FileNotFoundError:
        return []
    return content[-lines:]


def demo(images_root: Path, python: str, cleanup: bool) -> None:
    print(f"Using base images directory {images_root}")
    first_dump = images_root / "first-freeze"
    second_dump = images_root / "second-freeze"
    for directory in (first_dump, second_dump):
        directory.mkdir(parents=True, exist_ok=True)

    proc = _launch_goblin(python)
    assert proc.stdin and proc.stdout
    thawed: goblins.GoblinProcess | None = None
    try:
        _write_line(proc.stdin, "ping-1")
        print(f"Original response: {_read_line(proc.stdout)}")

        print("Freezing original goblin...")
        first_log = goblins.freeze(proc.pid, first_dump, leave_running=True, verbosity=4, shell_job=False)
        print(f"Initial freeze complete (log {first_log})")

        print("Thawing cloned goblin...")
        thawed = goblins.thaw(first_dump, shell_job=False, detach=True)
        _wait_for_pidfile(thawed.pidfile)
        thawed_pid = thawed.read_pidfile()
        print(f"Thawed goblin PID={thawed_pid} helper PID={thawed.helper_pid}")
        _write_line(thawed.stdin, "thaw-ping")
        print(f"Thawed response: {_read_line(thawed.stdout)}")

        _write_line(proc.stdin, "ping-2")
        print(f"Original response: {_read_line(proc.stdout)}")

        print("Attempting to freeze the restored goblin (expected to fail)...")
        second_log = second_dump / f"second-freeze.{thawed_pid}.log"
        try:
            goblins.freeze(
                thawed_pid,
                second_dump,
                leave_running=True,
                verbosity=4,
                log_path=second_log,
                shell_job=False,
            )
        except RuntimeError as exc:
            print(f"Second freeze failed as expected: {exc}")
            tail = _tail(second_log)
            if tail:
                print(f"Tail of {second_log}:")
                for line in tail:
                    print(f"    {line}")
        else:
            print("Unexpected success: the thawed goblin was frozen again.")
            sys.exit(1)
    finally:
        print("Shutting down goblins...")
        if proc.stdin and not proc.stdin.closed:
            proc.stdin.close()
        if thawed is not None:
            if thawed.stdin and not thawed.stdin.closed:
                thawed.stdin.close()
            thawed.close()
            if thawed.helper_pid is not None:
                print(f"Waiting for helper PID {thawed.helper_pid} to exit")
        try:
            proc.terminate()
        except OSError:
            pass
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()

        if cleanup:
            print(f"Removing {images_root}")
            shutil.rmtree(images_root, ignore_errors=True)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Demonstrate freeze -> thaw -> freeze failure for counter goblins.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--images-dir",
        type=Path,
        default=Path("/tmp/pdum-freeze-thaw-freeze"),
        help="Base directory for CRIU image sets.",
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
    demo(args.images_dir.expanduser().resolve(), args.python, args.cleanup)


if __name__ == "__main__":
    main()
