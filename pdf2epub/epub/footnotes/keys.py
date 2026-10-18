"""Display helpers for internal footnote keys."""

import re

_SCOPED_KEY_RE = re.compile(r"^fnc-[A-Za-z0-9]+-(?P<number>\d+)(?:-o\d+)?$")


def display_footnote_key(key: str) -> str:
    """Return the reader-facing number for an internal footnote key.

    Scoped centralized endnotes use ``fnc-<scope>-<number>`` (and ``-oN`` for
    repeated occurrences).  Page-note keys such as ``197n67`` and ordinary
    numeric keys are returned unchanged.
    """
    match = _SCOPED_KEY_RE.match(str(key))
    return match.group("number") if match else str(key)


__all__ = ["display_footnote_key"]
