#!/usr/bin/env python3
"""Launch, freeze, and thaw a goblin to measure restore latency."""

from __future__ import annotations

import select
import subprocess
import sys
import time
from pathlib import Path

from pdum.criu import goblins

EXECUTABLE = ["lake", "env", str(Path("/home/nehal/src/lean4-llm/blog/repl/.lake/build/bin/repl"))]
WORKDIR = Path("/home/nehal/src/lean4-llm/blog/repl/test/Mathlib")
IMAGES_DIR = Path("/tmp/time-demo-image")
PAYLOAD_TEXT = '{"cmd": "def f := 37"}\n\n'
PRIME_COMMAND = '{"cmd": "import Mathlib\nopen BigOperators\nopen Real\nopen Nat"}\n\n'
READ_TIMEOUT = 10.0
PRIME_TIMEOUT = 5.0


def _write_line(writer, text: str) -> None:
    """Write a UTF-8 line to the goblin stdin."""

    data = (text.rstrip("\n") + "\n\n").encode("utf-8")
    writer.write(data)
    writer.flush()


def _read_line(reader, *, timeout: float) -> str:
    """Return one decoded line from stdout with a timeout.

    Parameters
    ----------
    reader :
        Binary stdout stream returned by :func:`goblins.thaw`.
    timeout : float
        Seconds to wait for an incoming line.

    Returns
    -------
    str
        Line contents without the trailing newline. Empty string indicates
        the goblin emitted a blank line.

    Raises
    ------
    TimeoutError
        If no stdout data arrives before ``timeout`` expires.
    RuntimeError
        If the stdout pipe closes before emitting a blank line.
    """

    fd = reader.fileno()
    deadline = time.time() + timeout
    while True:
        remaining = deadline - time.time()
        if remaining <= 0:
            raise TimeoutError("timed out waiting for goblin output")
        ready, _, _ = select.select([fd], [], [], remaining)
        if not ready:
            continue
        chunk = reader.readline()
        print(f"Read {len(chunk)} bytes from goblin stdout")
        if not chunk:
            raise RuntimeError("goblin stdout closed before an empty line was seen")
        return chunk.decode("utf-8", errors="replace").rstrip("\n")


def _launch_process(executable: Path, workdir: Path) -> subprocess.Popen[bytes]:
    """Launch the target process with pipe-based stdio."""

    
    print(f"Launching {EXECUTABLE} (cwd={workdir})")
    proc = subprocess.Popen(
        EXECUTABLE,
        cwd=str(workdir),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    if proc.stdin is None or proc.stdout is None or proc.stderr is None:
        raise RuntimeError("failed to capture stdio pipes for the target process")
    return proc


def _prime_process(proc: subprocess.Popen[bytes], command: str) -> str:
    """Send a command to the process and drain stdout until a blank line."""

    print(f"Priming target with: {command!r}")
    _write_line(proc.stdin, command)
    lines: list[str] = []
    while True:
        line = _read_line(proc.stdout, timeout=PRIME_TIMEOUT)
        print(f"Prime response: {line}")
        if line == "":
            break
        lines.append(line)
    return "\n".join(lines)


def _freeze_process(proc: subprocess.Popen[bytes]) -> Path:
    """Freeze the launched process into the requested images directory."""

    print(f"Freezing PID {proc.pid} into {IMAGES_DIR}")
    log_path = goblins.freeze(
        proc.pid,
        IMAGES_DIR,
        leave_running=False,
        shell_job=True,
    )
    print(f"Freeze complete (log: {log_path})")
    return log_path


def _cleanup_process(proc: subprocess.Popen[bytes]) -> None:
    """Best-effort termination of the original process."""

    for stream in (proc.stdin, proc.stdout, proc.stderr):
        if stream is None:
            continue
        try:
            stream.close()
        except Exception:
            pass
    try:
        proc.terminate()
    except OSError:
        return
    try:
        proc.wait(timeout=5)
    except Exception:
        pass


def prepare_images(
    executable: Path,
    workdir: Path,
    command: str,
) -> tuple[Path, float, float]:
    """Launch the target, issue a command, and freeze its state."""

    start_launch = time.perf_counter()
    proc = _launch_process(executable, workdir)
    try:
        _prime_process(proc, command)
        prime_done = time.perf_counter()
        launch_prime_elapsed = prime_done - start_launch

        freeze_start = time.perf_counter()
        log_path = _freeze_process(proc)
        freeze_elapsed = time.perf_counter() - freeze_start
        return log_path, launch_prime_elapsed, freeze_elapsed
    finally:
        _cleanup_process(proc)


def measure_thaw(images_dir: Path, message: str, *, timeout: float) -> float:
    """Thaw a goblin, send a message, and time until a blank line appears.

    Parameters
    ----------
    images_dir : Path
        Directory containing the CRIU image set to restore.
    message : str
        Payload written to the goblin's stdin after thaw.
    timeout : float
        Seconds to wait for each stdout line before declaring a failure.

    Returns
    -------
    float
        Seconds elapsed between invoking :func:`goblins.thaw` and observing
        the terminating blank line on stdout.

    Raises
    ------
    TimeoutError
        If the goblin does not emit a line within ``timeout`` seconds.
    RuntimeError
        If CRIU stdio pipes close unexpectedly.
    """

    start = time.perf_counter()
    goblin = goblins.thaw(images_dir, shell_job=True)
    try:
        print(f"Sending thaw payload: {message!r}")
        _write_line(goblin.stdin, message)
        while True:
            line = _read_line(goblin.stdout, timeout=timeout)
            print(line)
            if line == "":
                break
    finally:
        goblin.close()
    return time.perf_counter() - start


def main() -> None:
    images_dir = IMAGES_DIR
    executable = EXECUTABLE
    workdir = WORKDIR.expanduser().resolve()

    try:
        images_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        print(f"error: failed to create images directory: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc

    print(f"Preparing goblin checkpoint in {images_dir}")
    try:
        log_path, launch_prime_elapsed, freeze_elapsed = prepare_images(executable, workdir, PRIME_COMMAND)
        print(f"Startup + prime elapsed: {launch_prime_elapsed:.3f}s")
        print(f"Freeze log written to {log_path}")
        print(f"Freeze duration: {freeze_elapsed:.3f}s")

        print(f"Thawing goblin from {images_dir}")
        thaw_elapsed = measure_thaw(images_dir, PAYLOAD_TEXT, timeout=READ_TIMEOUT)
        print(f"Thaw + response elapsed: {thaw_elapsed:.3f}s")
    except Exception as exc:  # pragma: no cover - demo CLI
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc

if __name__ == "__main__":
    main()
