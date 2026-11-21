"""Cached read-only inventories for Markdown sources.

Build and validation phases repeatedly read the same Markdown files while
walking the TOC, iterating parts, and checking inline endnote continuations.
Reading each file once per process keeps those loops dominated by parsing
rather than disk I/O.  Every cache is keyed by the file's stat signature, so a
rewritten file is re-read in long-lived processes and tests instead of
serving stale content.
"""

from __future__ import annotations

import hashlib
from functools import lru_cache
from pathlib import Path
from typing import Dict, Optional, Tuple

# name -> (utf-8 text, sha256 of the text)
MarkdownEntry = Tuple[str, str]
MarkdownInventory = Dict[str, MarkdownEntry]

__all__ = [
    "MarkdownEntry",
    "MarkdownInventory",
    "build_markdown_inventory",
    "clear_markdown_caches",
    "read_markdown_text",
]


@lru_cache(maxsize=256)
def _read_text_cached(path_str: str, mtime_ns: int, size: int) -> Optional[str]:
    """Read a Markdown file once per stat signature."""
    try:
        return Path(path_str).read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return None


def _signature(path: Path) -> Optional[Tuple[str, int, int]]:
    """Return ``(resolved path, mtime_ns, size)`` for a regular file."""
    try:
        if not path.is_file():
            return None
        stat = path.stat()
    except OSError:
        return None
    return str(path.resolve()), stat.st_mtime_ns, stat.st_size


def read_markdown_text(path) -> Optional[str]:
    """Read one Markdown file through the shared stat-keyed cache."""
    signature = _signature(Path(path))
    if signature is None:
        return None
    return _read_text_cached(*signature)


@lru_cache(maxsize=16)
def _build_inventory_cached(
    directory: str,
    signature: Tuple[Tuple[str, str, int, int], ...],
) -> MarkdownInventory:
    inventory: MarkdownInventory = {}
    for name, path_str, mtime_ns, size in signature:
        text = _read_text_cached(path_str, mtime_ns, size)
        if text is None:
            continue
        inventory[name] = (text, hashlib.sha256(text.encode("utf-8")).hexdigest())
    return inventory


def build_markdown_inventory(source_dir) -> MarkdownInventory:
    """Inventory every ``*.md`` file directly inside ``source_dir``.

    The result maps a file name to its decoded text and the sha256 of that
    text.  Discovery stats the directory's Markdown files only; file contents
    are read once and reused for the lifetime of the stat signature.
    """
    base = Path(source_dir)
    if not base.is_dir():
        return {}
    try:
        children = sorted(base.iterdir(), key=lambda item: item.name)
    except OSError:
        return {}
    entries: list[Tuple[str, str, int, int]] = []
    for child in children:
        if child.suffix.lower() != ".md":
            continue
        signature = _signature(child)
        if signature is None:
            continue
        entries.append((child.name, signature[0], signature[1], signature[2]))
    return _build_inventory_cached(str(base.resolve()), tuple(entries))


def clear_markdown_caches() -> None:
    """Drop every cached text/inventory entry (tests and long-lived workers)."""
    _read_text_cached.cache_clear()
    _build_inventory_cached.cache_clear()
