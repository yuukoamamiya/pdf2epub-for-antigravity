#!/usr/bin/env bash
# build_all_epubs.sh — Build every EPUB variant for a translated PDF project.
#
# Applies to pipeline: translation. Both builds are mandatory and produce two
# artifacts:
#   1. build-epub                   -> original-language EPUB under the formal book title
#   2. build-epub --only-translated -> translated EPUB under the translated title
# No English _en.epub is generated. To also produce the English _en.epub, use
# `build-epub --translated` instead of `--only-translated` (three artifacts:
# formal-title original, English _en.epub, and translated EPUB).
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
