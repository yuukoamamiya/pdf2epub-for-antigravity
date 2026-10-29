"""Audits shared by translation pipelines for special-role identity markers."""
from __future__ import annotations
import re
from .page_furniture_rules import candidate_for_line

_SPECIAL_ROLE_NUMERIC_MARKER_RE = re.compile(r"(?<![A-Za-z0-9])\d+(?:[./:]\d+)*(?:[-‐‑‒–—]\d+(?:[./:]\d+)*)*(?![A-Za-z0-9])")

def special_role_numeric_markers(text: str) -> list[str]:
    """Extract stable numeric markers from bibliography/index content.

    Arabic numerals carry bibliographic identity (years, editions, DOI/ISBN
    fragments) and index navigation (page numbers and ranges).  Normalize
    Unicode dashes and whitespace around ranges, but keep the marker order and
    punctuation so a changed mapping is not silently accepted.
    """
    import unicodedata

    normalized = unicodedata.normalize("NFKC", text)
    normalized = re.sub(
        r"(?<=\d)\s*([-‐‑‒–—])\s*(?=\d)",
        r"\1",
        normalized,
    )
    markers = [
        (match.start(), re.sub(r"[‐‑‒–—]", "-", match.group(0)))
        for match in _SPECIAL_ROLE_NUMERIC_MARKER_RE.finditer(normalized)
    ]
    # Roman numerals are semantic only in explicit volume/part contexts. Bare
    # I/II remains ordinary prose, initials, or an identifier and is ignored.
    roman_context = re.compile(
        r"(?:\b(?:vol(?:ume)?|part)\s*\.?\s*"
        r"(?P<english_roman>M{0,3}(?:CM|CD|D?C{0,3})(?:XC|XL|L?X{0,3})(?:IX|IV|V?I{0,3}))\b"
        r"|第\s*"
        r"(?P<chinese_roman>M{0,3}(?:CM|CD|D?C{0,3})(?:XC|XL|L?X{0,3})(?:IX|IV|V?I{0,3}))(?=卷|部))",
        re.IGNORECASE,
    )

    def roman_value(value: str) -> int:
        values = {"I": 1, "V": 5, "X": 10, "L": 50, "C": 100, "D": 500, "M": 1000}
        total = 0
        previous = 0
        for char in reversed(value.upper()):
            number = values[char]
            total += -number if number < previous else number
            previous = max(previous, number)
        return total

    roman_scan_text = re.sub(
        r"https?://\S+|\b\d{1,4}\.\d{3,9}/\S+|doi(?:\.org)?/\S+|`[^`]*`",
        lambda match: " " * len(match.group(0)),
        normalized,
        flags=re.IGNORECASE,
    )
    for match in roman_context.finditer(roman_scan_text):
        roman = match.group("english_roman") or match.group("chinese_roman")
        if roman:
            value = roman_value(roman)
            if value > 0 and _int_to_roman(value) == roman.upper():
                markers.append((match.start(), str(value)))
    return [value for _, value in sorted(markers)]


def _int_to_roman(value: int) -> str:
    """Return the canonical Roman spelling for the supported 1..3999 range."""
    parts = []
    for number, numeral in (
        (1000, "M"), (900, "CM"), (500, "D"), (400, "CD"),
        (100, "C"), (90, "XC"), (50, "L"), (40, "XL"),
        (10, "X"), (9, "IX"), (5, "V"), (4, "IV"), (1, "I"),
    ):
        count, value = divmod(value, number)
        parts.append(numeral * count)
    return "".join(parts)


def validate_special_role_markers(
    source_text: str,
    target_text: str,
    role: str,
    *,
    allow_page_furniture_deletion: bool = False,
) -> list[str]:
    """Reject loss or alteration of numeric identity markers.

    This is deliberately narrower than a general semantic comparison.  It
    protects the machine-useful parts of bibliography and index units while
    allowing names, titles, prose, punctuation and ordinary Markdown to be
    translated naturally.  Sequence comparison catches both dropped markers
    and accidental reordering of page mappings.
    """
    source_markers = special_role_numeric_markers(source_text)
    target_markers = special_role_numeric_markers(target_text)

    if role == "index":
        source_entries = _index_entry_groups(
            source_text,
            allow_page_furniture_deletion=allow_page_furniture_deletion,
        )
        target_entries = _index_entry_groups(
            target_text,
            allow_page_furniture_deletion=allow_page_furniture_deletion,
        )
        if len(source_entries) != len(target_entries):
            return [
                "index entry count mismatch: "
                f"source={len(source_entries)}, target={len(target_entries)}"
            ]
        for index, (source_entry, target_entry) in enumerate(
            zip(source_entries, target_entries), 1
        ):
            source_entry_markers = special_role_numeric_markers(source_entry)
            target_entry_markers = special_role_numeric_markers(target_entry)
            if source_entry_markers != target_entry_markers:
                return [
                    f"index numeric marker mismatch (entry {index}): "
                    f"source={source_entry_markers!r}, "
                    f"target={target_entry_markers!r}"
                ]
    if source_markers == target_markers:
        return []

    if allow_page_furniture_deletion:
        import difflib

        furniture_numbers = []
        for line in source_text.splitlines():
            if candidate_for_line(line):
                furniture_numbers.extend(special_role_numeric_markers(line))
        matcher = difflib.SequenceMatcher(
            None, source_markers, target_markers, autojunk=False
        )
        mismatch = False
        for tag, i1, i2, j1, j2 in matcher.get_opcodes():
            if tag == "equal":
                continue
            if tag == "delete":
                deleted = source_markers[i1:i2]
                for num in deleted:
                    if num not in furniture_numbers:
                        mismatch = True
                        break
                if mismatch:
                    break
            else:
                mismatch = True
                break
        if not mismatch:
            return []

    limit = 12
    source_preview = source_markers[:limit]
    target_preview = target_markers[:limit]
    suffix = " ..." if len(source_markers) > limit or len(target_markers) > limit else ""
    return [
        f"{role} numeric marker mismatch: "
        f"source={source_preview!r}, target={target_preview!r}{suffix}"
    ]


def _index_entry_groups(
    text: str,
    *,
    allow_page_furniture_deletion: bool = False,
) -> list[str]:
    """Group an index into stable entries while allowing indented wrapping.

    The translation contract requires a new top-level entry to start at the
    same indentation boundary as the source. A wrapped target entry may use
    additional indented lines, which are folded into the preceding entry for
    marker comparison. This catches omitted prose-only entries as well as
    entries whose page markers were dropped.
    """
    entries: list[str] = []
    current: list[str] = []
    for line in str(text or "").splitlines():
        if not line.strip() or re.match(r"^\s*#{1,6}\s+", line):
            continue
        if allow_page_furniture_deletion:
            candidate = candidate_for_line(line)
            if (
                candidate
                and candidate.get("confidence") == "high"
                and candidate.get("kind")
                in {"running_header", "running_title_plus_page_label"}
            ):
                continue
        stripped = line.strip()
        if current and line[:1].isspace():
            current.append(stripped)
            continue
        if current:
            entries.append(" ".join(current))
        current = [stripped]
    if current:
        entries.append(" ".join(current))
    return entries
