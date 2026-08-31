"""Removed legacy PDF structure-analysis entry point.

PDF structure analysis is now a file-based Antigravity Subagent hand-off:
``refine-prepare`` creates the task and ``refine-local`` consumes the
Subagent's ``toc_tree.json``.  This compatibility module intentionally has no
model, provider, credential, or network code so an old direct import cannot
silently reintroduce the removed API workflow.
"""

from __future__ import annotations


_REMOVAL_MESSAGE = (
    "The legacy breakdown API workflow was removed. Run 'pdf2epub "
    "refine-prepare', let an Antigravity Subagent write toc_tree.json, then "
    "run 'pdf2epub refine-local'."
)


def create_breakdown_client(config=None):
    """Reject the removed provider-backed client factory."""
    raise RuntimeError(_REMOVAL_MESSAGE)


def analyze_pdf_structure(*args, **kwargs):
    """Reject the removed provider-backed structure analysis function."""
    raise RuntimeError(_REMOVAL_MESSAGE)


def main() -> None:
    """Explain how to use the supported replacement when run as a module."""
    raise SystemExit(_REMOVAL_MESSAGE)


__all__ = ["analyze_pdf_structure", "create_breakdown_client", "main"]


if __name__ == "__main__":
    main()
