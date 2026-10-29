"""Dependency-free identities for printed footnote markers."""

from __future__ import annotations

import re
import html
from dataclasses import dataclass
from typing import Optional

CIRCLED_LABELS = (
    "①②③④⑤⑥⑦⑧⑨⑩⑪⑫⑬⑭⑮⑯⑰⑱⑲⑳"
    "㉑㉒㉓㉔㉕㉖㉗㉘㉙㉚㉛㉜㉝㉞㉟"
    "㊱㊲㊳㊴㊵㊶㊷㊸㊹㊺㊻㊼㊽㊾㊿"
)

_SUPERSCRIPT_DIGITS = str.maketrans("⁰¹²³⁴⁵⁶⁷⁸⁹", "0123456789")
_KEY_RE = re.compile(r"[A-Za-z0-9_-]+")


def canonical_numeric_label(value: str) -> Optional[str]:
    """Return ASCII decimal digits for recognized printed numeric forms."""
    value = str(value or "").strip().translate(_SUPERSCRIPT_DIGITS)
    if not value:
        return None
    digits: list[str] = []
    for char in value:
        if not char.isdecimal():
            return None
        digits.append(str(int(char)))
    return "".join(digits) if len(digits) <= 4 else None


def is_valid_footnote_key(key: str) -> bool:
    """Whether a persisted logical marker key is safe ASCII."""
    return bool(_KEY_RE.fullmatch(str(key or "")))


@dataclass(frozen=True)
class FootnoteMarker:
    """A visible printed marker and its safe ASCII key."""

    kind: str
    label: str
    key: str


def marker_from_label(label: str) -> Optional[FootnoteMarker]:
    value = html.unescape(str(label or "")).strip()
    if len(value) == 1 and value in CIRCLED_LABELS:
        return FootnoteMarker("numeric", value, str(CIRCLED_LABELS.index(value) + 1))
    numeric = canonical_numeric_label(value)
    if numeric is not None:
        return FootnoteMarker("numeric", value, numeric)
    if re.fullmatch(r"\*{1,3}", value):
        return FootnoteMarker("star", value, f"fnm-star-{len(value)}")
    if value in {"†", "††"}:
        return FootnoteMarker("dagger", value, "fnm-dagger" if value == "†" else "fnm-dagger-2")
    if value in {"‡", "‡‡"}:
        return FootnoteMarker("double_dagger", value, "fnm-double-dagger" if value == "‡" else "fnm-double-dagger-2")
    if re.fullmatch(r"[A-Za-z]", value):
        return FootnoteMarker("letter", value, f"fnm-letter-{value}")
    return None


def key_from_printed_marker(label: str) -> Optional[str]:
    marker = marker_from_label(label)
    return marker.key if marker else None


def marker_label_from_key(key: str) -> Optional[str]:
    """Decode only the bounded reserved marker namespace, including repeats."""
    value = re.sub(r"-o(?:[2-9]|[1-9][0-9]+)$", "", str(key or ""))
    circle = re.fullmatch(r"fnm-circle-([0-9]{1,2})", value)
    if circle:
        number = int(circle.group(1))
        return CIRCLED_LABELS[number - 1] if 1 <= number <= len(CIRCLED_LABELS) else None
    number = re.fullmatch(r"fnm-number-([0-9]{1,4})", value)
    if number:
        return number.group(1)
    match = re.fullmatch(
        r"fnm-(?:star-(?P<stars>[123])|dagger(?P<daggers>-2)?|"
        r"double-dagger(?P<double>-2)?|letter-(?P<letter>[A-Za-z]))",
        value,
    )
    if not match:
        return None
    if match.group("stars"):
        return "*" * int(match.group("stars"))
    if match.group("daggers") is not None:
        return "††"
    if match.group("double") is not None:
        return "‡‡"
    if match.group("letter") is not None:
        return match.group("letter")
    if "double-dagger" in value:
        return "‡"
    return "†"


def output_marker_key(key: str, marker_label: str, occurrence: int = 1) -> str:
    """Choose a safe Markdown key without changing logical marker matching."""
    if len(marker_label) == 1 and marker_label in CIRCLED_LABELS:
        output_key = f"fnm-circle-{CIRCLED_LABELS.index(marker_label) + 1}"
    else:
        output_key = key
    if occurrence > 1:
        if output_key.startswith("fnm-circle-"):
            output_key += f"-o{occurrence}"
        elif re.fullmatch(r"\d{1,4}", key):
            output_key = f"fnm-number-{key}"
            output_key += f"-o{occurrence}"
        elif marker_label_from_key(key) is not None or output_key.startswith("fnm-circle-"):
            output_key += f"-o{occurrence}"
        else:
            output_key += f"-{occurrence}"
    return output_key


__all__ = [
    "FootnoteMarker", "marker_from_label", "marker_label_from_key",
    "output_marker_key", "CIRCLED_LABELS", "key_from_printed_marker",
    "canonical_numeric_label", "is_valid_footnote_key",
]
