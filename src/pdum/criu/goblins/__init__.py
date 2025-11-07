"""Utility APIs for freezing and thawing goblin processes."""

from __future__ import annotations

import asyncio
import io
import json
import logging
import os
import shlex
import shutil
import signal
import subprocess
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
    )

    logger.debug("Running command: %s", shlex.join(context.command))

    result = subprocess.run(context.command, check=False)
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
) -> Path:
    """Async variant of :func:`freeze` using asyncio subprocesses."""

    context = _build_freeze_context(
        pid,
        images_dir,
        leave_running=leave_running,
        log_path=log_path,
        verbosity=verbosity,
        extra_args=extra_args,
    )

    logger.debug("Running command (async): %s", shlex.join(context.command))

    process = await asyncio.create_subprocess_exec(*context.command)
    returncode = await process.wait()
    _handle_freeze_result(returncode, context.log_path)

    _record_freeze_metadata(context.images_dir, pid, context.pipe_ids)
    logger.info("Goblin pid %s frozen successfully (async).", pid)
    return context.log_path


@dataclass
class GoblinProcess:
    pid: int
    stdin: io.BufferedWriter
    stdout: io.BufferedReader
    stderr: io.BufferedReader
    images_dir: Path
    log_path: Path

    def terminate(self, sig: int = signal.SIGTERM) -> None:
        os.kill(self.pid, sig)

    def close(self) -> None:
        for stream in (self.stdin, self.stdout, self.stderr):
            try:
                stream.close()
            except Exception:
                pass


@dataclass
class AsyncGoblinProcess:
    pid: int
    stdin: asyncio.StreamWriter
    stdout: asyncio.StreamReader
    stderr: asyncio.StreamReader
    images_dir: Path
    log_path: Path

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
) -> GoblinProcess:
    """Restore a goblin synchronously and return file objects for stdio."""

    context = _build_thaw_context(images_dir, extra_args=extra_args)
    pipes = _prepare_stdio_pipes(context.pipe_ids)

    try:
        _run_criu_restore(context, pipes)
        pid = _wait_for_pidfile(context.pidfile)
    except Exception:
        pipes.close_parent_ends()
        raise

    stdin, stdout, stderr = pipes.build_sync_streams()
    return GoblinProcess(
        pid=pid,
        stdin=stdin,
        stdout=stdout,
        stderr=stderr,
        images_dir=context.images_dir,
        log_path=context.log_path,
    )


async def thaw_async(
    images_dir: str | Path,
    *,
    extra_args: Iterable[str] | None = None,
) -> AsyncGoblinProcess:
    """Restore a goblin and expose asyncio streams."""

    context = _build_thaw_context(images_dir, extra_args=extra_args)
    pipes = _prepare_stdio_pipes(context.pipe_ids)

    try:
        _run_criu_restore(context, pipes)
        pid = _wait_for_pidfile(context.pidfile)
    except Exception:
        pipes.close_parent_ends()
        raise

    stdin_writer = await _make_writer_from_fd(pipes.parent_stdin_fd)
    stdout_reader = await _make_reader_from_fd(pipes.parent_stdout_fd)
    stderr_reader = await _make_reader_from_fd(pipes.parent_stderr_fd)

    return AsyncGoblinProcess(
        pid=pid,
        stdin=stdin_writer,
        stdout=stdout_reader,
        stderr=stderr_reader,
        images_dir=context.images_dir,
        log_path=context.log_path,
    )


def _build_thaw_context(images_dir: str | Path, *, extra_args: Iterable[str] | None) -> _ThawContext:
    images = Path(images_dir).expanduser().resolve()
    if not images.exists():
        raise RuntimeError(f"images directory does not exist: {images}")

    meta = _load_metadata(images)
    if "pipe_ids" not in meta:
        pipe_ids = _pipe_ids_from_images(images)
    else:
        pipe_ids = meta["pipe_ids"]

    log_path = images / f"goblin-thaw.{int(time.time())}.log"
    pidfile = images / f"goblin-thaw.{int(time.time())}.pid"

    sudo_cmd = utils.resolve_command("sudo")

    try:
        criu_ns = utils.ensure_criu_ns(verbose=False, raise_=True)
        restore_cmd = [criu_ns]
    except Exception:
        criu_ns = None
        criu_bin = utils.ensure_criu(verbose=False, raise_=True)
        restore_cmd = [criu_bin]

    command = [
        sudo_cmd,
        "-n",
        *restore_cmd,
        "restore",
        "-D",
        str(images),
        "-o",
        str(log_path),
        "--pidfile",
        str(pidfile),
    ]

    if extra_args:
        command.extend(extra_args)

    return _ThawContext(
        command=command,
        log_path=log_path,
        images_dir=images,
        pipe_ids=pipe_ids,
        pidfile=pidfile,
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
    ) -> None:
        self.command = command
        self.log_path = log_path
        self.images_dir = images_dir
        self.pid = pid
        self.leave_running = leave_running
        self.pipe_ids = pipe_ids


@dataclass
class _ThawContext:
    command: list[str]
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

    if leave_running:
        command.append("--leave-running")

    if extra_args:
        command.extend(extra_args)

    return _FreezeContext(command, resolved_log, images_dir, pid, leave_running, pipe_ids)
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


def _run_criu_restore(context: _ThawContext, pipes: _StdioPipes) -> None:
    utils.ensure_sudo_closefrom()
    command = list(context.command)
    if pipes.child_stdio_fds:
        closefrom = max(pipes.child_stdio_fds) + 1
        command.insert(1, str(closefrom))
        command.insert(1, "-C")
    command += pipes.inherit_args
    result = subprocess.run(command, check=False, pass_fds=pipes.child_stdio_fds)
    if result.returncode != 0:
        pipes.close_parent_ends()
        _handle_thaw_failure(result.returncode, context.log_path)
    pipes.close_child_fds()


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


def _wait_for_pidfile(pidfile: Path, timeout: float = 5.0) -> int:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if pidfile.exists():
            content = pidfile.read_text().strip()
            if content.isdigit():
                return int(content)
        time.sleep(0.02)
    raise RuntimeError(f"Timed out waiting for CRIU pidfile {pidfile}")


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
