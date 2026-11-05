"""Shared lexical recognition for standalone numeral candidates."""
from __future__ import annotations

import re

_ROMAN = re.compile(r"(?i)^(?=.+$)m{0,3}(?:cm|cd|d?c{0,3})(?:xc|xl|l?x{0,3})(?:ix|iv|v?i{0,3})$")
_NUMERAL = re.compile(
    r"^(?:\d{1,5}|[一二三四五六七八九十百千万零〇]+|[壹贰叁肆伍陆柒捌玖拾佰仟]+)$"
)
_PAGE_LABEL = re.compile(r"(?i)^(?:pdf\s+)?page\s*[:#-]?\s*(\d{1,5}|[ivxlcdm]{1,12})$")


def standalone_number_label(value: str) -> str | None:
    """Return the number in a complete standalone numeral line, if any."""
    text = re.sub(r"^\s*#{1,6}\s*", "", str(value or "")).strip()
    page = _PAGE_LABEL.fullmatch(text)
    if page:
        return page.group(1)
    text = text.strip(" \t()（）[]【】、.．:：—–-")
    return text if _NUMERAL.fullmatch(text) or _ROMAN.fullmatch(text) else None
