"""Patterns shared by endnote validation and endnote normalization.

Only the definitions that are *identical* on both sides live here.  The
definition regexes stay in their modules on purpose: validation matches a
prefix while apply requires content to the end of the line, so merging them
would change behavior.
"""
from __future__ import annotations

import re
from typing import Optional


UNICODE_SUP_RE = re.compile(r"(?P<value>[⁰¹²³⁴⁵⁶⁷⁸⁹]+)")
UNICODE_DIGITS = str.maketrans("⁰¹²³⁴⁵⁶⁷⁸⁹", "0123456789")
MARKDOWN_REF_RE = re.compile(r"\[\^(?P<key>[A-Za-z0-9_-]+)\](?!:)")


def markdown_fence_mask(lines: list[str]) -> list[bool]:
    """Mark fenced-code lines so source-like code is not read as structure."""
    mask = [False] * len(lines)
    fence_char: Optional[str] = None
    fence_length = 0
    for index, line in enumerate(lines):
        if fence_char is not None:
            mask[index] = True
            close = re.match(r"^[ \t]{0,3}(`{3,}|~{3,})[ \t]*$", line)
            if (
                close
                and close.group(1)[0] == fence_char
                and len(close.group(1)) >= fence_length
            ):
                fence_char = None
                fence_length = 0
            continue
        opening = re.match(r"^[ \t]{0,3}(`{3,}|~{3,})(.*)$", line)
        if opening is None:
            continue
        run = opening.group(1)
        if run[0] == "`" and "`" in opening.group(2):
            continue
        mask[index] = True
        fence_char = run[0]
        fence_length = len(run)
    return mask


__all__ = ["MARKDOWN_REF_RE", "UNICODE_DIGITS", "UNICODE_SUP_RE", "markdown_fence_mask"]
