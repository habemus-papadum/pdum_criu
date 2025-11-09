#!/usr/bin/env python3
"""Minimal synchronous goblin freeze/thaw demo."""

from __future__ import annotations

import argparse
import logging
import os
import select
import shutil
import subprocess
import sys
import tempfile
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
    if text == "exit":
        print(f"[{os.getpid()}] exiting", flush=True)
        break
    print(f"[{os.getpid()}] echo: {text}", flush=True)
    sys.stdout.flush()

print(f"[{os.getpid()}] bye", flush=True)
"""


def _configure_logging() -> None:
    logging.basicConfig(
        level=logging.DEBUG,
        format="%(asctime)s %(levelname)-8s %(name)s %(message)s",
    )
    logging.getLogger("pdum").setLevel(logging.DEBUG)
    logging.getLogger("pdum.criu").setLevel(logging.DEBUG)


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
        logging.debug("stderr: %s", chunk.decode("utf-8", errors="replace").rstrip("\n"))


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
    logging.info("Original goblin says: %s", banner)
    return proc


def demo(images_dir: Path, python: str, cleanup: bool) -> None:
    logging.info("Using images directory %s", images_dir)
    images_dir.mkdir(parents=True, exist_ok=True)

    proc = _launch_goblin(python)
    assert proc.stdin and proc.stdout

    _write_line(proc.stdin, "hello before freeze")
    logging.info("Original response: %s", _read_line(proc.stdout))

    log_path = goblins.freeze(proc.pid, images_dir, leave_running=True, verbosity=4)
    logging.info("Goblin frozen into %s (log %s)", images_dir, log_path)

    thawed = goblins.thaw(images_dir)
    logging.info("Thawed goblin PID=%s (original PID=%s)", thawed.pid, proc.pid)

    _write_line(proc.stdin, "original still alive")
    logging.info("Original response: %s", _read_line(proc.stdout))

    _write_line(thawed.stdin, "hello from thawed client")
    logging.info("Thawed response: %s", _read_line(thawed.stdout))

    _write_line(proc.stdin, "orig second ping")
    _write_line(thawed.stdin, "thawed second ping")
    logging.info("Original second response: %s", _read_line(proc.stdout))
    logging.info("Thawed second response: %s", _read_line(thawed.stdout))

    _write_line(proc.stdin, "exit")
    _write_line(thawed.stdin, "exit")
    try:
        logging.info("Original exit message: %s", _read_line(proc.stdout, timeout=2))
    except TimeoutError:
        logging.warning("Original goblin did not exit on cue")
    try:
        logging.info("Thawed exit message: %s", _read_line(thawed.stdout, timeout=2))
    except TimeoutError:
        logging.warning("Thawed goblin did not exit on cue")

    thawed.close()
    proc.wait(timeout=5)
    _drain(proc.stderr)
    logging.info("Demo complete.")

    if cleanup:
        logging.info("Removing %s", images_dir)
        shutil.rmtree(images_dir, ignore_errors=True)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Synchronous goblin freeze/thaw sanity test.")
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
    _configure_logging()
    args = parse_args()
    images_dir = args.images_dir.expanduser().resolve()
    cleanup = args.cleanup
    demo(images_dir, args.python, cleanup)


if __name__ == "__main__":
    main()
