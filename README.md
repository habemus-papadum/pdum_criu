# criu

[![CI](https://github.com/habemus-papadum/pdum_criu/actions/workflows/ci.yml/badge.svg)](https://github.com/habemus-papadum/pdum_criu/actions/workflows/ci.yml)
[![Coverage](https://raw.githubusercontent.com/habemus-papadum/pdum_criu/python-coverage-comment-action-data/badge.svg)](https://htmlpreview.github.io/?https://github.com/habemus-papadum/pdum_criu/blob/python-coverage-comment-action-data/htmlcov/index.html)
[![PyPI](https://img.shields.io/pypi/v/habemus-papadum-criu.svg)](https://pypi.org/project/habemus-papadum-criu/)
[![Python 3.12+](https://img.shields.io/badge/python-3.12+-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Code style: ruff](https://img.shields.io/badge/code%20style-ruff-000000.svg)](https://github.com/astral-sh/ruff)

Freeze, thaw, & mulitply goblins using [CRIU](https://criu.org/Main_Page)

Check out the [**Tutorial notebook**](https://habemus-papadum.github.io/pdum_criu/goblins/) or learn the CLI with  
`uvx --from habemus-papadum-criu pdum-criu --help`

> **Caveat Emptor**  
> This toolkit drives `sudo`, `criu`, and friends like it stole them. If you don’t personally trust @habemus-papadum, the PyPI supply chain,  and everything else in the loop, you probably shouldn’t hand it a root prompt. Treat it like the loaded power tool it is.

## 🧙‍♂️ Goblins

__Small creatures that live in pipes.__

Goblins are minimal, self-contained processes that are in continuous dialogue with the outside world only through standard input, standard output, and standard error. They don’t need sockets, frameworks, or APIs — just a stream in, a stream out, and a place to mutter when things go wrong.

This package aims to make Linux user-space checkpoint/restore (via CRIU) feel high-level: freeze a running “goblin” process without disturbing it, then replay from that checkpoint as many times as you like. 

But, realistically, it’s a toolkit for folks already comfortable with Linux internals — high level helpers, but still aimed at power users who understand what CRIU is doing under the hood -- things will break or just not work, and you will need to spelunk to sort it.

Inside, goblins can be as clever as they like: they can spawn threads, map files into memory, make background HTTP requests, or run background jobs. None of that changes their essence. What defines a goblin is not how it thinks, but how it speaks — through the ancient UNIX tongue of stdin, stdout, and stderr.

This simplicity makes goblins easy to checkpoint, serialize, and resurrect (e.g., with CRIU). When you bring a goblin back to life, you only need to restore its three pipes — its ears, its mouth, and its voice. Everything else is internal mischief.


## Installation
```bash
pip install habemus-papadum-criu
```

<details>
<summary><strong>Note (Ubuntu 24.04, November 2025)</strong></summary>

CRIU packages are not published in the default Ubuntu 24.04 (Noble) repositories. Install the CRIU PPA manually before running the CLI or doctor:

```bash
echo 'deb http://download.opensuse.org/repositories/devel:/tools:/criu/xUbuntu_24.04/ /' | sudo tee /etc/apt/sources.list.d/devel:tools:criu.list
curl -fsSL https://download.opensuse.org/repositories/devel:tools:/criu/xUbuntu_24.04/Release.key | gpg --dearmor | sudo tee /etc/apt/trusted.gpg.d/devel_tools_criu.gpg > /dev/null
sudo apt update
sudo apt install criu
```

</details>

### Check System Capability

```bash
uvx --from habemus-papadum-criu pdum-criu doctor
```
Prints a green/red summary so you can fix your environment.


### CLI commands

- `pdum-criu shell freeze`: snapshot a running PID/pgrep match into a CRIU image directory.
- `pdum-criu shell thaw`: restore a previously frozen image set.
- `pdum-criu shell beam`: freeze then immediately thaw (beam) a target shell.
- `pdum-criu doctor`: check sudo/CRIU/pgrep availability before running anything.

Run `pdum-criu <command> --help` for full options and examples.

## Goblins API

API usage starts with `pdum.criu.goblins.freeze(pid, images_dir, leave_running=True)` to checkpoint a goblin process, and `pdum.criu.goblins.thaw(...)` / `thaw_async(...)` to reconnect to it with fresh stdin/stdout/stderr pipes. Consult the module docstrings for full details.


### Sudo configuration

Thawing goblins requires `sudo` to keep inherited file descriptors open (`sudo -C …`). If `pdum-criu doctor` reports a `closefrom_override` failure, run `sudo visudo` and add one of:

```
Defaults    closefrom_override
```

or a user-scoped variant:

```
Defaults:youruser    closefrom_override
```

Save, exit, and rerun the doctor to confirm the setting.

### Known limitations

- CRIU can’t restore shells spawned inside the VS Code integrated terminal—the pseudo-terminal belongs to VS Code’s pty proxy, so `criu restore` errors with `tty: No task found with sid …`. Run the target inside a real terminal (tmux/screen/gnome-terminal) or detach it with `setsid`/`script` before calling `pdum-criu shell freeze`/`shell beam`, otherwise thaw will fail (the CLI now warns/blocks by default).
- Dumping a process that was itself restored is not yet supported. CRIU frequently aborts the second dump with mount-parent errors because the restored namespaces and bind mounts don’t line up with the current host state. Treat “freeze → thaw → freeze again” workflows as experimental; a reliable solution is still work-in-progress.
- Most programs aren’t “goblins” out of the box. The helpers assume the workload speaks only stdin/stdout/stderr and tolerates signals/etc. For example, the stock Python REPL behaves well, but IPython does not, and some apps reuse signal handlers in surprising ways. Expect to wrap or adapt your processes; we’re still collecting patterns that make more off-the-shelf tools behave.
- CUDA workloads are largely untested. In theory you can freeze a CUDA program as long as you don’t leave it running on the GPU, then restore it once—but multiple restores appear to fail, and we haven’t validated this path yet.




## Development

This project uses [uv](https://docs.astral.sh/uv/) for dependency management.

### Setup

```bash
# Install UV if you haven't already
curl -LsSf https://astral.sh/uv/install.sh | sh

# Clone the repository
git clone https://github.com/habemus-papadum/pdum_criu.git
cd pdum_criu

# Provision the entire toolchain (uv sync, pnpm install, widget build, pre-commit hooks)
./scripts/setup.sh
```

**Important for Development**:
- `./scripts/setup.sh` is idempotent—rerun it after pulling dependency changes
- Use `uv sync --frozen` to ensure the lockfile is respected when installing Python deps

### Running Tests

```bash
# Run all tests
uv run pytest

# Run a specific test file
uv run pytest tests/test_example.py

# Run a specific test function
uv run pytest tests/test_example.py::test_version

# Run tests with coverage
./scripts/coverage.sh
```
This wrapper exports `COVERAGE_PROCESS_START`, runs `pytest` under `coverage`, combines the generated `.coverage.*` shards (including those from CLI subprocesses), and writes both the XML and terminal reports. Pass additional pytest arguments after the script to scope the run (for example, `./scripts/coverage.sh tests/test_live_cli.py`).
### Live testing

To run the end-to-end CRIU test locally (requires Linux, CRIU, `pgrep`, and password-less `sudo`):

```bash
pytest tests/test_live_criu.py -k goblin_freeze_live
```

### Code Quality

```bash
# Check code with ruff
uv run ruff check .

# Format code with ruff
uv run ruff format .

# Fix auto-fixable issues
uv run ruff check --fix .
```

### Building

```bash
# Build Python + TypeScript artifacts
./scripts/build.sh

# Or build just the Python distribution artifacts
uv build
```

### Publishing

```bash
# Build and publish to PyPI (requires credentials)
./scripts/publish.sh
```

### Automation scripts

- `./scripts/setup.sh` – bootstrap uv, pnpm, widget bundle, and pre-commit hooks
- `./scripts/build.sh` – reproduce the release build locally
- `./scripts/pre-release.sh` – run the full battery of quality checks
- `./scripts/release.sh` – orchestrate the release (creates tags, publishes to PyPI/GitHub)
- `./scripts/test_notebooks.sh` – execute demo notebooks (uses `./scripts/nb.sh` under the hood)
- `./scripts/setup-visual-tests.sh` – install Playwright browsers for visual tests

## License

MIT License - see LICENSE file for details.
