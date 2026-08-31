"""Shared validators for compressed HTML translation units."""

import re
from typing import List


_PROTECTED_TOKEN_RE = re.compile(
    r"<[^>]*>|&(?:#[0-9]+|#x[0-9a-fA-F]+|[A-Za-z][A-Za-z0-9]+);|"
    r"\{\{[^{}\n]+\}\}|\[\[[^\[\]\n]+\]\]|%[A-Za-z_][A-Za-z0-9_]*%"
)


def nonempty_lines(text: str) -> List[str]:
    """Return non-empty logical translation lines."""
    return [line for line in text.splitlines() if line.strip()]


def tag_sequence(text: str) -> List[str]:
    """Extract the HTML tag-name sequence used by compressed validation."""
    return [tag.lower() for tag in re.findall(r'<(/?[a-zA-Z0-9]+)', text)]


def protected_sequence(text: str) -> List[str]:
    """Extract tags, entities, and common placeholders in source order.

    Compressed HTML has already moved most attributes into its mapping file,
    but a Subagent can still add/remove tag attributes or alter entities in a
    translatable line.  Comparing the complete protected tokens catches those
    changes before decompression.
    """
    return [re.sub(r"\s+", " ", token.strip()) for token in _PROTECTED_TOKEN_RE.findall(text)]


def tag_mismatch_count(source_lines: List[str], translated_lines: List[str]) -> int:
    """Count protected-token mismatches over aligned source/translation lines."""
    return sum(
        1
        for source, translated in zip(source_lines, translated_lines)
        if protected_sequence(source) != protected_sequence(translated)
    )
