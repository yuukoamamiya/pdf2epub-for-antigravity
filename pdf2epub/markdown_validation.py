"""Markdown validation helpers for Subagent-produced translation files."""

from __future__ import annotations

import re
from typing import Any, Dict, Optional


_CHINESE_TARGET_LANGUAGE_ALIASES = {
    "chinese",
    "中文",
    "简体中文",
    "繁体中文",
    "zh",
    "zh-cn",
    "zh-hans",
    "zh-hant",
    "中文（简体）",
    "中文（繁體）",
}
_CJK_CHAR_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")
_LATIN_CHAR_RE = re.compile(r"[A-Za-z]")
_LANGUAGE_AUDIT_MIN_SOURCE_LETTERS = 120
_LANGUAGE_AUDIT_MIN_TARGET_LETTERS = 80
_LANGUAGE_AUDIT_MIN_CJK_RATIO = 0.60


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

_SPECIAL_ROLE_NUMERIC_MARKER_RE = re.compile(
    r"(?<![A-Za-z0-9])"
    r"\d+(?:[./:]\d+)*(?:[-‐‑‒–—]\d+(?:[./:]\d+)*)*"
    r"(?![A-Za-z0-9])"
)


def _special_role_numeric_markers(text: str) -> list[str]:
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
    return [
        re.sub(r"[‐‑‒–—]", "-", match.group(0))
        for match in _SPECIAL_ROLE_NUMERIC_MARKER_RE.finditer(normalized)
    ]


def _validate_special_role_markers(
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
    source_markers = _special_role_numeric_markers(source_text)
    target_markers = _special_role_numeric_markers(target_text)
    if source_markers == target_markers:
        return []

    if allow_page_furniture_deletion:
        import difflib
        from .page_furniture_repair import _candidate_for_line

        furniture_numbers = []
        for line in source_text.splitlines():
            if _candidate_for_line(line):
                furniture_numbers.extend(_special_role_numeric_markers(line))
        matcher = difflib.SequenceMatcher(None, source_markers, target_markers)
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


def fix_reference_heading_mismatch(
    source_text: str, target_text: str
) -> tuple[str, list[dict[str, int | str]]]:
    """Remove only a high-confidence, accidentally added references heading.

    A translation may legitimately add or remove Markdown headings elsewhere,
    so this helper is intentionally conservative.  It acts only when there is
    exactly one extra target heading, the source contains a plain standalone
    references label, and a translated references label appears near the same
    line near the end of the unit.
    """
    heading_pattern = re.compile(r"^(\s*)(#{1,6})(\s+)(.+?)(\s*)$")
    source_lines = source_text.splitlines()
    target_lines = target_text.splitlines()
    source_heading_count = sum(
        bool(re.match(r"^\s*#{1,6}\s+", line)) for line in source_lines
    )
    target_headings = [
        (index, match)
        for index, line in enumerate(target_lines)
        if (match := heading_pattern.match(line))
    ]
    if len(target_headings) != source_heading_count + 1:
        return target_text, []

    source_candidates = [
        index
        for index, line in enumerate(source_lines)
        if not re.match(r"^\s*#{1,6}\s+", line)
        and _is_reference_label(line)
        and index >= max(0, int(len(source_lines) * 0.4))
    ]
    target_candidates = [
        (index, match)
        for index, match in target_headings
        if _is_reference_label(match.group(4), target=True)
        and index >= max(0, int(len(target_lines) * 0.4))
    ]
    if len(source_candidates) != 1 or len(target_candidates) != 1:
        return target_text, []

    source_index = source_candidates[0]
    target_index, target_match = target_candidates[0]
    if abs(source_index - target_index) > 8:
        return target_text, []

    target_lines[target_index] = (
        target_match.group(1)
        + target_match.group(4)
        + target_match.group(5)
    )
    trailing_newline = "\n" if target_text.endswith("\n") else ""
    return "\n".join(target_lines) + trailing_newline, [
        {
            "source_line": source_index + 1,
            "target_line": target_index + 1,
            "removed_level": len(target_match.group(2)),
            "reason": "plain references label was upgraded to a Markdown heading",
        }
    ]

def _heading_reduction_is_duplicate_only(source_text: str, target_text: str) -> bool:
    """Allow polishing to remove only headings duplicated in the source.

    This covers running headers repeated across a page boundary without
    silently accepting the loss of a unique section heading.
    """
    from collections import Counter

    heading_pattern = re.compile(r"^(#{1,6})\s+(.+?)\s*$", re.MULTILINE)

    def signatures(text: str) -> Counter:
        return Counter(
            (len(match.group(1)), re.sub(r"\s+", " ", match.group(2)).strip().casefold())
            for match in heading_pattern.finditer(text)
        )

    source = signatures(source_text)
    target = signatures(target_text)
    if sum(source.values()) <= sum(target.values()):
        return False
    missing = source - target
    return bool(missing) and all(source[signature] >= 2 for signature in missing)


def strip_outer_markdown_fences(text: str) -> tuple[str, bool]:
    """Remove only a wrapping Markdown fence accidentally added by a Subagent.

    Internal fences are left untouched and still fail validation.  This narrow
    cleanup handles the common case where the model wraps the whole file in a
    ````markdown`` block, without changing source code or mathematical content.
    """
    lines = text.splitlines(keepends=True)
    nonempty = [index for index, line in enumerate(lines) if line.strip()]
    if len(nonempty) < 3:
        return text, False
    first, last = nonempty[0], nonempty[-1]
    if not re.fullmatch(r"```(?:markdown|md)?\s*", lines[first].strip(), re.IGNORECASE):
        return text, False
    if lines[last].strip() != "```":
        return text, False
    cleaned = "".join(lines[:first] + lines[first + 1:last] + lines[last + 1:])
    return cleaned, True


def _language_audit_text(text: str) -> str:
    """Remove non-prose regions before measuring target-language density."""
    text = re.sub(r"```.*?```", " ", text, flags=re.DOTALL)
    text = re.sub(r"!?\[[^\]]*\]\([^)]*\)", " ", text)
    text = re.sub(r"https?://\S+|www\.\S+", " ", text, flags=re.IGNORECASE)
    text = re.sub(r"`[^`]*`", " ", text)
    text = re.sub(r"\$\$?.*?\$\$?", " ", text, flags=re.DOTALL)
    return text


def _is_chinese_target_language(target_language: Optional[str]) -> bool:
    if not target_language:
        return False
    normalized = re.sub(r"\s+", " ", str(target_language).strip().casefold())
    return normalized in {value.casefold() for value in _CHINESE_TARGET_LANGUAGE_ALIASES}


def target_language_ratio_check(
    source_text: str,
    target_text: str,
    target_language: Optional[str],
    *,
    threshold: float = _LANGUAGE_AUDIT_MIN_CJK_RATIO,
    min_source_letters: int = _LANGUAGE_AUDIT_MIN_SOURCE_LETTERS,
    min_target_letters: int = _LANGUAGE_AUDIT_MIN_TARGET_LETTERS,
) -> Dict[str, Any]:
    """Audit Chinese target-language density without judging special units.

    This is intentionally scoped to Chinese for now.  Other target languages
    need their own script detectors; treating Latin text as untranslated would
    incorrectly reject translations between European languages.
    """
    result: Dict[str, Any] = {
        "applicable": _is_chinese_target_language(target_language),
        "target_language": str(target_language or ""),
        "threshold": threshold,
        "source_latin_letters": 0,
        "target_cjk_letters": 0,
        "target_latin_letters": 0,
        "target_letters": 0,
        "target_cjk_ratio": None,
        "blocked": False,
    }
    if not result["applicable"]:
        return result

    source_visible = _language_audit_text(source_text)
    target_visible = _language_audit_text(target_text)
    source_latin = len(_LATIN_CHAR_RE.findall(source_visible))
    target_cjk = len(_CJK_CHAR_RE.findall(target_visible))
    target_latin = len(_LATIN_CHAR_RE.findall(target_visible))
    target_letters = target_cjk + target_latin
    result.update(
        {
            "source_latin_letters": source_latin,
            "target_cjk_letters": target_cjk,
            "target_latin_letters": target_latin,
            "target_letters": target_letters,
        }
    )
    if source_latin < min_source_letters or target_letters < min_target_letters:
        return result
    ratio = target_cjk / target_letters
    result["target_cjk_ratio"] = round(ratio, 4)
    if ratio < threshold:
        result["blocked"] = True
        result["reason"] = (
            "untranslated_source_detected: target-language density is below "
            f"{threshold:.0%} ({ratio:.1%})"
        )
    return result


def detect_polish_page_furniture(text: str) -> list[Dict[str, Any]]:
    """Report high-confidence page-furniture candidates left after polishing.

    The candidates are review signals.  Layout judgment belongs to the
    polishing Subagent, so this function never edits text; the caller decides
    whether an unresolved candidate blocks the hand-off.
    """
    from .page_furniture_repair import _candidate_for_line

    findings: list[Dict[str, Any]] = []
    for line_number, line in enumerate(text.splitlines(), 1):
        candidate = _candidate_for_line(line)
        if not candidate or candidate.get("confidence") != "high":
            continue
        findings.append({"line": line_number, **candidate})
    return findings


def translation_diff_summary(source_text: str, target_text: str) -> Dict[str, Any]:
    """Return structural and translation-risk counters for a Markdown unit."""
    heading_pattern = re.compile(r"^#{1,6}\s", re.MULTILINE)
    warning = detect_bilingual_output(source_text, target_text)
    return {
        "source_line_count": len(source_text.splitlines()),
        "target_line_count": len(target_text.splitlines()),
        "line_count_changed": len(source_text.splitlines()) != len(target_text.splitlines()),
        "source_heading_count": len(heading_pattern.findall(source_text)),
        "target_heading_count": len(heading_pattern.findall(target_text)),
        "heading_count_changed": len(heading_pattern.findall(source_text)) != len(heading_pattern.findall(target_text)),
        "source_code_fence_count": source_text.count("```") ,
        "target_code_fence_count": target_text.count("```") ,
        "code_fence_changes": source_text.count("```") != target_text.count("```"),
        "unchanged_english_spans": 1 if warning else 0,
    }


def detect_bilingual_output(source_text: str, target_text: str) -> Optional[Dict[str, Any]]:
    """Report when a translation appears to contain a long unchanged source span.

    This is deliberately advisory: names, formulas, URLs and references can be
    legitimately unchanged, so the validator exposes the exact span for
    Subagent/human review instead of silently treating it as a translation
    failure.  The Markdown hand-off validator may turn this signal into a
    ``review_required`` gate for ordinary translation units.
    """
    source_lines = source_text.splitlines()
    target_lines = target_text.splitlines()
    unchanged = []
    for index, (source_line, target_line) in enumerate(zip(source_lines, target_lines), 1):
        source_line = source_line.strip()
        target_line = target_line.strip()
        if len(source_line) < 80 or source_line != target_line:
            if unchanged:
                break
            continue
        letters = re.sub(r"[^A-Za-z]", "", source_line)
        if len(letters) >= 60:
            unchanged.append(index)
        elif unchanged:
            break
    if len(unchanged) >= 2:
        return {
            "reason": "long unchanged English source span; possible bilingual output",
            "start_line": unchanged[0],
            "end_line": unchanged[-1],
        }
    return None


__all__ = [
    "detect_bilingual_output",
    "detect_polish_page_furniture",
    "fix_reference_heading_mismatch",
    "strip_outer_markdown_fences",
    "target_language_ratio_check",
    "translation_diff_summary",
]
