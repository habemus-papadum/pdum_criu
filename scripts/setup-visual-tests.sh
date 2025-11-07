#!/bin/bash
# Setup script for visual testing with Playwright

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="${SCRIPT_DIR}/.."

cd "$REPO_ROOT"

echo "Setting up visual testing environment..."

uv run playwright install chromium

echo ""
echo "✅ Visual testing setup complete!"
echo "Run visual tests with:"
echo "  uv run pytest -m visual"
