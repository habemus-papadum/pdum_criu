"""Command-line entry point for pdum-criu utilities."""

from __future__ import annotations

import os
import shlex
import sys
import tempfile
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console

from . import __version__, utils

app = typer.Typer(
    help="Utilities for freezing and thawing processes via CRIU.",
)
shell_app = typer.Typer(help="Shell helpers for CRIU workflows.")
console = Console()

app.add_typer(shell_app, name="shell", help="Manage shell-based freeze/thaw operations.")


@app.callback(invoke_without_command=True)
def main_callback(ctx: typer.Context) -> None:
    """Show help when no subcommand is provided."""
    if ctx.invoked_subcommand is None:
        typer.echo(ctx.get_help())
        raise typer.Exit()


@shell_app.callback(invoke_without_command=True)
def shell_callback(ctx: typer.Context) -> None:
    """Show help for shell sub-commands when none are provided."""
    if ctx.invoked_subcommand is None:
        typer.echo(ctx.get_help())
        raise typer.Exit()


@app.command("version")
def version_command() -> None:
    """Display version information."""
    console.print(
        "[bold green]pdum-criu[/] CLI\n"
        f"[bold cyan]version:[/] {__version__}"
    )


@shell_app.command("freeze")
def shell_freeze(
    images_dir: Path = typer.Option(..., "--dir", "-d", help="Directory that will contain the CRIU image set."),
    pid: Optional[int] = typer.Option(None, "--pid", help="PID to freeze."),
    pgrep: Optional[str] = typer.Option(None, "--pgrep", help="pgrep pattern to resolve the PID."),
    log_file: Optional[Path] = typer.Option(
        None,
        "--log-file",
        "-l",
        help="Optional log file override (default: freeze.<pid>.log inside the image dir).",
    ),
    verbosity: int = typer.Option(4, "--verbosity", "-v", min=0, max=4, help="CRIU verbosity level (0-4)."),
    leave_running: bool = typer.Option(
        True,
        "--leave-running/--no-leave-running",
        help="Keep the target process running after dump completes.",
    ),
    show_command: bool = typer.Option(
        True,
        "--show-command/--hide-command",
        help="Print the CRIU command before executing it.",
    ),
) -> None:
    """Freeze a running shell/job into a CRIU image directory."""

    try:
        utils.ensure_linux()
    except RuntimeError as exc:
        console.print(f"[bold red]{exc}[/]")
        raise typer.Exit(code=1)

    target_pid = _resolve_pid_option(pid, pgrep)
    utils.ensure_sudo(verbose=True, raise_=True)
    utils.ensure_pgrep(verbose=True, raise_=True)
    criu_path = utils.ensure_criu(verbose=True, raise_=True)
    if not criu_path:
        raise typer.Exit(code=1)

    sudo_path = utils.resolve_command("sudo")
    images_dir = _prepare_dir(images_dir)
    log_path = _resolve_log_path(log_file, images_dir, f"freeze.{target_pid}.log")

    command = _build_criu_dump_command(
        sudo_path,
        criu_path,
        images_dir,
        target_pid,
        log_path,
        verbosity,
        leave_running,
    )

    console.print(f"[bold cyan]Freezing PID {target_pid} into {images_dir}[/]")
    exit_code = _run_command(command, show=show_command)
    if exit_code == 0:
        console.print(f"[bold green]Freeze complete.[/] Log: {log_path}")
        return

    tail = utils.tail_file(log_path, lines=10)
    console.print(f"[bold red]Freeze failed (exit {exit_code}).[/]")
    if tail:
        console.print("[bold yellow]Log tail:[/]")
        console.print(tail)
    raise typer.Exit(code=exit_code)


@shell_app.command("thaw")
def shell_thaw(
    images_dir: Path = typer.Option(..., "--dir", "-d", help="CRIU image directory to restore."),
    show_command: bool = typer.Option(
        True,
        "--show-command/--hide-command",
        help="Print the CRIU command before executing it.",
    ),
) -> None:
    """Restore a shell/job from a CRIU image directory."""

    try:
        utils.ensure_linux()
    except RuntimeError as exc:
        console.print(f"[bold red]{exc}[/]")
        raise typer.Exit(code=1)

    images_dir = images_dir.expanduser().resolve()
    if not images_dir.exists():
        console.print(f"[bold red]Image directory does not exist:[/] {images_dir}")
        raise typer.Exit(code=1)

    exit_code, initial_log, final_log, _ = _execute_restore(images_dir, show_command=show_command)
    effective_log = final_log if final_log.exists() else initial_log
    if exit_code == 0:
        console.print(f"[bold green]Restore complete.[/] Log: {effective_log}")
        return

    console.print(f"[bold red]Restore failed (exit {exit_code}).[/]")
    tail = utils.tail_file(effective_log, lines=10)
    if tail:
        console.print("[bold yellow]Log tail:[/]")
        console.print(tail)
    raise typer.Exit(code=exit_code)


@shell_app.command("beam")
def shell_beam(
    images_dir: Optional[Path] = typer.Option(
        None,
        "--dir",
        "-d",
        help="Optional directory for the CRIU image set (defaults to a temp dir).",
    ),
    pid: Optional[int] = typer.Option(None, "--pid", help="PID to beam."),
    pgrep: Optional[str] = typer.Option(None, "--pgrep", help="pgrep pattern to resolve the PID."),
    log_file: Optional[Path] = typer.Option(
        None,
        "--log-file",
        "-l",
        help="Optional log file override for the freeze phase.",
    ),
    verbosity: int = typer.Option(4, "--verbosity", "-v", min=0, max=4, help="CRIU verbosity level."),
    leave_running: bool = typer.Option(
        True,
        "--leave-running/--no-leave-running",
        help="Keep the target process running after dump completes.",
    ),
    cleanup: bool = typer.Option(
        True,
        "--cleanup/--no-cleanup",
        help="Remove the image directory once the beamed process exits.",
    ),
    show_command: bool = typer.Option(
        True,
        "--show-command/--hide-command",
        help="Print each CRIU command before executing it.",
    ),
) -> None:
    """Freeze then immediately thaw a shell, cleaning up artifacts afterwards."""

    try:
        utils.ensure_linux()
    except RuntimeError as exc:
        console.print(f"[bold red]{exc}[/]")
        raise typer.Exit(code=1)

    target_pid = _resolve_pid_option(pid, pgrep)
    utils.ensure_sudo(verbose=True, raise_=True)
    utils.ensure_pgrep(verbose=True, raise_=True)
    criu_path = utils.ensure_criu(verbose=True, raise_=True)
    if not criu_path:
        raise typer.Exit(code=1)

    sudo_path = utils.resolve_command("sudo")

    if images_dir is None:
        images_dir = Path(tempfile.mkdtemp(prefix="pdum-criu-beam-"))
        console.print(f"[bold cyan]Beam images directory:[/] {images_dir}")
    else:
        images_dir = _prepare_dir(images_dir)

    log_path = _resolve_log_path(log_file, images_dir, f"freeze.{target_pid}.log")

    command = _build_criu_dump_command(
        sudo_path,
        criu_path,
        images_dir,
        target_pid,
        log_path,
        verbosity,
        leave_running,
    )

    console.print(f"[bold cyan]Freezing PID {target_pid} before beam[/]")
    exit_code = _run_command(command, show=show_command)
    if exit_code != 0:
        tail = utils.tail_file(log_path, lines=10)
        console.print(f"[bold red]Beam freeze failed (exit {exit_code}).[/]")
        if tail:
            console.print("[bold yellow]Log tail:[/]")
            console.print(tail)
        raise typer.Exit(code=exit_code)

    console.print(f"[bold cyan]Thawing beam image from {images_dir}[/]")
    restore_exit, initial_log, final_log, thaw_pid = _execute_restore(images_dir, show_command=show_command)
    effective_log = final_log if final_log.exists() else initial_log
    if restore_exit != 0:
        console.print(f"[bold red]Beam restore failed (exit {restore_exit}).[/]")
        tail = utils.tail_file(effective_log, lines=10)
        if tail:
            console.print("[bold yellow]Log tail:[/]")
            console.print(tail)
        raise typer.Exit(code=restore_exit)

    if cleanup:
        watcher_pid = thaw_pid or os.getpid()
        utils.spawn_directory_cleanup(images_dir, watcher_pid)

    console.print(f"[bold green]Beam complete.[/] Restore log: {effective_log}")


@app.command("doctor")
def doctor() -> None:
    """Run environment checks to confirm required executables are present."""
    if not sys.platform.startswith("linux"):
        console.print(
            f"[bold red]pdum-criu doctor only supports Linux hosts (detected: {sys.platform}).[/]\n"
            "CRIU depends on Linux kernel checkpoint/restore features."
        )
        raise typer.Exit(code=1)

    console.print("[bold cyan]Running environment diagnostics...[/]")

    checks = [
        ("Password-less sudo", utils.ensure_sudo),
        ("CRIU", utils.ensure_criu),
        ("pgrep", utils.ensure_pgrep),
    ]

    all_ok = True
    for label, checker in checks:
        try:
            ok = bool(checker(verbose=True))
        except Exception as exc:  # pragma: no cover - guard rail
            ok = False
            console.print(f"[bold red]✗ {label}[/] - {exc}")
        else:
            if ok:
                console.print(f"[bold green]✓ {label}[/]")
            else:
                console.print(f"[bold red]✗ {label}[/]")
        all_ok = all_ok and ok

    if all_ok:
        console.print("[bold green]All doctor checks passed![/]")
    else:
        console.print("[bold yellow]Resolve the failed checks above before continuing.[/]")


def _resolve_pid_option(pid: Optional[int], pgrep: Optional[str]) -> int:
    try:
        return utils.resolve_target_pid(pid, pgrep)
    except ValueError as exc:
        raise typer.BadParameter(str(exc))
    except RuntimeError as exc:
        console.print(f"[bold red]{exc}[/]")
        raise typer.Exit(code=1)


def _prepare_dir(path: Path) -> Path:
    resolved = path.expanduser().resolve()
    resolved.mkdir(parents=True, exist_ok=True)
    return resolved


def _resolve_log_path(candidate: Optional[Path], base_dir: Path, default_name: str) -> Path:
    if candidate is None:
        path = base_dir / default_name
    else:
        path = candidate.expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _build_criu_dump_command(
    sudo_path: str,
    criu_path: str,
    images_dir: Path,
    pid: int,
    log_path: Path,
    verbosity: int,
    leave_running: bool,
) -> list[str]:
    command = [
        sudo_path,
        "-n",
        criu_path,
        "dump",
        "-D",
        str(images_dir),
        "-t",
        str(pid),
        "-o",
        str(log_path),
        f"-v{verbosity}",
        "--shell-job",
    ]
    if leave_running:
        command.append("--leave-running")
    return command


def _create_temp_log(base_dir: Path, prefix: str) -> Path:
    handle = tempfile.NamedTemporaryFile(prefix=f"{prefix}.", suffix=".log", dir=os.fspath(base_dir), delete=False)
    temp_name = handle.name
    handle.close()
    return Path(temp_name)


def _build_criu_restore_command(
    sudo_path: str,
    criu_ns_path: str,
    images_dir: Path,
    log_path: Path,
    pidfile: Path,
) -> list[str]:
    return [
        sudo_path,
        "-n",
        criu_ns_path,
        "restore",
        "-D",
        str(images_dir),
        "--shell-job",
        "-o",
        str(log_path),
        "--pidfile",
        str(pidfile),
    ]


def _run_command(command: list[str], *, show: bool) -> int:
    rendered = shlex.join(command)
    if show:
        console.print(f"[bold magenta]$ {rendered}[/]")

    result = os.system(rendered)
    if os.WIFEXITED(result):
        return os.WEXITSTATUS(result)
    if os.WIFSIGNALED(result):
        return -os.WTERMSIG(result)
    return result


def _execute_restore(images_dir: Path, *, show_command: bool) -> tuple[int, Path, Path, Optional[int]]:
    utils.ensure_sudo(verbose=True, raise_=True)
    criu_ns_path = utils.ensure_criu_ns(verbose=True, raise_=True)
    if not criu_ns_path:
        raise typer.Exit(code=1)

    sudo_path = utils.resolve_command("sudo")
    log_path = _create_temp_log(images_dir, prefix="restore")
    pidfile = images_dir / "thaw.pid"
    if pidfile.exists():
        pidfile.unlink()
    utils.spawn_log_renamer(log_path, pidfile)

    command = _build_criu_restore_command(sudo_path, criu_ns_path, images_dir, log_path, pidfile)
    exit_code = _run_command(command, show=show_command)
    thaw_pid = _read_pidfile(pidfile)
    final_log = log_path.with_name(f"thaw.{thaw_pid}.log") if thaw_pid else log_path
    return exit_code, log_path, final_log, thaw_pid


def _read_pidfile(pidfile: Path) -> Optional[int]:
    if not pidfile.exists():
        return None
    try:
        content = pidfile.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if not content:
        return None
    try:
        return int(content)
    except ValueError:
        return None
