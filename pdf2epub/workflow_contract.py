"""Machine-readable workflow names shared by the CLI, diagnostics and CI.

The detailed hand-off schemas remain in their domain modules. This file keeps
the public command inventory and the high-level stage order in one small,
dependency-free contract so documentation and recovery tooling can validate
against the same vocabulary.
"""

from __future__ import annotations

from typing import Final


CLI_COMMANDS: Final[frozenset[str]] = frozenset(
    {
        "build-epub",
        "build-html-epub",
        "build-novel-epub",
        "check-ready",
        "doctor",
        "extract-entities",
        "extract-entities-validate",
        "footnote-apply",
        "footnote-prepare",
        "footnote-validate",
        "glossary-candidates",
        "html-prepare",
        "html-skeleton-retry",
        "html-skeleton-restore",
        "html-validate",
        "illustration-apply",
        "illustration-prepare",
        "illustration-validate",
        "ocr-consensus-rebuild",
        "ocr-correct",
        "ocr-correct-validate",
        "ocr-pages",
        "polish",
        "polish-validate",
        "refine",
        "refine-local",
        "refine-prepare",
        "repair-page-furniture",
        "repair-page-furniture-validate",
        "status",
        "translate",
        "translate-arxiv",
        "translate-arxiv-validate",
        "translate-novel",
        "translate-novel-validate",
        "translate-toc",
        "translate-toc-validate",
        "translate-validate",
    }
)


PDF_STRUCTURE_STAGES: Final[tuple[str, ...]] = (
    "ocr-pages",
    "ocr-correct",
    "refine-prepare",
    "illustration",
    "refine-local",
    "footnote",
    "polish",
)

PDF_TRANSLATION_STAGES: Final[tuple[str, ...]] = (
    "extract-entities",
    "translate-toc",
    "translate",
)

HTML_STAGES: Final[tuple[str, ...]] = (
    "html-prepare",
    "html-validate",
    "build-html-epub",
)


__all__ = [
    "CLI_COMMANDS",
    "PDF_STRUCTURE_STAGES",
    "PDF_TRANSLATION_STAGES",
    "HTML_STAGES",
]
