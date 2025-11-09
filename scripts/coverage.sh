#!/usr/bin/env bash

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${ROOT_DIR}"

export COVERAGE_PROCESS_START="${COVERAGE_PROCESS_START:-${ROOT_DIR}/pyproject.toml}"

find "${ROOT_DIR}" -name ".coverage.cli-*" -delete >/dev/null 2>&1 || true
rm -f .coverage .coverage.*

uv run coverage erase
uv run coverage run -m pytest "$@"

MAIN_DATA="${ROOT_DIR}/.coverage.main"
if [ -f ".coverage" ]; then
  mv .coverage "${MAIN_DATA}"
fi

shopt -s nullglob
CLI_DATA=( "${ROOT_DIR}"/.coverage.cli-* )
if [ -f "${MAIN_DATA}" ] && [ ${#CLI_DATA[@]} -gt 0 ]; then
  uv run coverage combine "${MAIN_DATA}" "${CLI_DATA[@]}"
elif [ -f "${MAIN_DATA}" ]; then
  mv "${MAIN_DATA}" .coverage
elif [ ${#CLI_DATA[@]} -gt 0 ]; then
  uv run coverage combine "${CLI_DATA[@]}"
else
  echo "warning: no coverage data files found" >&2
fi

uv run coverage xml
uv run coverage report

find "${ROOT_DIR}" -name ".coverage.cli-*" -delete >/dev/null 2>&1 || true
rm -f "${MAIN_DATA}"
