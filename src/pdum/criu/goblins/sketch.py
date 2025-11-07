#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Demo: Freeze & Clone a PIPE-stdio subprocess with CRIU, while keeping asyncio pipes.

- freeze(pid, imgdir, leave_running=True): checkpoint a process and record pipe IDs
- clone(imgdir): restore a clone whose stdin/stdout/stderr are new anonymous pipes
  owned by this Python process (no named FIFOs), exposed as asyncio streams.

Core CRIU idea used: --inherit-fd for external unnamed pipes. See:
  https://criu.org/Inheriting_FDs_on_restore  (external unnamed pipes example)
We restore inside a fresh PID namespace via criu-ns (or unshare fallback):
  https://github.com/checkpoint-restore/criu/issues/1670 (why PID ns avoids PID clashes)

Tested conceptually against CRIU v4.x API/semantics as documented.
"""

import asyncio
import json
import os
import shlex
import shutil
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple

SUDO = os.environ.get("CRIU_SUDO", "sudo")  # allow overriding (or set to "" if running as root)
CRIU_BIN = os.environ.get("CRIU_BIN", "criu")
CRIU_NS = os.environ.get("CRIU_NS", "criu-ns")  # usually /usr/sbin/criu-ns
CRIT_BIN = os.environ.get("CRIT_BIN", "crit")

META_NAME = "py-meta.json"


# ---------- small helpers ----------


def _which_or_none(bin_name: str) -> Optional[str]:
    return shutil.which(bin_name)


def _cmd(*parts: str) -> List[str]:
    return [p for p in parts if p != ""]


def _readlink(path: Path) -> str:
    return os.readlink(str(path))


def _pipe_id_from_proc(pid: int, fd: int) -> Optional[str]:
    """Return e.g. 'pipe:[12345]' for /proc/<pid>/fd/<fd>, or None if not a pipe."""
    target = _readlink(Path("/proc") / str(pid) / "fd" / str(fd))
    return target if target.startswith("pipe:[") else None


async def _run(*argv: str, **popen_kwargs) -> Tuple[int, str, str]:
    """Run a child process to completion; return (rc, stdout, stderr)."""
    p = await asyncio.create_subprocess_exec(
        *argv, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, **popen_kwargs
    )
    out, err = await p.communicate()
    return p.returncode, out.decode(errors="replace"), err.decode(errors="replace")


async def _sudo_run(*argv: str, **kwargs) -> Tuple[int, str, str]:
    return await _run(*(_cmd(SUDO) + list(argv)), **kwargs)


def _require(cond: bool, msg: str):
    if not cond:
        raise RuntimeError(msg)


# ---------- CRIU metadata (pipe IDs for stdio) ----------


async def _collect_stdio_pipe_ids(pid: int) -> Dict[str, str]:
    """
    Best effort: read the pipe identities for fd 0/1/2 from /proc.
    Returns {'stdin': 'pipe:[X]', 'stdout': 'pipe:[Y]', 'stderr': 'pipe:[Z]'}.
    """
    pipe_ids = {}
    for name, fd in (("stdin", 0), ("stdout", 1), ("stderr", 2)):
        v = _pipe_id_from_proc(pid, fd)
        if not v:
            # not a pipe (maybe /dev/null or a tty) – we only support pipes in this demo
            raise RuntimeError(
                f"{name} (fd {fd}) is not an unnamed pipe; got {_readlink(Path('/proc') / str(pid) / 'fd' / str(fd))}"
            )
        pipe_ids[name] = v
    return pipe_ids


async def _pipe_ids_from_images(imgdir: Path) -> Dict[str, str]:
    """
    Fallback when the original process is gone (leave_running=False).
    Parse CRIU images with 'crit show' to map fd 0/1/2 -> pipe:[inode].
    We use CRIU’s CRIT tool (JSON) and look through fdinfo & files images.

    NOTE: image formats can evolve; this is a best-effort demo.
    """
    # Find the single leader's fdinfo image (fdinfo-<pid>.img).
    fdinfo_imgs = sorted(imgdir.glob("fdinfo-*.img"))
    _require(fdinfo_imgs, f"no fdinfo-*.img in {imgdir}")
    fdinfo_img = fdinfo_imgs[0]

    # 1) Map fd numbers -> CRIU "file id"
    rc, out, err = await _run(CRIT_BIN, "show", "-i", str(fdinfo_img), "--pretty")
    _require(rc == 0, f"crit show failed on {fdinfo_img}:\n{err or out}")
    fdinfo = json.loads(out)
    fd_to_id = {}
    for e in fdinfo.get("entries", []):
        fdnum = e.get("fd")
        fid = e.get("id") or e.get("file_id") or e.get("id_id")  # tolerate slight schema changes
        if fdnum is not None and fid is not None:
            fd_to_id[int(fdnum)] = fid

    needed_ids = [fd_to_id.get(0), fd_to_id.get(1), fd_to_id.get(2)]
    _require(all(needed_ids), "could not map fd 0/1/2 from fdinfo image")

    # 2) Resolve those file ids in files.img -> pipe inode
    files_img = imgdir / "files.img"
    rc, out, err = await _run(CRIT_BIN, "show", "-i", str(files_img), "--pretty")
    _require(rc == 0, f"crit show failed on {files_img}:\n{err or out}")
    files = json.loads(out)

    # CRIU usually records pipe entries with something like {"type":"PIPE","id":X,"pipe":"pipe:[INODE]"}.
    id_to_pipe = {}
    for e in files.get("entries", []):
        # Heuristics: look for a literal "pipe:[...]" string under plausible keys.
        for k, v in e.items():
            if isinstance(v, str) and v.startswith("pipe:["):
                # associate to its numeric id
                idval = e.get("id") or e.get("file_id") or e.get("ino_id")
                if idval is not None:
                    id_to_pipe[idval] = v

    stdin_pipe = id_to_pipe.get(needed_ids[0])
    stdout_pipe = id_to_pipe.get(needed_ids[1])
    stderr_pipe = id_to_pipe.get(needed_ids[2])
    _require(stdin_pipe and stdout_pipe and stderr_pipe, "failed to resolve pipe ids from images")

    return {"stdin": stdin_pipe, "stdout": stdout_pipe, "stderr": stderr_pipe}


# ---------- Public API ----------


async def freeze(pid: int, imgdir: str, leave_running: bool = True) -> Path:
    """
    Dump a running process with CRIU into imgdir and store stdio pipe IDs in py-meta.json.
    If leave_running=True, we keep the process alive (CRIU --leave-running).
    """
    images = Path(imgdir)
    images.mkdir(parents=True, exist_ok=True)

    args = [
        CRIU_BIN,
        "dump",
        "-D",
        str(images),
        "-t",
        str(pid),
        "-o",
        "dump.log",
        "-v4",
    ]
    if leave_running:
        args.append("--leave-running")

    # No TTY is used, so we shouldn't need --shell-job. See CRIU simple examples.
    # (If your target *does* have a controlling TTY, you’ll need --shell-job on dump+restore.)
    rc, out, err = await _sudo_run(*args)
    _require(rc == 0, f"criu dump failed (rc={rc})\nSTDOUT:\n{out}\nSTDERR:\n{err}")

    # Gather pipe identities for fd 0/1/2. Prefer /proc if still running; else parse with CRIT.
    if leave_running:
        pipe_ids = await _collect_stdio_pipe_ids(pid)
    else:
        pipe_ids = await _pipe_ids_from_images(images)

    meta = {
        "pid_dumped": pid,
        "pipe_ids": pipe_ids,
        "timestamp": time.time(),
        "criu_bin": _which_or_none(CRIU_BIN),
        "criu_ns": _which_or_none(CRIU_NS),
        "leave_running": leave_running,
    }
    (images / META_NAME).write_text(json.dumps(meta, indent=2))
    return images


@dataclass
class CloneHandle:
    pid: int  # host PID of the restored leader
    stdin: asyncio.StreamWriter
    stdout: asyncio.StreamReader
    stderr: asyncio.StreamReader
    imgdir: Path

    async def wait_for_exit(self) -> int:
        """
        Best-effort wait: since we didn't spawn the restored task via asyncio,
        we can poll /proc or just try reading until EOF from stdout/stderr.
        For demo simplicity we just wait until both stdout and stderr hit EOF.
        """

        # Drain readers to EOF (both).
        async def _drain(reader: asyncio.StreamReader):
            while True:
                chunk = await reader.read(1 << 16)
                if not chunk:
                    return

        await asyncio.gather(_drain(self.stdout), _drain(self.stderr))
        # Then poll /proc/<pid>
        for _ in range(100):
            if not Path("/proc") / str(self.pid).exists():
                return 0
            await asyncio.sleep(0.05)
        return 0


async def _make_reader_from_fd(fd: int) -> asyncio.StreamReader:
    loop = asyncio.get_running_loop()
    reader = asyncio.StreamReader()
    protocol = asyncio.StreamReaderProtocol(reader)
    rfile = os.fdopen(fd, "rb", buffering=0, closefd=False)
    await loop.connect_read_pipe(lambda: protocol, rfile)
    return reader


async def _make_writer_from_fd(fd: int) -> asyncio.StreamWriter:
    """
    Create an asyncio StreamWriter bound to a writable file descriptor.
    Pattern: connect_write_pipe + wrap transport in StreamWriter.
    """
    loop = asyncio.get_running_loop()
    wfile = os.fdopen(fd, "wb", buffering=0, closefd=False)
    transport, protocol = await loop.connect_write_pipe(asyncio.streams.FlowControlMixin, wfile)
    return asyncio.StreamWriter(transport, protocol, None, loop)


async def clone(imgdir: str, *, log_name: Optional[str] = None) -> CloneHandle:
    """
    Restore a clone from imgdir. Creates three anonymous pipes and maps them
    to the stdio pipes recorded at freeze() using --inherit-fd.

    Returns a CloneHandle exposing asyncio streams for stdin/stdout/stderr.
    """
    images = Path(imgdir)
    meta = json.loads((images / META_NAME).read_text()) if (images / META_NAME).exists() else {}
    if "pipe_ids" not in meta:
        # Fallback if py-meta.json not present (e.g., another tool created the image)
        pipe_ids = await _pipe_ids_from_images(images)
    else:
        pipe_ids = meta["pipe_ids"]

    # Create three new anonymous pipes:
    #  - For child's stdin: pass the READ end to CRIU; keep WRITE end.
    #  - For child's stdout/stderr: pass the WRITE ends to CRIU; keep READ ends.
    r_stdin, w_stdin = os.pipe()
    r_stdout, w_stdout = os.pipe()
    r_stderr, w_stderr = os.pipe()

    # We’ll pass these end FDs into criu so they become the new stdio in the restored process.
    inherit_map = {
        r_stdin: pipe_ids["stdin"],  # child's fd 0 will be restored from this
        w_stdout: pipe_ids["stdout"],  # child's fd 1
        w_stderr: pipe_ids["stderr"],  # child's fd 2
    }

    # Build --inherit-fd arguments like: --inherit-fd fd[<FD>]:pipe:[INODE]
    inherit_args: List[str] = []
    for fdnum, pipe_spec in inherit_map.items():
        inherit_args += ["--inherit-fd", f"fd[{fdnum}]:{pipe_spec}"]

    # Prepare restore command (prefer criu-ns to avoid PID clashes)
    restore_log = images / (log_name or f"restore.{int(time.time())}.log")
    pidfile = images / f"clone.{int(time.time())}.pid"

    # We'll detach CRIU itself (-d). After it returns, the clone runs in its own pidns.
    if _which_or_none(CRIU_NS):
        cmd = [
            CRIU_NS,
            "restore",
            "-D",
            str(images),
            "-o",
            str(restore_log),
            "-d",
            "--pidfile",
            str(pidfile),
        ] + inherit_args
        runner = _sudo_run
    else:
        # Manual namespace dance as a fallback.
        cmd = [
            "unshare",
            "-p",
            "-m",
            "--fork",
            "--mount-proc",
            CRIU_BIN,
            "restore",
            "-D",
            str(images),
            "-o",
            str(restore_log),
            "-d",
            "--pidfile",
            str(pidfile),
        ] + inherit_args
        runner = _sudo_run

    # Ensure CRIU sees these FDs open; keep all ends inheritable for the child process
    pass_fds = list(inherit_map.keys())

    # Launch restore (synchronously, but detached from the clone).
    rc, out, err = await runner(*cmd, pass_fds=pass_fds)
    _require(rc == 0, f"restore failed (rc={rc})\nCMD: {shlex.join(cmd)}\nSTDOUT:\n{out}\nSTDERR:\n{err}")

    # Close the ends we handed to CRIU; we keep only our sides.
    os.close(r_stdin)
    os.close(w_stdout)
    os.close(w_stderr)

    # Wait for pidfile to appear and contain a PID
    deadline = time.time() + 5.0
    while time.time() < deadline:
        if pidfile.exists():
            try:
                pid = int(pidfile.read_text().strip())
                break
            except Exception:
                pass
        await asyncio.sleep(0.02)
    else:
        raise RuntimeError(f"Timed out waiting for --pidfile {pidfile}; check {restore_log}")

    # Wrap parent ends as asyncio streams
    stdin_writer = await _make_writer_from_fd(w_stdin)
    stdout_reader = await _make_reader_from_fd(r_stdout)
    stderr_reader = await _make_reader_from_fd(r_stderr)

    return CloneHandle(pid=pid, stdin=stdin_writer, stdout=stdout_reader, stderr=stderr_reader, imgdir=images)


# ---------- Demo harness ----------


async def _demo():
    """
    Demo flow:
      1) start a trivial child that echoes lines back (pure PIPE stdio, no TTY)
      2) freeze it to ./demo_images (leave_running=False just to show the fallback)
      3) create three clones; talk to them independently
    """
    print("Starting demo child (no TTY, PIPE stdio)...")
    # A tiny echo program: read lines, prefix with PID, flush
    child = await asyncio.create_subprocess_exec(
        sys.executable,
        "-u",
        "-c",
        "import os,sys; "
        "import sys; "
        "pid=os.getpid(); "
        "print(f'ECHO online (pid={pid})', flush=True); "
        "import sys "
        ";\n"
        "import sys\n"
        "import time\n"
        "import sys\n"
        "from sys import stdin\n"
        "import sys\n"
        "import sys\n"
        "import sys\n"
        "import sys\n"
        "for line in sys.stdin:\n"
        "    sys.stdout.write(f'[{pid}] ' + line)\n"
        "    sys.stdout.flush()\n",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    # Prime it
    await child.stdout.readline()

    imgdir = Path("./demo_images").resolve()
    print(f"Freezing pid={child.pid} into {imgdir} ...")
    await freeze(child.pid, str(imgdir), leave_running=False)

    # The original exited because leave_running=False; now create 3 clones
    clones: List[CloneHandle] = []
    for i in range(3):
        c = await clone(str(imgdir), log_name=f"restore.clone{i + 1}.log")
        clones.append(c)
        print(f"Clone {i + 1}: host PID {c.pid}")

    # Talk to clones independently
    print("Talking to clones...")
    for i, c in enumerate(clones, 1):
        c.stdin.write(f"hello from parent -> clone{i}\n".encode())
        await c.stdin.drain()

    # Read one line from each clone
    for i, c in enumerate(clones, 1):
        line = await c.stdout.readline()
        print(f"From clone{i} (pid={c.pid}): {line.decode().rstrip()}")

    # (optional) finish clones
    for c in clones:
        try:
            c.stdin.write_eof()
        except Exception:
            pass

    # Give them a moment to exit cleanly
    await asyncio.sleep(0.2)
    print("Demo done.")


if __name__ == "__main__":
    # Run the demo if invoked directly; otherwise, import freeze/clone in your code.
    try:
        asyncio.run(_demo())
    except KeyboardInterrupt:
        pass
