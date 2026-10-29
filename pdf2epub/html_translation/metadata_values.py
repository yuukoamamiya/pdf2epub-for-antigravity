"""Library index values and JSON-safe metadata normalization."""
from __future__ import annotations
import re
from typing import Any

def sort_title_for_library(title: str) -> str:
    """Return a library-friendly title sort value.

    Calibre's default convention moves a leading English article to the end,
    e.g. ``The Class Matrix`` -> ``Class Matrix, The``.  Keep the source title
    casing because this field is an index value, not translated prose.
    """
    value = str(title or "").strip()
    if not value:
        return ""

    match = re.match(r'^(?P<article>the|a|an)\s+(?P<rest>.+)$', value, re.IGNORECASE)
    if not match:
        return value
    return f"{match.group('rest').strip()}, {match.group('article')}"

def sort_author_for_library(author: str) -> str:
    """Return a library-friendly sort value for one personal name.

    Names already written as ``Family, Given`` are left in that order.  For
    the common ``Given Family`` form, move the final name component first.
    """
    value = " ".join(str(author or "").split())
    if not value:
        return ""
    if "," in value:
        return value
    parts = value.split(" ")
    if len(parts) < 2:
        return value
    return f"{parts[-1]}, {' '.join(parts[:-1])}"

def make_json_safe(obj: Any) -> Any:
    """Convert nested compressor metadata to JSON-serializable values."""
    from collections.abc import Iterable, Mapping

    if isinstance(obj, (bool, int, float, str, type(None))):
        return obj
    if isinstance(obj, Mapping):
        return {str(make_json_safe(k)): make_json_safe(v) for k, v in obj.items()}
    if isinstance(obj, Iterable) and not isinstance(obj, (str, bytes)):
        return [make_json_safe(v) for v in obj]
    return str(obj)


# Compatibility alias for the former private helper name.
_make_json_safe = make_json_safe
