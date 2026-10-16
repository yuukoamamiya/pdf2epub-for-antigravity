"""Shared lexical signals for PDF footnote candidate detection.

The prepare and continuation stages use the same conservative definition of a
leading footnote key and the same label exclusions. Keeping those signals here
prevents the two stages from drifting apart.
"""

from __future__ import annotations

import re
from typing import Optional


_FOOTNOTE_LABEL_RE = re.compile(
    r"(?:foot[\s_-]*note\b|注脚|脚注|页下注)", re.IGNORECASE
)
_CITATION_LABEL_RE = re.compile(
    r"(?:bibliograph(?:y|ies)|reference(?:s)?|citation|参考文献|引用|文献)",
    re.IGNORECASE,
)
_FURNITURE_LABEL_RE = re.compile(
    r"(?:page[\s_-]*(?:header|footer)|running[\s_-]*(?:header|footer))",
    re.IGNORECASE,
)
_CIRCLED_CHARS = (
    "①②③④⑤⑥⑦⑧⑨⑩⑪⑫⑬⑭⑮⑯⑰⑱⑲⑳"
    "㉑㉒㉓㉔㉕㉖㉗㉘㉙㉚㉛㉜㉝㉞㉟"
    "㊱㊲㊳㊴㊵㊶㊷㊸㊹㊺㊻㊼㊽㊾㊿"
)
_CIRCLED_DIGITS_MAP = {char: str(i) for i, char in enumerate(_CIRCLED_CHARS, 1)}
_INT_TO_CIRCLED_MAP = {str(i): char for i, char in enumerate(_CIRCLED_CHARS, 1)}
_CIRCLED_PATTERN = "[" + "".join(_CIRCLED_CHARS) + "]"

_FOOTNOTE_KEY_RE = re.compile(
    r"^\s*(?P<key>\d{1,4})(?=\s|[.)、，:：;；\-–—]|$)",
    re.IGNORECASE,
)
_FOOTNOTE_SUP_KEY_RE = re.compile(
    r"^\s*<sup\b[^>]*>\s*(?P<key>\d{1,4})\s*</sup>(?=\s|[.)、，:：;；\-–—]|$)",
    re.IGNORECASE,
)
_FOOTNOTE_UNICODE_KEY_RE = re.compile(
    r"^\s*(?P<key>[⁰¹²³⁴⁵⁶⁷⁸⁹]{1,4})(?=\s|[.)、，:：;；\-–—]|$)"
)
_FOOTNOTE_CIRCLED_KEY_RE = re.compile(
    rf"^\s*(?P<key>{_CIRCLED_PATTERN})"
)
_FOOTNOTE_SUP_CIRCLED_KEY_RE = re.compile(
    rf"^\s*<sup\b[^>]*>\s*(?P<key>{_CIRCLED_PATTERN})\s*</sup>",
    re.IGNORECASE,
)
_NUMERIC_ONLY_RE = re.compile(rf"^\s*(?:\d{{1,4}}|{_CIRCLED_PATTERN})\s*$")
_NATIVE_NUMERIC_ONLY_RE = _NUMERIC_ONLY_RE
_NATIVE_SUP_MARKER_RE = re.compile(
    rf"<sup\b[^>]*>\s*(?P<key>\d{{1,4}}|{_CIRCLED_PATTERN})\s*</sup>|"
    rf"(?P<unicode>[⁰¹²³⁴⁵⁶⁷⁸⁹]{{1,4}}|{_CIRCLED_PATTERN})"
)
_SUPERSCRIPT_DIGITS = str.maketrans("⁰¹²³⁴⁵⁶⁷⁸⁹", "0123456789")


def leading_footnote_key(text: str) -> Optional[str]:
    """Extract a conservative key from the beginning of a layout block."""
    value = str(text or "")
    for pattern in (
        _FOOTNOTE_SUP_KEY_RE,
        _FOOTNOTE_SUP_CIRCLED_KEY_RE,
        _FOOTNOTE_KEY_RE,
        _FOOTNOTE_UNICODE_KEY_RE,
        _FOOTNOTE_CIRCLED_KEY_RE,
    ):
        match = pattern.match(value)
        if match:
            raw_key = match.group("key")
            if raw_key in _CIRCLED_DIGITS_MAP:
                return _CIRCLED_DIGITS_MAP[raw_key]
            return raw_key.translate(_SUPERSCRIPT_DIGITS)
    return None


__all__ = [
    "_CIRCLED_DIGITS_MAP",
    "_CITATION_LABEL_RE",
    "_FOOTNOTE_CIRCLED_KEY_RE",
    "_FOOTNOTE_KEY_RE",
    "_FOOTNOTE_LABEL_RE",
    "_FOOTNOTE_SUP_CIRCLED_KEY_RE",
    "_FOOTNOTE_SUP_KEY_RE",
    "_FOOTNOTE_UNICODE_KEY_RE",
    "_FURNITURE_LABEL_RE",
    "_INT_TO_CIRCLED_MAP",
    "_NATIVE_NUMERIC_ONLY_RE",
    "_NATIVE_SUP_MARKER_RE",
    "_NUMERIC_ONLY_RE",
    "_SUPERSCRIPT_DIGITS",
    "leading_footnote_key",
]
