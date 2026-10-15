"""Shared document-role and special-unit classification rules.

Role inference is used by hand-off preparation, validation, and HTML/PDF
pipelines.  Keeping it independent from Markdown validation prevents command
and validation modules from becoming the de facto shared-services layer.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Optional


TRANSLATION_SPECIAL_ROLES = frozenset(
    {
        "bibliography",
        "index",
        "metadata",
        "frontmatter",
        "copyright",
        "cover",
        "notes",
        "author_notes",
        "catalog",
        "ad",
    }
)

DENSITY_EXEMPT_ROLES = frozenset(
    {
        "bibliography",
        "index",
        "notes",
        "author_notes",
        "catalog",
        "ad",
    }
)

_REFERENCE_SOURCE_LABELS = {
    "references",
    "reference",
    "bibliography",
    "literatur",
    "literature",
    "bibliographie",
    "works cited",
    "sources",
    "notes",
    "endnotes",
}
_REFERENCE_TARGET_LABELS = {
    "参考文献",
    "参考书目",
    "文献",
    "书目",
    "注释",
    "脚注",
    "尾注",
    "参考资料",
}


def is_front_matter_text(text: str) -> bool:
    """Recognize common copyright/CIP front-matter labels."""
    head = "\n".join(str(text or "").splitlines()[:24])
    return bool(
        re.search(
            r"\b(?:cataloging[- ]in[- ]publication|library of congress|"
            r"copyright|all rights reserved|isbn|cip data)\b",
            head,
            flags=re.IGNORECASE,
        )
    )


def _plain_markdown_label(line: str) -> str:
    """Normalize a standalone label without treating it as a heading."""
    value = line.strip()
    value = re.sub(r"^#{1,6}\s+", "", value)
    value = re.sub(r"^\*{1,3}|\*{1,3}$|^_{1,3}|_{1,3}$", "", value)
    value = value.strip().strip("：:—–-·• ")
    return re.sub(r"\s+", " ", value).casefold()


def _is_reference_label(line: str, target: bool = False) -> bool:
    """Return whether a line is an exact, standalone references label."""
    label = _plain_markdown_label(line)
    if target and label in _REFERENCE_TARGET_LABELS:
        return True
    return label in _REFERENCE_SOURCE_LABELS


def is_bibliography_text(text: str) -> bool:
    """Recognize standalone or continuation bibliography/reference units."""
    normalized = str(text or "").replace("\r\n", "\n")
    blocks = [block.strip() for block in normalized.split("\n\n") if block.strip()]
    if not blocks:
        return False
    first_line = blocks[0].splitlines()[0].strip()
    if _is_reference_label(first_line):
        return True
    first_block = blocks[0].strip()
    if re.match(
        r"^(?:#{1,6}\s+)?(?:references|bibliography|literatur|works cited|参考文献)\b",
        first_block,
        re.IGNORECASE,
    ):
        return True
    if len(blocks) >= 5:
        cite_re = re.compile(r"\(\d{4}[a-z]?\)|https?://|doi\.org", re.IGNORECASE)
        cite_count = sum(1 for block in blocks if cite_re.search(block))
        if cite_count / len(blocks) >= 0.8:
            return True
    return False


def infer_translation_file_role(
    filename: str,
    text: Optional[str] = None,
) -> Optional[str]:
    """Infer a conservative special-unit role from a generated filename."""
    stem = Path(str(filename)).stem.lower()
    stem = re.sub(r"\.part\d+$", "", stem)
    normalized = re.sub(r"[^a-z0-9]+", "_", stem).strip("_")
    tokens = set(normalized.split("_"))
    if "author" in tokens and "notes" in tokens:
        return "author_notes"
    role_tokens = (
        ("bibliography", "bibliography"),
        ("references", "bibliography"),
        ("reference", "bibliography"),
        ("index", "index"),
        ("notes", "notes"),
        ("endnotes", "notes"),
        ("copyright", "copyright"),
        ("cover", "cover"),
        ("catalog", "catalog"),
        ("ad", "ad"),
    )
    for token, role in role_tokens:
        if token in tokens:
            return role
    if text and is_bibliography_text(text):
        return "bibliography"
    return None


def is_density_exempt_role(role: Optional[str]) -> bool:
    """Return whether a unit is exempt from prose-language density checks."""
    return str(role or "").strip().lower() in DENSITY_EXEMPT_ROLES


__all__ = [
    "DENSITY_EXEMPT_ROLES",
    "TRANSLATION_SPECIAL_ROLES",
    "infer_translation_file_role",
    "is_bibliography_text",
    "is_density_exempt_role",
    "is_front_matter_text",
]
