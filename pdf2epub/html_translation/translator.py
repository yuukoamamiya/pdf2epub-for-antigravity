"""Removed in-process HTML translator.

HTML translation is intentionally performed by an Antigravity workspace
Subagent.  ``html-prepare`` creates compressed units, ``html-validate`` checks
the Subagent's files, and ``build-html-epub`` rebuilds the book locally.  This
stub exists only to give callers of the removed module a clear migration error;
it contains no model or provider integration.
"""

from __future__ import annotations


class HTMLTranslateProcessor:
    """Compatibility stub for the removed provider-backed translator."""

    def __init__(self, *args, **kwargs) -> None:
        raise RuntimeError(
            "The in-process HTML translator was removed. Run 'pdf2epub "
            "html-prepare', let an Antigravity Subagent translate the "
            "compressed units, then run 'pdf2epub html-validate' and "
            "'pdf2epub build-html-epub'."
        )


__all__ = ["HTMLTranslateProcessor"]
