"""Display helpers for internal footnote keys."""

import html

from pdf2epub.footnote_keys import printed_label_from_key


def display_footnote_key(key: str) -> str:
    """Return the reader-facing number for an internal footnote key.

    Scoped centralized endnotes use ``fnc-<scope>-<number>`` (and ``-oN`` for
    repeated occurrences).  Page-note keys such as ``197n67`` and ordinary
    numeric keys are returned unchanged.
    """
    return printed_label_from_key(key)


def display_footnote_key_html(key: str) -> str:
    """Escape a printed label for inline XHTML that passes through Markdown."""
    return html.escape(display_footnote_key(key), quote=True).replace("*", "&#42;")


__all__ = ["display_footnote_key", "display_footnote_key_html"]
