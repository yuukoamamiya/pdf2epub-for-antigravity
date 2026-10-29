"""Display helpers for internal footnote keys."""

import re
import html

from pdf2epub.footnote_keys import marker_label_from_key

_SCOPED_KEY_RE = re.compile(r"^fnc-[A-Za-z0-9]+-(?P<number>\d+)(?:-o\d+)?$")


def display_footnote_key(key: str) -> str:
    """Return the reader-facing number for an internal footnote key.

    Scoped centralized endnotes use ``fnc-<scope>-<number>`` (and ``-oN`` for
    repeated occurrences).  Page-note keys such as ``197n67`` and ordinary
    numeric keys are returned unchanged.
    """
    value = str(key)
    marker_label = marker_label_from_key(value)
    if marker_label is not None:
        return marker_label
    scoped_marker = re.fullmatch(r"fnc-[A-Za-z0-9]+-(?P<marker>fnm-.+)", value)
    if scoped_marker:
        marker_label = marker_label_from_key(scoped_marker.group("marker"))
        if marker_label is not None:
            return marker_label
    match = _SCOPED_KEY_RE.match(value)
    return match.group("number") if match else value


def display_footnote_key_html(key: str) -> str:
    """Escape a printed label for inline XHTML that passes through Markdown."""
    return html.escape(display_footnote_key(key), quote=True).replace("*", "&#42;")


__all__ = ["display_footnote_key", "display_footnote_key_html"]
