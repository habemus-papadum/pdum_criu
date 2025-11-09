"""Minimal CLI live tests executed under a pseudo-TTY."""

from __future__ import annotations

import os
import re
import shlex
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path

import pytest

from .test_live_criu import (
    _images_dir,
    _require_live_prereqs,
    _terminate,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = REPO_ROOT / "src"
_EXIT_MARKER = "__PDUM_EXIT_CODE="


def _build_command(args: list[str], env: dict[str, str], tmp_path: Path) -> list[str]:
    cli_exe = shutil.which("pdum-criu")
    if cli_exe is None:
        raise RuntimeError("pdum-criu entrypoint not found on PATH")
    if "COVERAGE_PROCESS_START" in env:
        env["COVERAGE_FILE"] = os.fspath(REPO_ROOT / f".coverage.cli-{uuid.uuid4().hex}")
    return [cli_exe, *args]


def _run_cli(args: list[str], tmp_path: Path, timeout: float = 15.0) -> tuple[int, str]:
    """Run pdum-criu inside script(1) so CRIU sees a real TTY."""

    env = os.environ.copy()
    existing = env.get("PYTHONPATH")
    merged = os.pathsep.join(
        [str(SRC_DIR)] + ([existing] if existing else []) + [str(REPO_ROOT)]
    )
    env["PYTHONPATH"] = merged

    base_cmd = _build_command(args, env, tmp_path)
    quoted = " ".join(shlex.quote(str(part)) for part in base_cmd)
    wrapped = f"{quoted}; printf '\\n{_EXIT_MARKER}%s\\n' $?"

    log_path = tmp_path / f"cli-{uuid.uuid4().hex}.log"
    subprocess.run(
        ["script", "-q", str(log_path), "-c", wrapped],
        capture_output=True,
        text=True,
        env=env,
        timeout=timeout,
        check=False,
    )
    output = log_path.read_text(encoding="utf-8", errors="ignore")
    match = re.search(rf"{_EXIT_MARKER}(\d+)", output)
    exit_code = int(match.group(1)) if match else 0
    return exit_code, output


def _spawn_cli_target() -> subprocess.Popen[bytes]:
    script = (
        "import os, sys, time\n"
        "os.setsid()\n"
        "sys.stdout.write('cli target ready\\n')\n"
        "sys.stdout.flush()\n"
        "for line in sys.stdin:\n"
        "    if line.strip() == 'exit':\n"
        "        break\n"
        "    sys.stdout.write(line)\n"
        "    sys.stdout.flush()\n"
        "time.sleep(300)\n"
    )
    proc = subprocess.Popen(
        [sys.executable, "-u", "-c", script],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if proc.stdout:
        proc.stdout.readline()
    time.sleep(0.1)
    return proc


@pytest.mark.criu_live
def test_cli_shell_freeze_creates_images(tmp_path: Path) -> None:
    """Ensure `pdum-criu shell freeze` produces CRIU artifacts."""

    _require_live_prereqs()
    proc = _spawn_cli_target()
    with _images_dir(tmp_path, "cli-freeze") as images_dir:
        freeze_args = [
            "shell",
            "freeze",
            "--dir",
            str(images_dir),
            "--pid",
            str(proc.pid),
            "--no-leave-running",
            "--no-validate-tty",
            "--log-file",
            str(images_dir / "cli-freeze.log"),
            "--hide-tail",
        ]
        code, output = _run_cli(freeze_args, tmp_path)
        assert code == 0, output
        contents = sorted(p.name for p in images_dir.iterdir())
        assert contents, f"CRIU directory empty.\nCLI output:\n{output}"
        assert (images_dir / ".pdum_criu_meta.json").exists()
    _terminate(proc)


@pytest.mark.criu_live
def test_cli_doctor_runs(tmp_path: Path) -> None:
    """`pdum-criu doctor` should complete successfully in live environments."""

    _require_live_prereqs()
    code, output = _run_cli(["doctor"], tmp_path, timeout=10.0)
    assert code == 0, output
