#!/usr/bin/env bash
# build_all_epubs.sh — Build every EPUB variant for a translated PDF project.
#
# Optional convenience for pipeline: translation, after translated validation,
# to rebuild both formal artifacts. The default workflow builds and inspects
# the original-language EPUB before translation, then builds only the translated
# EPUB after translation validation. This helper reproduces both builds:
#   1. build-epub                   -> original-language EPUB under the formal book title
#   2. build-epub --only-translated -> translated EPUB under the translated title
# No English _en.epub is generated. To produce an additional English _en.epub,
# run `build-epub --translated`; that command generates the English _en.epub and
# translated EPUB, but not the formal-title original. A three-artifact delivery
# therefore also requires the formal-title original built earlier.
# Do not use this script for pipeline: epub_conversion; that mode rejects
# --translated and --only-translated and only needs a single build-epub.
#
# Usage: bash scripts/build_all_epubs.sh [config.yaml]

set -euo pipefail

CONFIG="${1:-config.yaml}"

if [ ! -f "$CONFIG" ]; then
  echo "Error: config file '$CONFIG' not found." >&2
  echo "Usage: bash scripts/build_all_epubs.sh [config.yaml]" >&2
  exit 2
fi

CONFIG="$(cd "$(dirname "$CONFIG")" && pwd)/$(basename "$CONFIG")"
if command -v cygpath >/dev/null 2>&1; then
  CONFIG="$(cygpath -w "$CONFIG")"
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$REPO_ROOT"

echo "[build_all_epubs] Building original-language EPUB from $CONFIG ..."
uv run pdf2epub -c "$CONFIG" build-epub

echo "[build_all_epubs] Building translated EPUB from $CONFIG ..."
uv run pdf2epub -c "$CONFIG" build-epub --only-translated
