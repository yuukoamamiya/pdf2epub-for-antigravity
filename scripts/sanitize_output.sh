#!/usr/bin/env bash
# Backwards-discoverable entry point for creating a sanitized export copy.
# Usage: scripts/sanitize_output.sh [source_dir [destination_dir]]
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd -- "$SCRIPT_DIR/.." && pwd)"
exec uv run --project "$PROJECT_DIR" python "$SCRIPT_DIR/sanitize_output.py" "$@"
