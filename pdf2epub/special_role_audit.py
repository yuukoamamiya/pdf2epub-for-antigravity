"""Audits shared by translation pipelines for special-role identity markers."""
from __future__ import annotations
import re
from dataclasses import dataclass, field
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
        r"(?P<chinese_roman>M{0,3}(?:CM|CD|D?C{0,3})(?:XC|XL|L?X{0,3})(?:IX|IV|V?I{0,3}))(?=\s*(?:卷|部)))",
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
                _index_structure_diagnostic(source_entries, target_entries)
            ]
        errors: list[str] = []
        source_depths = _index_indent_depths(source_entries)
        target_depths = _index_indent_depths(target_entries)
        if source_depths != target_depths:
            mismatch_at = next(
                index for index, (source_depth, target_depth)
                in enumerate(zip(source_depths, target_depths), 1)
                if source_depth != target_depth
            )
            source_entry = source_entries[mismatch_at - 1]
            target_entry = target_entries[mismatch_at - 1]
            errors.append(
                "index indentation hierarchy changed "
                f"(entry {mismatch_at}; source line {source_entry.start_line}, "
                f"indent {source_entry.indent}, depth {source_depths[mismatch_at - 1]}; "
                f"target line {target_entry.start_line}, indent {target_entry.indent}, "
                f"depth {target_depths[mismatch_at - 1]}; "
                f"source={source_entry.text[:70]!r}, target={target_entry.text[:70]!r})"
            )
        for index, (source_entry, target_entry) in enumerate(
            zip(source_entries, target_entries), 1
        ):
            source_entry_markers = source_entry.markers
            target_entry_markers = target_entry.markers
            if source_entry_markers != target_entry_markers:
                errors.append(
                    f"index numeric marker mismatch (entry {index}, "
                    f"source line {source_entry.start_line}, "
                    f"target line {target_entry.start_line}; "
                    f"source text={source_entry.text[:70]!r}, "
                    f"target text={target_entry.text[:70]!r}): "
                    f"source={source_entry_markers!r}, "
                    f"target={target_entry_markers!r}"
                )
        if errors:
            return errors
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


@dataclass
class _IndexEntry:
    text: str
    start_line: int
    end_line: int
    indent: int
    markers: list[str] = field(default_factory=list)
    group_reason: str = "line boundary"


def _index_entry_groups(
    text: str,
    *,
    allow_page_furniture_deletion: bool = False,
) -> list[_IndexEntry]:
    """Parse index lines while retaining evidence used to diagnose drift.

    Top-level rows, term-plus-locator rows, explicit cross-references, and
    explicit list markers anchor entries. Page-only rows attach to the previous
    entry; other indented rows remain continuations until paired structure
    supplies evidence of a boundary.
    """
    entries: list[_IndexEntry] = []
    for line_number, line in enumerate(str(text or "").splitlines(), 1):
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
        indent = len(line) - len(line.lstrip())
        stripped = line.strip()
        markers = special_role_numeric_markers(stripped)
        cross_reference = bool(re.match(
            r"(?i)(?:see(?: also)?\b|参见|另见|见|另见条目)", stripped
        ))
        explicit_nested_marker = bool(re.match(r"^(?:[-*+]\s+|\d+[.)]\s+)", stripped))
        # Distinguish a term with a locator from a locator-only continuation.
        has_term_locator = bool(markers and _index_line_has_term_before_locator(stripped))
        locator_only = bool(markers and not has_term_locator)
        is_ambiguous_indented = bool(
            indent and not markers and not cross_reference and not explicit_nested_marker
        )
        is_entry = (
            indent == 0 or has_term_locator or cross_reference or explicit_nested_marker
        )
        if is_entry or (not entries and is_ambiguous_indented):
            entries.append(_IndexEntry(
                text=stripped, start_line=line_number, end_line=line_number,
                indent=indent, markers=markers,
                group_reason=("explicit nested marker" if explicit_nested_marker else
                              "term plus locator" if has_term_locator else
                              "cross-reference cue" if cross_reference else
                              "top-level line"),
            ))
        elif entries:
            previous = entries[-1]
            previous.text += " " + stripped
            previous.end_line = line_number
            previous.markers.extend(markers)
            if locator_only:
                previous.group_reason = "page locator continuation"
    return entries


def _index_line_has_term_before_locator(line: str) -> bool:
    """Return whether a numeric locator is preceded by a visible index term."""
    match = re.search(r"\d+(?:[./:]\d+)*(?:[-‐‑‒–—]\d+(?:[./:]\d+)*)*", line)
    if match is None:
        return False
    prefix = line[:match.start()].strip(" \t,;:，；：.。()（）[]【】")
    return bool(prefix)


def _index_structure_diagnostic(
    source_entries: list[_IndexEntry], target_entries: list[_IndexEntry]
) -> str:
    source_count, target_count = len(source_entries), len(target_entries)
    if target_count < source_count:
        kind = "suspected merge"
    else:
        kind = "suspected split"
    source_anchor_sequence = [marker for entry in source_entries for marker in entry.markers]
    target_anchor_sequence = [marker for entry in target_entries for marker in entry.markers]
    if source_anchor_sequence != target_anchor_sequence:
        kind = "page/sequence marker structure changed"
    source_line = source_entries[0].start_line if source_entries else 0
    target_line = target_entries[0].start_line if target_entries else 0
    if source_entries and target_entries and source_count != target_count:
        shared_count = min(source_count, target_count)
        source_unpaged = next((entry for entry in source_entries if not entry.markers), None)
        target_unpaged = next((entry for entry in target_entries if not entry.markers), None)
        if source_count > target_count and source_unpaged:
            kind = "unpaged/prose-only entry boundary changed"
            source_line, target_line = source_unpaged.start_line, target_entries[0].start_line
        elif target_count > source_count and target_unpaged:
            kind = "unpaged/prose-only entry boundary changed"
            source_line, target_line = source_entries[0].start_line, target_unpaged.start_line
        source_tail = source_entries[shared_count] if source_count > shared_count else None
        target_tail = target_entries[shared_count] if target_count > shared_count else None
        if source_tail and source_tail.group_reason == "cross-reference cue":
            kind = "cross-reference missing from target"
            source_line, target_line = source_tail.start_line, target_entries[shared_count - 1].start_line
        elif target_tail and target_tail.group_reason == "cross-reference cue":
            kind = "cross-reference added in target"
            source_line, target_line = source_entries[shared_count - 1].start_line, target_tail.start_line
    if source_entries and target_entries:
        for source_entry, target_entry in zip(source_entries, target_entries):
            if (
                source_entry.group_reason == "cross-reference cue"
                and target_entry.group_reason != "cross-reference cue"
            ):
                kind = "cross-reference boundary changed"
                source_line, target_line = source_entry.start_line, target_entry.start_line
                break
            if (
                target_entry.group_reason == "cross-reference cue"
                and source_entry.group_reason != "cross-reference cue"
            ):
                kind = "cross-reference boundary changed"
                source_line, target_line = source_entry.start_line, target_entry.start_line
                break
        source_owners = [
            (entry_index, entry, marker)
            for entry_index, entry in enumerate(source_entries)
            for marker in entry.markers
        ]
        target_owners = [
            (entry_index, entry, marker)
            for entry_index, entry in enumerate(target_entries)
            for marker in entry.markers
        ]
        for (source_owner, source_entry, source_marker), (
            target_owner, target_entry, target_marker
        ) in zip(source_owners, target_owners):
            if kind.startswith("cross-reference") or kind.startswith("unpaged/prose-only"):
                break
            if (
                source_marker != target_marker
                or source_owner != target_owner
            ):
                source_line, target_line = source_entry.start_line, target_entry.start_line
                break
        if source_line == source_entries[0].start_line and target_line == target_entries[0].start_line:
            for index, (source_entry, target_entry) in enumerate(zip(source_entries, target_entries)):
                if source_entry.group_reason != target_entry.group_reason:
                    source_line, target_line = source_entry.start_line, target_entry.start_line
                    break
            else:
                if source_count > target_count:
                    source_line = source_entries[target_count].start_line
                    target_line = target_entries[-1].start_line
                elif target_count > source_count:
                    source_line = source_entries[-1].start_line
                    target_line = target_entries[source_count].start_line
        src = next((entry for entry in source_entries if entry.start_line == source_line), source_entries[0])
        tgt = next((entry for entry in target_entries if entry.start_line == target_line), target_entries[0])
        evidence = (
            f"source line {src.start_line} (indent {src.indent}, {src.group_reason}); "
            f"target line {tgt.start_line} (indent {tgt.indent}, {tgt.group_reason})"
        )
    else:
        evidence = f"source line {source_line}; target line {target_line}"
    source_start = max(0, next((i for i, entry in enumerate(source_entries) if entry.start_line == source_line), 0) - 1)
    target_start = max(0, next((i for i, entry in enumerate(target_entries) if entry.start_line == target_line), 0) - 1)
    nearby_source = source_entries[source_start:source_start + 3]
    nearby_target = target_entries[target_start:target_start + 3]
    source_preview = " | ".join(f"L{e.start_line}: {e.text[:70]}" for e in nearby_source)
    target_preview = " | ".join(f"L{e.start_line}: {e.text[:70]}" for e in nearby_target)
    source_indent_pattern = _index_indent_depths(source_entries[:8])
    target_indent_pattern = _index_indent_depths(target_entries[:8])
    if any(
        source_depth != target_depth
        for source_depth, target_depth in zip(source_indent_pattern, target_indent_pattern)
    ):
        kind += " with indentation pattern change"
    return (
        f"index entry count mismatch ({kind}): source={source_count}, "
        f"target={target_count}; {evidence}; "
        f"nearby source [{source_preview}], target [{target_preview}]"
    )


def _index_indent_depths(entries: list[_IndexEntry]) -> list[int]:
    """Normalize absolute spaces into a relative hierarchy for entry rows."""
    stack: list[int] = []
    depths: list[int] = []
    for entry in entries:
        while stack and entry.indent < stack[-1]:
            stack.pop()
        if not stack or entry.indent > stack[-1]:
            stack.append(entry.indent)
        depths.append(max(0, len(stack) - 1))
    return depths
