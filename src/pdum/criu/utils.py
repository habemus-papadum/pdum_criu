"""Helpers for locating CRIU-related executables on the system."""

from __future__ import annotations

import os
import re
import subprocess
from shutil import which

from rich import print as rich_print

__all__ = ["resolve_command", "ensure_sudo", "ensure_criu", "ensure_criu_ns", "ensure_pgrep"]

_ENV_PREFIX = "PDUM_CRIU_"


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


def ensure_sudo() -> bool:
    """
    Ensure ``sudo -n true`` succeeds on the current system.

    Returns
    -------
    bool
        True if the non-interactive sudo command exits with status 0, otherwise False.
    """

    try:
        sudo_cmd = resolve_command("sudo")
        true_cmd = resolve_command("true")
    except (FileNotFoundError, ValueError) as exc:
        rich_print(f"[bold red]Unable to locate sudo/true commands:[/] {exc}")
        return False

    try:
        result = subprocess.run(
            [sudo_cmd, "-n", true_cmd],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except OSError as exc:
        rich_print(f"[bold red]Failed to execute sudo:[/] {exc}")
        return False

    if result.returncode == 0:
        return True

    user = os.environ.get("USER", "your-user")
    rich_print(
        "[bold red]Password-less sudo is required to continue.[/]\n"
        "[bold yellow]Tip:[/] run [bold cyan]sudo visudo[/] and add the following line:\n"
        f"    [green]{user} ALL=(ALL) NOPASSWD:ALL[/]"
    )
    return False


def ensure_criu() -> str | None:
    """Ensure the ``criu`` executable is available."""

    return _ensure_tool(
        "criu",
        "Install CRIU on Ubuntu with [bold cyan]sudo apt update && sudo apt install -y criu[/].",
    )


def ensure_criu_ns() -> str | None:
    """Ensure the ``criu-ns`` helper is available."""

    return _ensure_tool(
        "criu-ns",
        "Install the CRIU tools on Ubuntu with [bold cyan]sudo apt install -y criu[/].",
    )


def ensure_pgrep() -> str | None:
    """Ensure the ``pgrep`` utility is available."""

    return _ensure_tool(
        "pgrep",
        "Install pgrep via the procps package on Ubuntu: [bold cyan]sudo apt install -y procps[/].",
    )


def _ensure_tool(executable: str, instructions: str) -> str | None:
    try:
        return resolve_command(executable)
    except (FileNotFoundError, ValueError) as exc:
        rich_print(
            f"[bold red]{executable} not found:[/] {exc}\n"
            f"{instructions}\n"
            f"Override via [bold yellow]{_env_var_name(executable)}[/] if installed elsewhere."
        )
        return None
