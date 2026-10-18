#!/usr/bin/env bash
# build_all_epubs.sh — Build every EPUB variant for a translated PDF project.
#
# Applies to pipeline: translation. Both builds are mandatory:
#   1. build-epub              -> original-language EPUB under the formal book title
#   2. build-epub --translated -> English _en.epub plus translated EPUB
#                                 (translated EPUB uses the translated title)
# --translated already emits the English _en.epub, but the first command is still
# required because it produces the original-language EPUB under the formal title.
# Do not use this script for pipeline: epub_conversion; that mode rejects
# --translated and only needs a single build-epub.
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

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$REPO_ROOT"

echo "[build_all_epubs] Building original-language EPUB from $CONFIG ..."
uv run pdf2epub -c "$CONFIG" build-epub

echo "[build_all_epubs] Building translated EPUB and English _en.epub from $CONFIG ..."
uv run pdf2epub -c "$CONFIG" build-epub --translated
