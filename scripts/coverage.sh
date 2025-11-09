#!/usr/bin/env bash

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${ROOT_DIR}"

export COVERAGE_PROCESS_START="${COVERAGE_PROCESS_START:-${ROOT_DIR}/pyproject.toml}"

find "${ROOT_DIR}" -name ".coverage.cli-*" -delete >/dev/null 2>&1 || true
rm -f .coverage .coverage.*

uv run coverage erase
uv run coverage run -m pytest "$@"
uv run coverage combine || true
uv run coverage xml
uv run coverage report

find "${ROOT_DIR}" -name ".coverage.cli-*" -delete >/dev/null 2>&1 || true
