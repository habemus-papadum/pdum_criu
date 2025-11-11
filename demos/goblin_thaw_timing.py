#!/usr/bin/env python3
"""Launch, freeze, and thaw a goblin to measure restore latency."""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

from pdum.criu import goblins

EXECUTABLE = ["lake", "env", str(Path("/home/nehal/src/lean4-llm/blog/repl/.lake/build/bin/repl"))]
WORKDIR = Path("/home/nehal/src/lean4-llm/blog/repl/test/Mathlib")
IMAGES_DIR = Path("/tmp/time-demo-image")
THAW_PIDFILE = IMAGES_DIR / "thaw.pid"
THAW_LOG = IMAGES_DIR / "thaw.log"
PRIME_COMMAND = '{"cmd": "import Mathlib\\nopen BigOperators\\nopen Real\\nopen Nat"}'
PAYLOAD_TEXT = '{"cmd": "def f := 37"}'


def _launch_process(command: list[str], workdir: Path) -> subprocess.Popen[bytes]:
    """Launch the target process with pipe-based stdio."""

    print(f"Launching {command} (cwd={workdir})")
    env = {**os.environ, "UV_USE_IO_URING": "0"}
    proc = subprocess.Popen(
        command,
        cwd=str(workdir),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
        env=env,
    )
    if proc.stdin is None or proc.stdout is None or proc.stderr is None:
        raise RuntimeError("failed to capture stdio pipes for the target process")
    return proc


def _send_command_read_response(
    writer,
    reader,
    command: str,
    *,
    stage: str,
) -> list[str]:
    """Write ``command`` and collect stdout until a blank line terminates the response."""

    if writer is None or reader is None:
        raise RuntimeError(f"{stage} pipes are not available")

    payload = (command.rstrip("\n") + "\n\n").encode("utf-8")
    print(f"[{stage}] Sending payload: {command!r}")
    writer.write(payload)
    writer.flush()

    lines: list[str] = []
    while True:
        raw = reader.readline()
        if not raw:
            raise RuntimeError(f"{stage} stdout closed before emitting a blank line")
        line = raw.decode("utf-8", errors="replace").rstrip("\n")
        print(f"[{stage}] Response: {line}")
        if line == "":
            break
        lines.append(line)
    return lines


def _prime_process(proc: subprocess.Popen[bytes], command: str) -> None:
    """Send a priming command and ensure the process responds fully."""

    _send_command_read_response(proc.stdin, proc.stdout, command, stage="prime")


def _freeze_process(proc: subprocess.Popen[bytes]) -> Path:
    """Freeze the launched process into the requested images directory."""

    print(f"Freezing PID {proc.pid} into {IMAGES_DIR}")
    log_path = goblins.freeze(
        proc.pid,
        IMAGES_DIR,
        leave_running=False,
        shell_job=False,
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
    executable: list[str],
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


def measure_thaw(images_dir: Path, message: str) -> float:
    """Thaw a goblin, send a message, and time until a blank line appears."""

    start = time.perf_counter()
    pidfile = THAW_PIDFILE
    log_path = THAW_LOG
    if pidfile.exists():
        pidfile.unlink()
    if log_path.exists():
        log_path.unlink()
    goblin = goblins.thaw(
        images_dir,
        shell_job=False,
        detach=True,
        pidfile=pidfile,
        log_path=log_path,
    )
    #print(f"Waiting for PID file {pidfile}")
    #pid = _wait_for_pidfile(pidfile)
    #print(f"Thawed goblin PID: {pid} (log: {log_path})")
    try:
        _send_command_read_response(goblin.stdin, goblin.stdout, message, stage="thaw")
    finally:
        goblin.close()
    return time.perf_counter() - start


def _wait_for_pidfile(pidfile: Path, timeout: float = 10.0) -> int:
    """Poll ``pidfile`` until CRIU writes the restored PID."""

    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            data = pidfile.read_text().strip()
        except FileNotFoundError:
            time.sleep(0.05)
            continue
        if data:
            return int(data)
        time.sleep(0.05)
    raise TimeoutError(f"timed out waiting for pidfile {pidfile}")


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
        thaw_elapsed = measure_thaw(images_dir, PAYLOAD_TEXT)
        print(f"Thaw + response elapsed: {thaw_elapsed:.3f}s")
    except Exception as exc:  # pragma: no cover - demo CLI
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc

if __name__ == "__main__":
    main()
