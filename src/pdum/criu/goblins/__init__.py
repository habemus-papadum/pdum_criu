"""Utility APIs for freezing and thawing goblin processes."""

from __future__ import annotations

import asyncio
import contextlib
import io
import json
import logging
import os
import shlex
import shutil
import signal
import subprocess
import threading
import time
from asyncio import streams
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from .. import utils

__all__ = [
    "freeze",
    "freeze_async",
    "thaw",
    "thaw_async",
    "GoblinProcess",
    "AsyncGoblinProcess",
]


logger = logging.getLogger(__name__)
logger.addHandler(logging.NullHandler())
logger.propagate = False
_META_NAME = ".pdum_goblin_meta.json"


def _metadata_path(images_dir: Path) -> Path:
    return Path(images_dir) / _META_NAME


def freeze(
    pid: int,
    images_dir: str | Path,
    *,
    leave_running: bool = True,
    log_path: str | Path | None = None,
    verbosity: int = 4,
    extra_args: Iterable[str] | None = None,
    shell_job: bool = True,
) -> Path:
    """Checkpoint a goblin process into the specified image directory.

    Parameters
    ----------
    pid : int
        PID of the goblin process to dump.
    images_dir : str | Path
        Directory that will store the CRIU image set.
    leave_running : bool, optional
        Whether to keep the goblin running after the dump completes. Defaults to True.
    log_path : str | Path, optional
        Optional path for CRIU's log file. Defaults to ``images_dir / f"goblin-freeze.{pid}.log"``.
    verbosity : int, optional
        CRIU verbosity level (0-4). Defaults to 4 to aid troubleshooting.
    extra_args : Iterable[str], optional
        Additional CRIU arguments to append verbatim.
    shell_job : bool, optional
        Whether to include ``--shell-job``. Disable when the target already runs
        detached from any controlling TTY. Defaults to True.

    Returns
    -------
    Path
        Path to the CRIU log file for the dump operation.

    Raises
    ------
    RuntimeError
        If CRIU fails to dump the process.
    ValueError
        If ``pid`` is not positive.
    """

    if pid <= 0:
        raise ValueError("PID must be a positive integer")

    logger.info("Freezing goblin pid %s into %s", pid, images_dir)

    context = _build_freeze_context(
        pid,
        images_dir,
        leave_running=leave_running,
        log_path=log_path,
        verbosity=verbosity,
        extra_args=extra_args,
        shell_job=shell_job,
    )

    logger.debug("Running command: %s", shlex.join(context.command))

    result = subprocess.run(
        context.command,
        check=False,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    _ensure_log_readable(context.log_path)
    _handle_freeze_result(result.returncode, context.log_path)

    _record_freeze_metadata(context.images_dir, pid, context.pipe_ids)

    logger.info("Goblin pid %s frozen successfully.", pid)
    return context.log_path


async def freeze_async(
    pid: int,
    images_dir: str | Path,
    *,
    leave_running: bool = True,
    log_path: str | Path | None = None,
    verbosity: int = 4,
    extra_args: Iterable[str] | None = None,
    shell_job: bool = True,
) -> Path:
    """Asynchronously checkpoint a goblin process with CRIU.

    Parameters
    ----------
    pid : int
        PID of the goblin process to dump.
    images_dir : str | Path
        Directory that will store the CRIU image set.
    leave_running : bool, optional
        Keep the goblin alive after dumping. Defaults to True.
    log_path : str | Path, optional
        Path for CRIU's log file. Defaults to ``images_dir / f"goblin-freeze.{pid}.log"``.
    verbosity : int, optional
        CRIU verbosity level (0-4). Defaults to 4.
    extra_args : Iterable[str], optional
        Additional CRIU arguments to append verbatim.
    shell_job : bool, optional
        Whether to pass ``--shell-job`` to CRIU. Defaults to True.

    Returns
    -------
    Path
        Path to the CRIU log file for the dump operation.

    Raises
    ------
    RuntimeError
        If CRIU fails to dump the process.
    ValueError
        If ``pid`` is not positive.
    """

    context = _build_freeze_context(
        pid,
        images_dir,
        leave_running=leave_running,
        log_path=log_path,
        verbosity=verbosity,
        extra_args=extra_args,
        shell_job=shell_job,
    )

    logger.debug("Running command (async): %s", shlex.join(context.command))
    logger.info("Freezing goblin pid %s into %s (async)", pid, images_dir)

    process = await asyncio.create_subprocess_exec(
        *context.command,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
        start_new_session=True,
    )
    returncode = await process.wait()
    _ensure_log_readable(context.log_path)
    _handle_freeze_result(returncode, context.log_path)

    _record_freeze_metadata(context.images_dir, pid, context.pipe_ids)
    logger.info("Goblin pid %s frozen successfully (async).", pid)
    return context.log_path


@dataclass
class GoblinProcess:
    """Synchronous handle returned by :func:`thaw`.

    Parameters
    ----------
    helper_pid : int | None
        PID of the helper process (``criu`` or ``criu-ns``) coordinating the restore.
    stdin, stdout, stderr :
        Binary file objects connected to the goblin's stdio pipes.
    images_dir : Path
        Directory that contains the CRIU image set used for this restore.
    log_path : Path
        Path to the CRIU restore log file.
    pidfile : Path
        Path CRIU populates with the restored process PID.
    """

    helper_pid: int | None
    stdin: io.BufferedWriter
    stdout: io.BufferedReader
    stderr: io.BufferedReader
    images_dir: Path
    log_path: Path
    pidfile: Path

    def read_pidfile(self) -> int:
        """Return the PID recorded by CRIU."""

        return int(self.pidfile.read_text().strip())

    def terminate(self, sig: int = signal.SIGTERM) -> None:
        os.kill(self.read_pidfile(), sig)

    def close(self) -> None:
        for stream in (self.stdin, self.stdout, self.stderr):
            try:
                stream.close()
            except Exception:
                pass


@dataclass
class AsyncGoblinProcess:
    """Async counterpart returned by :func:`thaw_async`.

    Parameters
    ----------
    helper_pid : int | None
        PID of the helper process (``criu`` or ``criu-ns``).
    stdin : asyncio.StreamWriter
        Writable pipe towards the goblin's stdin.
    stdout, stderr : asyncio.StreamReader
        Readers for stdout/stderr respectively.
    images_dir : Path
        Directory containing the image set.
    log_path : Path
        CRIU restore log path.
    pidfile : Path
        File CRIU uses to publish the restored PID.
    """

    helper_pid: int | None
    stdin: asyncio.StreamWriter
    stdout: asyncio.StreamReader
    stderr: asyncio.StreamReader
    images_dir: Path
    log_path: Path
    pidfile: Path

    async def read_pidfile(self) -> int:
        """Asynchronously return the PID recorded by CRIU."""

        data = await _read_file_async(self.pidfile)
        return int(data.decode("utf-8").strip())

    async def close(self) -> None:
        self.stdin.close()
        try:
            await self.stdin.wait_closed()
        except Exception:
            pass


def thaw(
    images_dir: str | Path,
    *,
    extra_args: Iterable[str] | None = None,
    log_path: str | Path | None = None,
    pidfile: str | Path | None = None,
    shell_job: bool = True,
    detach: bool = False,
) -> GoblinProcess:
    """Restore a goblin synchronously and reconnect to its pipes.

    Parameters
    ----------
    images_dir : str | Path
        Directory containing the CRIU image set to restore.
    extra_args : Iterable[str], optional
        Additional CRIU restore arguments to append verbatim.
    log_path : str | Path, optional
        Override for the CRIU restore log file. Defaults to ``images_dir / goblin-thaw.<ts>.log``.
    pidfile : str | Path, optional
        Override for the CRIU ``--pidfile`` argument. Defaults to ``images_dir / goblin-thaw.<ts>.pid``.
    shell_job : bool, optional
        Whether to run CRIU with ``--shell-job`` (required if the target is attached to a TTY). Defaults to True.
    detach : bool, optional
        Whether to pass ``-d`` to CRIU and let it run detached from the helper. Defaults to False.

    Returns
    -------
    GoblinProcess
        Handle that exposes stdio pipes plus metadata (log path, pidfile, helper pid).
        Call :meth:`GoblinProcess.read_pidfile` once CRIU writes the PID file.

    Raises
    ------
    ValueError
        If ``shell_job`` and ``detach`` are both True.
    RuntimeError
        If CRIU fails to start.
    """

    if shell_job and detach:
        raise ValueError("detach=True is incompatible with shell_job=True.")

    context = _build_thaw_context(
        images_dir,
        extra_args=extra_args,
        log_path=log_path,
        pidfile=pidfile,
        shell_job=shell_job,
        detach=detach,
    )
    pipes = _prepare_stdio_pipes(context.pipe_ids)

    restore_proc = _launch_criu_restore_sync(context, pipes)
    stdin, stdout, stderr = pipes.build_sync_streams()
    _reap_process_in_background(restore_proc)
    return GoblinProcess(
        helper_pid=restore_proc.pid,
        stdin=stdin,
        stdout=stdout,
        stderr=stderr,
        images_dir=context.images_dir,
        log_path=context.log_path,
        pidfile=context.pidfile,
    )


async def thaw_async(
    images_dir: str | Path,
    *,
    extra_args: Iterable[str] | None = None,
    log_path: str | Path | None = None,
    pidfile: str | Path | None = None,
    shell_job: bool = True,
    detach: bool = False,
) -> AsyncGoblinProcess:
    """Async variant of :func:`thaw` that returns asyncio streams.

    Parameters
    ----------
    images_dir : str | Path
        Directory containing the CRIU image set to restore.
    extra_args : Iterable[str], optional
        Additional CRIU restore arguments to append verbatim.
    log_path : str | Path, optional
        Override for the CRIU restore log file path.
    pidfile : str | Path, optional
        Override for the CRIU ``--pidfile`` argument.
    shell_job : bool, optional
        Whether to include ``--shell-job`` in the CRIU command. Defaults to True.
    detach : bool, optional
        Whether to pass ``-d`` (detached mode) to CRIU. Defaults to False.

    Returns
    -------
    AsyncGoblinProcess
        Handle exposing asyncio streams plus metadata about the restore. Use
        :meth:`AsyncGoblinProcess.read_pidfile` after CRIU writes the PID file.

    Raises
    ------
    ValueError
        If ``shell_job`` and ``detach`` are both True.
    RuntimeError
        If CRIU fails to start.
    """

    if shell_job and detach:
        raise ValueError("detach=True is incompatible with shell_job=True.")

    context = _build_thaw_context(
        images_dir,
        extra_args=extra_args,
        log_path=log_path,
        pidfile=pidfile,
        shell_job=shell_job,
        detach=detach,
    )
    pipes = _prepare_stdio_pipes(context.pipe_ids)

    restore_proc = await _launch_criu_restore_async(context, pipes)
    stdin_writer = await _make_writer_from_fd(pipes.parent_stdin_fd)
    stdout_reader = await _make_reader_from_fd(pipes.parent_stdout_fd)
    stderr_reader = await _make_reader_from_fd(pipes.parent_stderr_fd)
    _schedule_async_reap(restore_proc)

    return AsyncGoblinProcess(
        helper_pid=restore_proc.pid,
        stdin=stdin_writer,
        stdout=stdout_reader,
        stderr=stderr_reader,
        images_dir=context.images_dir,
        log_path=context.log_path,
        pidfile=context.pidfile,
    )


def _build_thaw_context(
    images_dir: str | Path,
    *,
    extra_args: Iterable[str] | None,
    log_path: str | Path | None,
    pidfile: str | Path | None,
    shell_job: bool,
    detach: bool,
) -> _ThawContext:
    images = Path(images_dir).expanduser().resolve()
    if not images.exists():
        raise RuntimeError(f"images directory does not exist: {images}")

    meta = _load_metadata(images)
    if "pipe_ids" not in meta:
        pipe_ids = _pipe_ids_from_images(images)
    else:
        pipe_ids = meta["pipe_ids"]

    timestamp = int(time.time())
    if log_path is None:
        resolved_log = images / f"goblin-thaw.{timestamp}.log"
    else:
        resolved_log = Path(log_path).expanduser().resolve()
        resolved_log.parent.mkdir(parents=True, exist_ok=True)

    if pidfile is None:
        resolved_pidfile = images / f"goblin-thaw.{timestamp}.pid"
    else:
        resolved_pidfile = Path(pidfile).expanduser().resolve()
        resolved_pidfile.parent.mkdir(parents=True, exist_ok=True)

    sudo_cmd = utils.resolve_command("sudo")

    try:
        criu_ns = utils.ensure_criu_ns(verbose=False, raise_=True)
        restore_cmd = [criu_ns]
    except Exception:
        criu_bin = utils.ensure_criu(verbose=False, raise_=True)
        restore_cmd = [criu_bin]

    command = [
        *restore_cmd,
        "restore",
        "-D",
        str(images),
        "-o",
        str(resolved_log),
        "--pidfile",
        str(resolved_pidfile),
    ]
    if shell_job:
        command.append("--shell-job")
    if detach:
        command.append("-d")

    if extra_args:
        command.extend(extra_args)

    return _ThawContext(
        restore_cmd=command,
        log_path=resolved_log,
        images_dir=images,
        pipe_ids=pipe_ids,
        pidfile=resolved_pidfile,
        sudo_cmd=sudo_cmd,
    )


class _FreezeContext:
    def __init__(
        self,
        command: list[str],
        log_path: Path,
        images_dir: Path,
        pid: int,
        leave_running: bool,
        pipe_ids: dict[str, str],
        shell_job: bool,
    ) -> None:
        self.command = command
        self.log_path = log_path
        self.images_dir = images_dir
        self.pid = pid
        self.leave_running = leave_running
        self.pipe_ids = pipe_ids
        self.shell_job = shell_job


@dataclass
class _ThawContext:
    restore_cmd: list[str]
    log_path: Path
    images_dir: Path
    pipe_ids: dict[str, str]
    pidfile: Path
    sudo_cmd: str


def _build_freeze_context(
    pid: int,
    images_dir: str | Path,
    *,
    leave_running: bool,
    log_path: str | Path | None,
    verbosity: int,
    extra_args: Iterable[str] | None,
    shell_job: bool,
) -> _FreezeContext:
    utils.ensure_linux()
    utils.ensure_sudo(verbose=False, raise_=True)
    criu_path = utils.ensure_criu(verbose=False, raise_=True)
    if not criu_path:
        raise RuntimeError("CRIU executable not found")

    pipe_ids = _collect_pipe_ids_from_proc(pid)

    images_dir = Path(images_dir).expanduser().resolve()
    images_dir.mkdir(parents=True, exist_ok=True)

    if log_path is None:
        resolved_log = images_dir / f"goblin-freeze.{pid}.log"
    else:
        resolved_log = Path(log_path).expanduser().resolve()
    resolved_log.parent.mkdir(parents=True, exist_ok=True)

    command = [
        utils.resolve_command("sudo"),
        "-n",
        criu_path,
        "dump",
        "-D",
        str(images_dir),
        "-t",
        str(pid),
        "-o",
        str(resolved_log),
        f"-v{verbosity}",
    ]

    if shell_job:
        command.append("--shell-job")

    if leave_running:
        command.append("--leave-running")

    if extra_args:
        command.extend(extra_args)

    return _FreezeContext(command, resolved_log, images_dir, pid, leave_running, pipe_ids, shell_job)
def _handle_freeze_result(returncode: int, log_path: Path) -> None:
    if returncode == 0:
        return

    try:
        log_tail = utils.tail_file(log_path, lines=10)
    except PermissionError:
        log_tail = "(log unreadable due to permission error)"
    except OSError as exc:
        log_tail = f"(failed to read log: {exc})"

    logger.error(
        "CRIU dump failed (exit %s). Tail:%s%s",
        returncode,
        "\n" if log_tail else "",
        log_tail,
    )
    raise RuntimeError(f"CRIU dump failed with exit code {returncode}")


def _record_freeze_metadata(images_dir: Path, pid: int, pipe_ids: dict[str, str]) -> None:
    meta = {
        "pid": pid,
        "pipe_ids": pipe_ids,
        "captured_at": datetime.now(timezone.utc).isoformat(),
    }
    _metadata_path(images_dir).write_text(json.dumps(meta, indent=2))


def _load_metadata(images_dir: Path) -> dict:
    path = _metadata_path(images_dir)
    if not path.exists():
        return {}
    return json.loads(path.read_text())


def _collect_pipe_ids_from_proc(pid: int) -> dict[str, str]:
    base = Path("/proc") / str(pid) / "fd"
    pipe_ids: dict[str, str] = {}
    for name, fd in (("stdin", 0), ("stdout", 1), ("stderr", 2)):
        try:
            target = os.readlink(str(base / str(fd)))
        except OSError as exc:
            raise RuntimeError(f"failed to inspect fd {fd}: {exc}") from exc
        if not target.startswith("pipe:["):
            raise RuntimeError(
                f"fd {fd} ({name}) is not an unnamed pipe (target={target!r}); goblins require pipe stdio"
            )
        pipe_ids[name] = target
    return pipe_ids


def _pipe_ids_from_images(images_dir: Path) -> dict[str, str]:
    crit_bin = shutil.which("crit")
    if not crit_bin:
        raise RuntimeError("crit utility not found; install CRIU tools to support leave_running=False")

    fdinfo_imgs = sorted(images_dir.glob("fdinfo-*.img"))
    if not fdinfo_imgs:
        raise RuntimeError(f"no fdinfo-*.img present in {images_dir}")
    fdinfo_img = fdinfo_imgs[0]

    fd_map = _crit_show_json(crit_bin, fdinfo_img)
    fd_to_id: dict[int, str] = {}
    for entry in fd_map.get("entries", []):
        fdnum = entry.get("fd")
        file_id = entry.get("id") or entry.get("file_id") or entry.get("id_id")
        if fdnum is not None and file_id is not None:
            fd_to_id[int(fdnum)] = file_id

    ids = [fd_to_id.get(0), fd_to_id.get(1), fd_to_id.get(2)]
    if not all(ids):
        raise RuntimeError("unable to resolve fd ids from fdinfo image")

    files_img = images_dir / "files.img"
    files_json = _crit_show_json(crit_bin, files_img)
    id_to_pipe: dict[str, str] = {}
    for entry in files_json.get("entries", []):
        candidate_id = entry.get("id") or entry.get("file_id") or entry.get("ino_id")
        if not candidate_id:
            continue
        pipe_value = _find_pipe_value(entry)
        if pipe_value:
            id_to_pipe[candidate_id] = pipe_value

    stdin_pipe = id_to_pipe.get(ids[0])
    stdout_pipe = id_to_pipe.get(ids[1])
    stderr_pipe = id_to_pipe.get(ids[2])
    if not (stdin_pipe and stdout_pipe and stderr_pipe):
        raise RuntimeError("failed to map pipe ids from CRIU files image")

    return {"stdin": stdin_pipe, "stdout": stdout_pipe, "stderr": stderr_pipe}


def _crit_show_json(crit_bin: str, image_path: Path) -> dict:
    candidate_args = [
        ["show", "-i", str(image_path), "--pretty"],
        ["show", "-i", str(image_path), "--format", "json"],
        ["show", "-i", str(image_path), "-f", "json"],
        ["show", str(image_path), "--format", "json"],
        ["show", str(image_path), "-f", "json"],
        ["show", str(image_path)],
    ]

    last_error = None
    for args in candidate_args:
        result = subprocess.run(
            [crit_bin, *args],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            last_error = result.stderr or result.stdout
            continue
        try:
            return json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            last_error = str(exc)
            continue

    raise RuntimeError(
        f"crit show failed for {image_path}: {last_error or 'unknown error'}"
    )


def _find_pipe_value(obj) -> str | None:
    if isinstance(obj, str) and obj.startswith("pipe:["):
        return obj
    if isinstance(obj, dict):
        for value in obj.values():
            found = _find_pipe_value(value)
            if found:
                return found
    elif isinstance(obj, list):
        for item in obj:
            found = _find_pipe_value(item)
            if found:
                return found
    return None


class _StdioPipes:
    def __init__(self, pipe_ids: dict[str, str]) -> None:
        self.pipe_ids = pipe_ids
        self.parent_stdin_fd: int | None = None
        self.parent_stdout_fd: int | None = None
        self.parent_stderr_fd: int | None = None
        self.child_fds: list[int] = []
        self._inherit_args: list[str] = []
        self._create_pipes()

    def _create_pipes(self) -> None:
        r_stdin, w_stdin = os.pipe()
        r_stdout, w_stdout = os.pipe()
        r_stderr, w_stderr = os.pipe()

        _make_inheritable(r_stdin)
        _make_inheritable(w_stdout)
        _make_inheritable(w_stderr)

        self.parent_stdin_fd = w_stdin
        self.parent_stdout_fd = r_stdout
        self.parent_stderr_fd = r_stderr

        self.child_fds = [r_stdin, w_stdout, w_stderr]

        inherit_map = {
            r_stdin: self.pipe_ids["stdin"],
            w_stdout: self.pipe_ids["stdout"],
            w_stderr: self.pipe_ids["stderr"],
        }
        for fdnum, pipe_spec in inherit_map.items():
            self._inherit_args += ["--inherit-fd", f"fd[{fdnum}]:{pipe_spec}"]

    @property
    def inherit_args(self) -> list[str]:
        return list(self._inherit_args)

    @property
    def child_stdio_fds(self) -> list[int]:
        return list(self.child_fds)

    def close_child_fds(self) -> None:
        for fd in self.child_fds:
            try:
                os.close(fd)
            except OSError:
                pass
        self.child_fds.clear()

    def close_parent_ends(self) -> None:
        for fd in (self.parent_stdin_fd, self.parent_stdout_fd, self.parent_stderr_fd):
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass
        self.parent_stdin_fd = self.parent_stdout_fd = self.parent_stderr_fd = None

    def build_sync_streams(self) -> tuple[io.BufferedWriter, io.BufferedReader, io.BufferedReader]:
        if None in (self.parent_stdin_fd, self.parent_stdout_fd, self.parent_stderr_fd):
            raise RuntimeError("stdio pipes already closed")
        stdin = os.fdopen(self.parent_stdin_fd, "wb", buffering=0)
        stdout = os.fdopen(self.parent_stdout_fd, "rb", buffering=0)
        stderr = os.fdopen(self.parent_stderr_fd, "rb", buffering=0)
        # transfer ownership to file objects
        self.parent_stdin_fd = self.parent_stdout_fd = self.parent_stderr_fd = None
        return stdin, stdout, stderr


def _prepare_stdio_pipes(pipe_ids: dict[str, str]) -> _StdioPipes:
    return _StdioPipes(pipe_ids)


def _make_inheritable(fd: int) -> None:
    os.set_inheritable(fd, True)


def _launch_criu_restore_sync(context: _ThawContext, pipes: _StdioPipes) -> subprocess.Popen[bytes]:
    utils.ensure_sudo_closefrom()
    command = _build_restore_command_with_inherit(context, pipes)
    child_fds = pipes.child_stdio_fds
    try:
        proc = subprocess.Popen(
            command,
            pass_fds=child_fds,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except Exception:
        pipes.close_parent_ends()
        pipes.close_child_fds()
        raise

    _ensure_log_readable(context.log_path)
    pipes.close_child_fds()
    return proc


async def _launch_criu_restore_async(
    context: _ThawContext,
    pipes: _StdioPipes,
) -> asyncio.subprocess.Process:
    utils.ensure_sudo_closefrom()
    command = _build_restore_command_with_inherit(context, pipes)
    child_fds = pipes.child_stdio_fds
    try:
        proc = await asyncio.create_subprocess_exec(
            *command,
            pass_fds=child_fds,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
            start_new_session=True,
        )
    except Exception:
        pipes.close_parent_ends()
        pipes.close_child_fds()
        raise

    _ensure_log_readable(context.log_path)
    pipes.close_child_fds()
    return proc


def _build_restore_command_with_inherit(context: _ThawContext, pipes: _StdioPipes) -> list[str]:
    command = [context.sudo_cmd, "-n"]
    child_fds = pipes.child_stdio_fds
    if child_fds:
        closefrom = max(child_fds) + 1
        command += ["-C", str(closefrom)]
    return command + context.restore_cmd + pipes.inherit_args


def _handle_thaw_failure(returncode: int, log_path: Path) -> None:
    try:
        log_tail = utils.tail_file(log_path, lines=20)
    except PermissionError:
        log_tail = "(log unreadable due to permission error)"
    except OSError as exc:
        log_tail = f"(failed to read log: {exc})"
    raise RuntimeError(
        f"CRIU restore failed with exit code {returncode}. Log tail:\n{log_tail}"
    )


def _ensure_log_readable(log_path: Path) -> None:
    if not log_path.exists():
        return
    if os.access(log_path, os.R_OK):
        return
    try:
        sudo_path = utils.resolve_command("sudo")
    except (FileNotFoundError, ValueError):
        return

    owner = f"{os.getuid()}:{os.getgid()}"
    subprocess.run([sudo_path, "-n", "chown", owner, str(log_path)], check=False)
    subprocess.run([sudo_path, "-n", "chmod", "0644", str(log_path)], check=False)


def _terminate_process(proc: subprocess.Popen[bytes]) -> None:
    if proc.poll() is not None:
        _reap_process(proc)
        return
    try:
        proc.terminate()
    except OSError:
        pass
    try:
        proc.wait(timeout=2)
    except subprocess.TimeoutExpired:
        try:
            proc.kill()
            proc.wait(timeout=1)
        except Exception:
            pass


async def _terminate_process_async(proc: asyncio.subprocess.Process) -> None:
    if proc.returncode is not None:
        return
    try:
        proc.terminate()
    except ProcessLookupError:
        return
    try:
        await asyncio.wait_for(proc.wait(), timeout=2)
        return
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except ProcessLookupError:
            return
        try:
            await proc.wait()
        except Exception:
            pass


def _reap_process(proc: subprocess.Popen[bytes]) -> None:
    try:
        proc.wait(timeout=0)
    except subprocess.TimeoutExpired:
        pass
    except Exception:
        pass


def _reap_process_in_background(proc: subprocess.Popen[bytes]) -> None:
    if proc.poll() is not None:
        _reap_process(proc)
        return

    def _wait() -> None:
        try:
            proc.wait()
        except Exception:
            pass

    threading.Thread(target=_wait, daemon=True).start()


def _schedule_async_reap(proc: asyncio.subprocess.Process) -> None:
    if proc.returncode is not None:
        return

    async def _wait() -> None:
        try:
            await proc.wait()
        except Exception:
            pass

    loop = asyncio.get_running_loop()
    loop.create_task(_wait())




async def _make_reader_from_fd(fd: int) -> asyncio.StreamReader:
    loop = asyncio.get_running_loop()
    reader = asyncio.StreamReader()
    protocol = asyncio.StreamReaderProtocol(reader)
    read_file = os.fdopen(fd, "rb", buffering=0, closefd=False)
    await loop.connect_read_pipe(lambda: protocol, read_file)
    return reader


async def _make_writer_from_fd(fd: int) -> asyncio.StreamWriter:
    loop = asyncio.get_running_loop()
    write_file = os.fdopen(fd, "wb", buffering=0, closefd=False)
    transport, protocol = await loop.connect_write_pipe(streams.FlowControlMixin, write_file)
    return asyncio.StreamWriter(transport, protocol, None, loop)


async def _read_file_async(path: Path, chunk_size: int = 65536) -> bytes:
    loop = asyncio.get_running_loop()
    fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
    try:
        chunks: list[bytes] = []
        while True:
            try:
                chunk = os.read(fd, chunk_size)
                if not chunk:
                    break
                chunks.append(chunk)
                continue
            except BlockingIOError:
                await _wait_for_fd_readable(loop, fd)
            except InterruptedError:
                continue
        return b"".join(chunks)
    finally:
        os.close(fd)


async def _wait_for_fd_readable(loop: asyncio.AbstractEventLoop, fd: int) -> None:
    fut: asyncio.Future[None] = loop.create_future()

    def _resume() -> None:
        loop.remove_reader(fd)
        if not fut.done():
            fut.set_result(None)

    loop.add_reader(fd, _resume)
    try:
        await fut
    finally:
        if not fut.done():
            fut.cancel()
        with contextlib.suppress(Exception):
            loop.remove_reader(fd)
