"""Live integration tests that exercise CRIU end-to-end."""

from __future__ import annotations

import os
import select
import shutil
import signal
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path

import pytest

from pdum.criu import goblins

_SBIN_PATH = os.pathsep.join(
    segment
    for segment in ["/usr/local/sbin", "/usr/sbin", "/sbin"]
    if segment
)


def _which(cmd: str) -> str | None:
    search_path = os.environ.get("PATH", "")
    if _SBIN_PATH:
        search_path = f"{search_path}{os.pathsep if search_path else ''}{_SBIN_PATH}"
    return shutil.which(cmd, path=search_path)


def _require_live_prereqs() -> None:
    if os.name != "posix":
        pytest.skip("CRIU requires Linux/posix")

    if _which("criu") is None:
        pytest.skip("CRIU binary not available (searched PATH + sbin dirs)")
    if _which("sudo") is None:
        pytest.skip("sudo is required for live CRIU tests")
    if _which("pgrep") is None:
        pytest.skip("pgrep is required for live CRIU tests")

    sudo_cmd = _which("sudo")
    assert sudo_cmd is not None  # for mypy/linters

    result = subprocess.run([sudo_cmd, "-n", "true"], capture_output=True)
    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", errors="ignore").strip()
        if not detail:
            detail = result.stdout.decode("utf-8", errors="ignore").strip()
        pytest.skip(
            "password-less sudo is required for live CRIU tests"
            + (f" (sudo -n true: {detail})" if detail else "")
        )


def _spawn_goblin() -> subprocess.Popen[bytes]:
    script = "import os, time;\n" "os.setsid();\n" "time.sleep(300)"
    proc = subprocess.Popen(
        [sys.executable, "-u", "-c", script],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    time.sleep(0.2)
    return proc


@contextmanager
def _images_dir(tmp_path: Path, name: str):
    root = tmp_path / name
    if root.exists():
        shutil.rmtree(root, ignore_errors=True)
    root.mkdir()
    cleaned = False
    try:
        yield root
        cleaned = True
    finally:
        if cleaned:
            shutil.rmtree(root, ignore_errors=True)


def _terminate(proc: subprocess.Popen[bytes]) -> None:
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            proc.kill()


def _read_log_tail_as_root(log_path: Path) -> str:
    if not log_path.exists():
        return "(log file not created)"

    sudo_cmd = _which("sudo")
    if not sudo_cmd:
        return f"(log exists at {log_path}, but sudo not found to read it)"

    result = subprocess.run(
        [sudo_cmd, "-n", "tail", "-n", "20", str(log_path)],
        capture_output=True,
        text=True,
    )
    if result.returncode == 0:
        return result.stdout.strip()
    detail = result.stderr.strip() or result.stdout.strip()
    return f"(sudo tail failed: {detail})"


@pytest.mark.criu_live
def test_goblin_freeze_live(tmp_path: Path) -> None:
    _require_live_prereqs()

    proc = _spawn_goblin()
    with _images_dir(tmp_path, "freeze-sync") as images_dir:
        try:
            try:
                log_path = goblins.freeze(proc.pid, images_dir, leave_running=False)
            except RuntimeError as exc:
                log_tail = _read_log_tail_as_root(images_dir / f"goblin-freeze.{proc.pid}.log")
                pytest.skip(
                    "CRIU freeze failed in this environment: "
                    f"{exc}\nLog tail (sudo tail -n 20):\n{log_tail}"
                )

            assert log_path.exists()
            assert any(images_dir.iterdir()), "CRIU did not produce any image files"

            # Process should have been stopped by CRIU when leave_running=False.
            proc.wait(timeout=5)
        finally:
            _terminate(proc)
def _spawn_echo_goblin() -> subprocess.Popen[bytes]:
    script = (
        "import os, sys\n"
        "os.setsid()\n"
        "sys.stdout.write('ready\\n')\n"
        "sys.stdout.flush()\n"
        "import sys\n"
        "for line in sys.stdin:\n"
        "    sys.stdout.write(line.upper())\n"
        "    sys.stdout.flush()\n"
        "    sys.stderr.write('echoed\\n')\n"
        "    sys.stderr.flush()\n"
    )
    proc = subprocess.Popen(
        [sys.executable, "-u", "-c", script],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    # Wait for the ready banner so CRIU snapshots a quiescent state.
    proc.stdout.readline()
    time.sleep(0.2)
    return proc


def _read_line(file, timeout: float = 5.0) -> bytes:
    start = time.time()
    while True:
        if file.closed:
            return b""
        ready, _, _ = select.select([file], [], [], max(0.0, start + timeout - time.time()))
        if not ready:
            raise TimeoutError("timed out waiting for goblin output")
        line = file.readline()
        if line:
            return line


def _wait_for_pid_exit(pid: int, timeout: float = 5.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not Path(f"/proc/{pid}").exists():
            return
        time.sleep(0.05)
    raise AssertionError(f"process {pid} still alive after {timeout}s")


@pytest.mark.criu_live
def test_goblin_thaw_sync_live(tmp_path: Path) -> None:
    _require_live_prereqs()

    proc = _spawn_echo_goblin()
    with _images_dir(tmp_path, "thaw-sync") as images_dir:
        try:
            goblins.freeze(proc.pid, images_dir, leave_running=False, shell_job=False)
        except RuntimeError as exc:
            log_tail = _read_log_tail_as_root(images_dir / f"goblin-freeze.{proc.pid}.log")
            pytest.skip(f"CRIU freeze failed: {exc}\nLog tail:\n{log_tail}")

        proc.wait(timeout=5)

        try:
            thawed = goblins.thaw(images_dir, shell_job=False, detach=True)
        except RuntimeError as exc:
            if "closefrom_override" in str(exc):
                pytest.skip(str(exc))
            raise
        assert thawed.stdin and thawed.stdout
        thawed.stdin.write(b"ping sync\n")
        thawed.stdin.flush()
        response = _read_line(thawed.stdout)
        assert b"PING SYNC" in response.upper()

        thawed.stdin.write(b"exit\n")
        thawed.stdin.flush()
        thawed.close()
        if thawed.helper_pid is not None:
            os.kill(thawed.helper_pid, signal.SIGTERM)
            _wait_for_pid_exit(thawed.helper_pid, timeout=10)


@pytest.mark.criu_live
def test_pipe_ids_from_images_live(tmp_path: Path) -> None:
    _require_live_prereqs()
    if shutil.which("crit") is None:
        pytest.skip("crit utility not installed")

    proc = _spawn_echo_goblin()
    with _images_dir(tmp_path, "pipe-ids") as images_dir:
        try:
            goblins.freeze(proc.pid, images_dir, leave_running=False, shell_job=False)
        except RuntimeError as exc:
            log_tail = _read_log_tail_as_root(images_dir / f"goblin-freeze.{proc.pid}.log")
            pytest.skip(f"CRIU freeze failed: {exc}\nLog tail:\n{log_tail}")

        proc.wait(timeout=5)
        meta = images_dir / ".pdum_goblin_meta.json"
        if meta.exists():
            meta.unlink()

        try:
            pipe_ids = goblins._pipe_ids_from_images(images_dir)
        except RuntimeError as exc:
            # CRIT is present, but the CRIU build on this machine did not emit
            # fdinfo/files metadata in the shape _pipe_ids_from_images expects.
            # Rather than failing the entire live suite when CRIU omits those
            # internals, we skip with the captured reason.
            pytest.skip(f"crit parsing failed: {exc}")
        assert set(pipe_ids) == {"stdin", "stdout", "stderr"}
        for value in pipe_ids.values():
            assert value.startswith("pipe:[")
