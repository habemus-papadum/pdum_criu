"""Helpers for locating CRIU-related executables on the system."""

from __future__ import annotations

import logging
import os
import re
import subprocess
import sys
import textwrap
from collections import deque
from pathlib import Path
from shutil import which
from typing import Any

__all__ = [
    "resolve_command",
    "ensure_sudo",
    "ensure_criu",
    "ensure_criu_ns",
    "ensure_pgrep",
    "psgrep",
    "ensure_linux",
    "resolve_target_pid",
    "tail_file",
    "spawn_directory_cleanup",
    "ensure_sudo_closefrom",
    "check_sudo_closefrom",
]

_ENV_PREFIX = "PDUM_CRIU_"
_SUDO_CLOSEFROM_SUPPORTED: bool | None = None
_SUDO_CLOSEFROM_ERROR: str | None = None

logger = logging.getLogger(__name__)
logger.addHandler(logging.NullHandler())


def resolve_command(executable: str) -> str:
    """
    Resolve a supported command to a concrete executable path.

    The resolver first checks ``PDUM_CRIU_<EXE>`` for an override (where ``<EXE>``
    is the capitalized executable name with non-alphanumerics replaced by
    underscores) before falling back to ``shutil.which``.

    Parameters
    ----------
    executable : str
        Default executable name to locate. Can be overridden via environment.

    Returns
    -------
    str
        Absolute path to the resolved executable.

    Raises
    ------
    ValueError
        If ``executable`` is empty.
    FileNotFoundError
        If the executable cannot be located.
    """

    if not executable or executable.strip() == "":
        raise ValueError("Executable name must be a non-empty string.")

    default_executable = executable.strip()
    env_var = _env_var_name(default_executable)
    override = os.environ.get(env_var, "").strip()
    candidate = override or default_executable

    resolved = which(candidate)
    if resolved:
        return resolved

    raise FileNotFoundError(
        f"Unable to locate executable for {default_executable!r} "
        f"(checked {candidate!r}, override via {env_var})."
    )


def _env_var_name(executable: str) -> str:
    sanitized = re.sub(r"[^A-Z0-9]+", "_", executable.upper())
    return f"{_ENV_PREFIX}{sanitized}"


def ensure_sudo(*, verbose: bool = True, raise_: bool = False, **kwargs: Any) -> bool:
    """
    Ensure ``sudo -n true`` succeeds on the current system.

    Returns
    -------
    bool
        True if the non-interactive sudo command exits with status 0, otherwise False.
    """

    raise_flag = _pop_raise_flag(kwargs, raise_)

    try:
        sudo_cmd = resolve_command("sudo")
        true_cmd = resolve_command("true")
    except (FileNotFoundError, ValueError) as exc:
        message = f"Unable to locate sudo/true commands: {exc}"
        if verbose:
            logger.warning(message)
        if raise_flag:
            raise RuntimeError(message) from exc
        return False

    try:
        result = subprocess.run(
            [sudo_cmd, "-n", true_cmd],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except OSError as exc:
        message = f"Failed to execute sudo: {exc}"
        if verbose:
            logger.warning(message)
        if raise_flag:
            raise RuntimeError(message) from exc
        return False

    if result.returncode == 0:
        return True

    user = os.environ.get("USER", "your-user")
    message = (
        "Password-less sudo is required to continue.\n"
        f"Tip: run 'sudo visudo' and add '{user} ALL=(ALL) NOPASSWD:ALL'."
    )
    if verbose:
        logger.warning(message)

    if raise_flag:
        raise RuntimeError("Password-less sudo is required for pdum-criu operations.")
    return False


def ensure_criu(*, verbose: bool = True, raise_: bool = False, **kwargs: Any) -> str | None:
    """Ensure the ``criu`` executable is available."""

    raise_flag = _pop_raise_flag(kwargs, raise_)
    return _ensure_tool(
        "criu",
        "Install CRIU on Ubuntu with 'sudo apt update && sudo apt install -y criu'.",
        verbose=verbose,
        raise_flag=raise_flag,
    )


def ensure_criu_ns(*, verbose: bool = True, raise_: bool = False, **kwargs: Any) -> str | None:
    """Ensure the ``criu-ns`` helper is available."""

    raise_flag = _pop_raise_flag(kwargs, raise_)
    return _ensure_tool(
        "criu-ns",
        "Install the CRIU tools on Ubuntu with 'sudo apt install -y criu'.",
        verbose=verbose,
        raise_flag=raise_flag,
    )


def ensure_pgrep(*, verbose: bool = True, raise_: bool = False, **kwargs: Any) -> str | None:
    """Ensure the ``pgrep`` utility is available."""

    raise_flag = _pop_raise_flag(kwargs, raise_)
    return _ensure_tool(
        "pgrep",
        "Install pgrep via the procps package on Ubuntu: 'sudo apt install -y procps'.",
        verbose=verbose,
        raise_flag=raise_flag,
    )


def psgrep(query: str, *, ensure_unique: bool = True) -> int | list[int]:
    """
    Locate processes matching the supplied query using ``pgrep -f``.

    Parameters
    ----------
    query : str
        Pattern passed to ``pgrep -f``. Supports spaces (matches full command line).
    ensure_unique : bool, optional
        When True, ensure exactly one PID is returned; otherwise, return all matches.

    Returns
    -------
    int | list[int]
        PID of the unique match, or a list of PIDs when ``ensure_unique`` is False.

    Raises
    ------
    ValueError
        If ``query`` is empty.
    RuntimeError
        If no processes match, multiple matches are found while ``ensure_unique`` is
        True, or ``pgrep`` fails.
    """

    if not query or query.strip() == "":
        raise ValueError("Query must be a non-empty string.")

    pgrep_cmd = ensure_pgrep(verbose=False, raise_=True)
    result = subprocess.run(
        [pgrep_cmd, "-f", query],
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )

    if result.returncode == 1:
        raise RuntimeError(f"No processes matched query {query!r}.")
    if result.returncode not in (0, 1):
        stderr = result.stderr.strip()
        raise RuntimeError(f"pgrep failed with exit code {result.returncode}: {stderr}")

    pids = [int(line) for line in result.stdout.splitlines() if line.strip()]
    if not pids:
        raise RuntimeError(f"No processes matched query {query!r}.")

    if ensure_unique:
        if len(pids) > 1:
            raise RuntimeError(
                f"Expected a single process for {query!r}, found {len(pids)} matches: {pids}"
            )
        return pids[0]

    return pids


def _ensure_tool(executable: str, instructions: str, *, verbose: bool, raise_flag: bool) -> str | None:
    try:
        return resolve_command(executable)
    except (FileNotFoundError, ValueError) as exc:
        message = (
            f"{executable} not found: {exc}. {instructions} "
            f"Override via {_env_var_name(executable)} if installed elsewhere."
        )
        if verbose:
            logger.warning(message)
        if raise_flag:
            raise RuntimeError(message) from exc
        return None


def _pop_raise_flag(kwargs: dict[str, Any], default: bool) -> bool:
    """Extract the ``raise`` flag from kwargs (supports ``raise_=`` alias)."""

    raise_flag = bool(default)
    if "raise" in kwargs:
        raise_flag = bool(kwargs.pop("raise"))
    if "raise_" in kwargs:
        raise_flag = bool(kwargs.pop("raise_"))
    if kwargs:
        unexpected = ", ".join(kwargs)
        raise TypeError(f"Unexpected keyword arguments: {unexpected}")
    return raise_flag


def ensure_linux() -> None:
    """Raise if the current platform is not Linux."""

    if not sys.platform.startswith("linux"):
        raise RuntimeError(f"CRIU workflows require Linux (detected {sys.platform}).")


def resolve_target_pid(pid: int | None, pattern: str | None) -> int:
    """Resolve a target PID either directly or via ``pgrep``."""

    if pid is not None and pattern is not None:
        raise ValueError("Specify either --pid or --pgrep, not both.")
    if pid is None and pattern is None:
        raise ValueError("Either --pid or --pgrep is required.")
    if pid is not None:
        if pid <= 0:
            raise ValueError("PID must be a positive integer.")
        return pid
    assert pattern is not None
    resolved = psgrep(pattern, ensure_unique=True)
    if isinstance(resolved, list):
        raise RuntimeError("resolve_target_pid expected a single PID result.")
    return resolved


def tail_file(path: Path, lines: int = 10) -> str:
    """Return the last ``lines`` lines from ``path``."""

    if not path.exists():
        return ""

    recent: deque[str] = deque(maxlen=lines)
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        for line in handle:
            recent.append(line.rstrip("\n"))
    return "\n".join(recent)


def spawn_directory_cleanup(path: Path, watched_pid: int) -> None:
    """
    Spawn a background helper that removes ``path`` when ``watched_pid`` exits.
    """

    script = textwrap.dedent(
        """
        import os
        import shutil
        import sys
        import time

        target = sys.argv[1]
        watched = int(sys.argv[2])

        while True:
            try:
                os.kill(watched, 0)
            except OSError:
                break
            time.sleep(0.5)

        try:
            shutil.rmtree(target)
        except FileNotFoundError:
            pass
        """
    )

    subprocess.Popen(
        [sys.executable, "-c", script, os.fspath(path), str(watched_pid)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
    )


def ensure_sudo_closefrom() -> None:
    """
    Verify that ``sudo`` supports the ``-C`` flag (closefrom_override).
    """

    global _SUDO_CLOSEFROM_SUPPORTED, _SUDO_CLOSEFROM_ERROR

    if _SUDO_CLOSEFROM_SUPPORTED:
        return
    if _SUDO_CLOSEFROM_SUPPORTED is False:
        raise RuntimeError(_SUDO_CLOSEFROM_ERROR or "sudo closefrom_override is not enabled.")

    sudo_path = resolve_command("sudo")
    result = subprocess.run([sudo_path, "-n", "-C", "32", "true"], capture_output=True, text=True)
    if result.returncode == 0:
        _SUDO_CLOSEFROM_SUPPORTED = True
        _SUDO_CLOSEFROM_ERROR = None
        return

    detail = (result.stderr or result.stdout or "sudo rejected -C").strip()
    _SUDO_CLOSEFROM_SUPPORTED = False
    _SUDO_CLOSEFROM_ERROR = (
        "sudo is not configured with closefrom_override; enable it via `sudo visudo` "
        "(add `Defaults    closefrom_override` or `Defaults:YOURUSER    closefrom_override`). "
        f"Original sudo output: {detail}"
    )
    raise RuntimeError(_SUDO_CLOSEFROM_ERROR)


def check_sudo_closefrom() -> tuple[bool, str | None]:
    """
    Probe ``sudo`` for closefrom_override support without raising.
    """

    try:
        ensure_sudo_closefrom()
    except RuntimeError as exc:
        return False, str(exc)
    return True, None


def doctor_check_results(verbose: bool = True) -> list[tuple[str, bool, str | None]]:
    """Run the same checks as the CLI doctor command.

    Returns a list of ``(label, ok, message)`` tuples.
    """

    checks = [
        ("Password-less sudo", ensure_sudo),
        ("CRIU", ensure_criu),
        ("CRIU-ns", ensure_criu_ns),
        ("pgrep", ensure_pgrep),
    ]

    results: list[tuple[str, bool, str | None]] = []
    for label, checker in checks:
        try:
            ok = bool(checker(verbose=verbose))
            results.append((label, ok, None))
        except Exception as exc:  # pragma: no cover - best-effort reporting
            results.append((label, False, str(exc)))

    closefrom_ok, closefrom_msg = check_sudo_closefrom()
    results.append(("sudo closefrom_override", closefrom_ok, closefrom_msg))
    return results
