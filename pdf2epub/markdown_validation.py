"""Markdown validation helpers for Subagent-produced translation files."""

from __future__ import annotations

import difflib
import math
import re
from collections import Counter
from typing import Any, Dict, Iterable, Optional

from .document_roles import (
    DENSITY_EXEMPT_ROLES,
    TRANSLATION_SPECIAL_ROLES,
    _is_reference_label,
    infer_translation_file_role,
    is_bibliography_text,
    is_density_exempt_role,
    is_front_matter_text,
)
from .page_furniture_rules import candidate_for_line
from .utils.ocr_artifacts import clean_ocr_page_artifacts


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


_SPECIAL_ROLE_NUMERIC_MARKER_RE = re.compile(
    r"(?<![A-Za-z0-9])"
    r"\d+(?:[./:]\d+)*(?:[-‐‑‒–—]\d+(?:[./:]\d+)*)*"
    r"(?![A-Za-z0-9])"
)

_NOTES_STRUCTURAL_HEADING_RE = re.compile(
    r"^#{2,6}\s+(?:"
    r"(?:chapter|notes?\s+to\s+chapter)\s+\d+(?:[.\-]\d+)*"
    r"|第\s*\d+(?:[.．]\d+)*\s*章"
    r")\s*$",
    re.IGNORECASE,
)

_POLISH_HARD_BREAK_RE = re.compile(r"<br\b[^>]*?/?>", re.IGNORECASE)
_POLISH_CODE_FENCE_RE = re.compile(r"^\s*(```|~~~)")
_POLISH_HEADING_RE = re.compile(r"^\s{0,3}#{1,6}\s+\S")
_POLISH_LIST_RE = re.compile(r"^\s*(?:[-+*]|\d+[.)])\s+\S")
_POLISH_FOOTNOTE_DEFINITION_RE = re.compile(r"^\s*\[\^[^\]]+\]:")
_POLISH_IMAGE_RE = re.compile(r"^\s*!\[[^\]]*\]\([^)]*\)\s*$")
_POLISH_HORIZONTAL_RULE_RE = re.compile(r"^\s*(?:\*\s*){3,}$|^\s*(?:-\s*){3,}$|^\s*(?:_\s*){3,}$", re.MULTILINE)


def _polish_markdown_blocks(text: str) -> list[list[str]]:
    """Split Markdown into blank-line blocks for the polish structure audit."""
    blocks: list[list[str]] = []
    current: list[str] = []
    for line in clean_ocr_page_artifacts(str(text or "")).splitlines():
        if line.strip():
            current.append(line)
        elif current:
            blocks.append(current)
            current = []
    if current:
        blocks.append(current)
    return blocks


def _polish_block_visible_text(lines: list[str]) -> str:
    """Return a conservative visible-text approximation for one Markdown block."""
    value = "\n".join(lines)
    value = re.sub(r"!\[[^\]]*\]\([^)]*\)", " ", value)
    value = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", value)
    value = _POLISH_HARD_BREAK_RE.sub(" ", value)
    value = re.sub(r"<[^>]+>", " ", value)
    value = re.sub(r"^\s{0,3}#{1,6}\s+", "", value, flags=re.MULTILINE)
    value = re.sub(r"^\s*(?:[-+*]|\d+[.)])\s+", "", value, flags=re.MULTILINE)
    value = re.sub(r"[*_`~]", "", value)
    return re.sub(r"\s+", " ", value).strip()


def _polish_block_is_structural(lines: list[str]) -> bool:
    """Exclude blocks whose short lines are intentional Markdown structure."""
    stripped = [line.strip() for line in lines if line.strip()]
    if not stripped:
        return True
    if any(_POLISH_CODE_FENCE_RE.match(line) for line in stripped):
        return True
    if all(
        _POLISH_HEADING_RE.match(line)
        or _POLISH_IMAGE_RE.match(line)
        or _POLISH_HORIZONTAL_RULE_RE.match(line)
        or _POLISH_FOOTNOTE_DEFINITION_RE.match(line)
        for line in stripped
    ):
        return True
    if all(line.startswith(">") for line in stripped):
        return True
    if all(_POLISH_LIST_RE.match(line) for line in stripped):
        return True
    if len(stripped) >= 2 and all(
        line.startswith("|") or re.fullmatch(r"\s*[:| -]+\s*", line)
        for line in stripped
    ):
        return True
    return False


def polish_structure_audit(
    source_text: str,
    target_text: str,
    role: Optional[str] = None,
) -> Dict[str, Any]:
    """Audit polish output for visual-line paragraph fragmentation.

    The audit is intentionally advisory/review-gated rather than an automatic
    rewriter.  Short one-line blocks can be aphorisms, dialogue, or verse, so
    the validator only asks the Subagent to review them.  Explicit ``<br />``
    blocks are treated as an intentional line-structure representation and do
    not count as ordinary paragraph fragments.  Bibliography, index, notes,
    catalog, advertisement, TOC, and other declared special units are exempt
    because their line/entry boundaries carry meaning of their own.
    """

    effective_role = str(role or "").strip().lower() or None
    if effective_role is None and is_bibliography_text(source_text):
        effective_role = "bibliography"

    source_blocks = _polish_markdown_blocks(source_text)
    target_blocks = _polish_markdown_blocks(target_text)

    heading_pattern = re.compile(r"^\s{0,3}(#{1,6})\s+(.+?)\s*$", re.MULTILINE)

    def heading_tokens(text: str) -> list[str]:
        value = re.sub(r"<[^>]+>", " ", str(text or ""))
        value = re.sub(r"[`*_~]", "", value)
        value = re.sub(r"^\s*\d{1,4}\s+", "", value)
        return re.findall(r"\w+", value.casefold(), flags=re.UNICODE)

    source_headings = [
        (len(match.group(1)), heading_tokens(match.group(2)), match.group(2))
        for match in heading_pattern.finditer(source_text)
    ]
    target_headings = [
        (len(match.group(1)), heading_tokens(match.group(2)), match.group(2))
        for match in heading_pattern.finditer(target_text)
    ]
    heading_append_findings: list[dict[str, str]] = []
    for index, (source_heading, target_heading) in enumerate(
        zip(source_headings, target_headings), 1
    ):
        source_level, source_words, source_label = source_heading
        target_level, target_words, target_label = target_heading
        if (
            source_level != target_level
            or len(source_words) < 2
            or len(target_words) - len(source_words) < 4
            or target_words[: len(source_words)] != source_words
        ):
            continue
        heading_append_findings.append(
            {
                "heading_index": str(index),
                "source_heading": source_label[:160],
                "target_heading": target_label[:240],
            }
        )

    def metrics(blocks: list[list[str]]) -> Dict[str, Any]:
        candidate_blocks = 0
        single_line_blocks = 0
        short_blocks = 0
        hard_break_blocks = 0
        hard_break_count = 0
        samples: list[str] = []
        for lines in blocks:
            if _polish_block_is_structural(lines):
                continue
            visible = _polish_block_visible_text(lines)
            if not visible:
                continue
            hard_breaks = _POLISH_HARD_BREAK_RE.findall("\n".join(lines))
            hard_break_count += len(hard_breaks)
            if hard_breaks:
                hard_break_blocks += 1
                if len(samples) < 5:
                    samples.append(visible[:160])
                continue
            candidate_blocks += 1
            if len(lines) == 1:
                single_line_blocks += 1
            visible_length = len(re.sub(r"\s+", "", visible))
            word_count = len(re.findall(r"\w+", visible, flags=re.UNICODE))
            if visible_length <= 90 or word_count <= 14:
                short_blocks += 1
        return {
            "block_count": len(blocks),
            "candidate_block_count": candidate_blocks,
            "single_line_block_count": single_line_blocks,
            "short_block_count": short_blocks,
            "single_line_ratio": round(
                single_line_blocks / max(1, candidate_blocks), 6
            ),
            "short_block_ratio": round(short_blocks / max(1, candidate_blocks), 6),
            "explicit_hard_break_count": hard_break_count,
            "intentional_line_structure_candidates": hard_break_blocks,
            "intentional_line_structure_samples": samples,
        }

    source_metrics = metrics(source_blocks)
    target_metrics = metrics(target_blocks)
    result: Dict[str, Any] = {
        "applicable": effective_role not in TRANSLATION_SPECIAL_ROLES,
        "status": "ok",
        "role": effective_role,
        "source": source_metrics,
        **target_metrics,
        "heading_content_appends": heading_append_findings,
    }
    if not result["applicable"]:
        result["status"] = "exempt"
        result["reason"] = (
            f"special Markdown unit role {effective_role!r} is exempt from "
            "ordinary prose paragraph-fragmentation checks"
        )
        return result

    candidate_count = int(target_metrics["candidate_block_count"])
    single_ratio = float(target_metrics["single_line_ratio"])
    short_ratio = float(target_metrics["short_block_ratio"])
    if heading_append_findings:
        result["status"] = "review"
        result["reason"] = (
            "polish heading structure: body prose may have been appended to "
            f"{len(heading_append_findings)} heading(s); keep headings as "
            "heading-only blocks and restore trailing prose to its paragraph"
        )
    if candidate_count >= 8 and single_ratio >= 0.60 and short_ratio >= 0.60:
        result["status"] = "review"
        fragmentation_reason = (
            "polish paragraph fragmentation: target contains "
            f"{target_metrics['single_line_block_count']}/{candidate_count} "
            "ordinary candidate blocks as short single-line blocks "
            f"(single-line ratio {single_ratio:.1%}, short-block ratio "
            f"{short_ratio:.1%}); merge visual line wraps in ordinary prose, "
            "but preserve verse/dialogue/epigraph lines inside one block with "
            "explicit <br /> breaks"
        )
        if result.get("reason"):
            result["reason"] += "; " + fragmentation_reason
        else:
            result["reason"] = fragmentation_reason
    return result


from .special_role_audit import (
    special_role_numeric_markers as _special_role_numeric_markers,
    validate_special_role_markers as _validate_special_role_markers,
)


def _validate_footnote_markers(source_text: str, target_text: str) -> list[str]:
    """Require translated Markdown to preserve exact footnote marker syntax."""
    marker_re = re.compile(r"\[\^([A-Za-z0-9_-]+)\]")
    source_markers = [match.group(1) for match in marker_re.finditer(source_text)]
    target_markers = [match.group(1) for match in marker_re.finditer(target_text)]
    if source_markers != target_markers:
        return [
            "translate footnote marker mismatch: "
            f"source={source_markers!r}, target={target_markers!r}"
        ]
    if source_markers:
        for key in dict.fromkeys(source_markers):
            escaped = re.escape(key)
            target_sup_count = len(
                re.findall(
                    rf"<sup\b[^>]*>\s*{escaped}\s*</sup>",
                    target_text,
                    flags=re.IGNORECASE,
                )
            )
            source_sup_count = len(
                re.findall(
                    rf"<sup\b[^>]*>\s*{escaped}\s*</sup>",
                    source_text,
                    flags=re.IGNORECASE,
                )
            )
            if target_sup_count > source_sup_count or re.search(
                rf"\[(?:注|note)\s*{escaped}\]",
                target_text,
                flags=re.IGNORECASE,
            ):
                return [
                    f"translate footnote marker {key} changed from Markdown "
                    "[^N] syntax to an alternate notation"
                ]
    return []


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


def _strip_trailing_references(text: str) -> str:
    """Strip trailing bibliography/references section from text for density audit."""
    lines = text.splitlines(keepends=True)
    total_lines = len(lines)
    if total_lines < 5:
        return text
    start_search = 1
    for i in range(start_search, total_lines):
        line = lines[i].strip()
        if not line:
            continue
        if _is_reference_label(line) or _is_reference_label(line, target=True):
            tail = "".join(lines[i:])
            tail_blocks = [b.strip() for b in tail.split("\n\n") if b.strip()]
            if len(tail_blocks) >= 2:
                cite_re = re.compile(
                    r"\(\d{4}[a-z]?\)|https?://|doi\.org|\b(?:18|19|20)\d{2}[a-z]?\b",
                    re.IGNORECASE,
                )
                cite_count = sum(1 for b in tail_blocks[1:] if cite_re.search(b))
                if cite_count >= 1 and cite_count / max(1, len(tail_blocks) - 1) >= 0.4:
                    return "".join(lines[:i])
            elif len(tail_blocks) == 1:
                return "".join(lines[:i])
    return text


def _language_audit_text(text: str) -> str:
    """Remove non-prose regions before measuring target-language density."""
    text = _strip_trailing_references(text)
    text = re.sub(r"```.*?```", " ", text, flags=re.DOTALL)
    text = re.sub(r"!?\[[^\]]*\]\([^)]*\)", " ", text)
    text = re.sub(r"https?://\S+|www\.\S+", " ", text, flags=re.IGNORECASE)
    text = re.sub(r"`[^`]*`", " ", text)
    text = re.sub(r"\$\$?.*?\$\$?", " ", text, flags=re.DOTALL)
    text = re.sub(r"<[^>]+>", " ", text)
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


def detect_polish_page_furniture(
    text: str,
    role: Optional[str] = None,
    *,
    protected_toc_titles: Optional[Iterable[str]] = None,
) -> list[Dict[str, Any]]:
    """Report high-confidence page-furniture candidates left after polishing.

    The candidates are review signals.  Layout judgment belongs to the
    polishing Subagent, so this function never edits text; the caller decides
    whether an unresolved candidate blocks the hand-off.

    Registered TOC labels are exempt from the generic ``title + Roman numeral``
    heuristic only when they appear as Markdown headings: ``# PART I`` is a
    legitimate book-part heading, while a plain-text ``PART I`` may still be a
    repeated running header and remains a candidate.
    """
    # Short front-matter units often contain identifiers and address lines that
    # look like a numbered running header.  Their structure is already owned
    # by the TOC/refinement stage, so this paragraph-level heuristic should not
    # create a second, unrelated review gate for them.
    if str(role or "").strip().lower() in {
        "author_notes",
        "metadata",
        "frontmatter",
        "copyright",
        "cover",
    }:
        return []

    def repeat_key(candidate: Dict[str, Any]) -> str:
        """Normalize a candidate for repeated-header detection.

        Bibliography and index units contain legitimate short, punctuation-free
        titles and page labels.  For those roles, a page-furniture signal is
        useful only when it repeats.  A changing printed page number must not
        prevent two copies of the same running title from being recognized as
        a repeat.
        """
        value = str(candidate.get("text") or "")
        if candidate.get("kind") == "running_title_plus_page_label":
            value = re.sub(
                r"\s+(?:\d{1,4}|[ivxlcdm]{1,12})$",
                "",
                value,
                flags=re.IGNORECASE,
            )
        return re.sub(r"\s+", " ", value).strip().casefold()

    def normalize_toc_label(value: str) -> str:
        normalized = re.sub(r"^\s*#{1,6}\s+", "", str(value or "").strip())
        normalized = re.sub(r"[*_`]+", "", normalized).strip()
        return re.sub(r"\s+", " ", normalized).casefold()

    protected_labels = {
        normalize_toc_label(title)
        for title in (protected_toc_titles or ())
        if normalize_toc_label(title)
    }

    lines = text.splitlines()
    candidate_records = [
        candidate_for_line(line)
        for line in lines
    ]
    repeated_candidate_keys = Counter(
        repeat_key(candidate)
        for candidate in candidate_records
        if candidate
        and candidate.get("kind")
        in {"running_header", "running_title_plus_page_label"}
    )

    findings: list[Dict[str, Any]] = []
    special_role = role in {"bibliography", "index", "toc"}
    for line_number, (line, candidate) in enumerate(
        zip(lines, candidate_records), 1
    ):
        if not candidate or candidate.get("confidence") != "high":
            continue
        if (
            candidate.get("kind") == "running_title_plus_page_label"
            and protected_labels
            and re.match(r"^\s*#{1,6}\s+", line)
        ):
            line_label = normalize_toc_label(line)
            if line_label in protected_labels or any(
                title.startswith(f"{line_label}:")
                for title in protected_labels
            ):
                continue
        # Endnotes commonly repeat structural headings such as ``## Chapter
        # 1``.  The generic candidate detector sees the trailing number and
        # calls them running-title-plus-page-label lines.  Exempt only this
        # narrow Markdown-heading shape; ordinary notes page furniture still
        # goes through the normal review gate.
        if (
            role == "notes"
            and candidate.get("kind") == "running_title_plus_page_label"
            and _NOTES_STRUCTURAL_HEADING_RE.fullmatch(line.strip())
        ):
            continue
        if special_role:
            kind = candidate.get("kind")
            # Index entries commonly have the form "term page" and the same
            # term may legitimately occur with several page references.  In
            # that role, a title-plus-page candidate is content, not enough
            # evidence of a running header/footer.  Repeated plain running
            # headers are still checked below.
            if role == "index" and kind == "running_title_plus_page_label":
                continue
            # A standalone number, publisher imprint, or one-off title-like
            # line is ordinary bibliography/index content until repetition
            # supplies independent page-layout evidence.  This deliberately
            # keeps page numbers and source titles out of the furniture gate.
            if kind not in {"running_header", "running_title_plus_page_label"}:
                continue
            if repeated_candidate_keys.get(repeat_key(candidate), 0) < 2:
                continue
        if (
            candidate.get("kind") == "running_header"
            and line_number <= 5
            and not special_role
        ):
            continue
        findings.append({"line": line_number, **candidate})
    return findings


def polish_content_integrity_check(
    source_text: str,
    target_text: str,
    file_role: str | None = None,
) -> Dict[str, Any]:
    """Detect substantial source-content loss during full-text polishing.

    Polish is allowed to reflow paragraphs, remove confirmed page furniture,
    and normalize verified footnote superscripts. It is not allowed to drop a
    substantial amount of visible prose, numbers, or punctuation. This check
    is deliberately a blocking signal so an accidental truncated Subagent
    output cannot become the next source stage.

    Note-definition prefixes are only normalized for ``notes`` units. Ordinary
    body text keeps strict list-number comparison so a dropped ``- 1.`` prefix
    still counts as content loss.
    """

    notes_role = str(file_role or "").strip().lower() == "notes"

    def tokens(text: str) -> list[str]:
        visible_lines: list[str] = []
        for line_number, line in enumerate(clean_lines(text), 1):
            candidate = candidate_for_line(line)
            if candidate and candidate.get("confidence") == "high":
                if candidate.get("ornamented"):
                    continue
                if candidate.get("kind") == "running_header" and line_number <= 5:
                    pass
                else:
                    continue
            line = re.sub(
                r"\s+(?:Introduction|Transforming Translation|ChatGPT in Professional Translation|Ethical Issues in Translation Education in the AI Era|Contributors)\s*(?:<sup\b[^>]*>\s*)?(?:\d{1,4}|[ivxlcdm]{1,12})(?:\s*</sup>)?$",
                "",
                line,
                flags=re.IGNORECASE,
            )
            line = re.sub(
                r"\s+(?:<sup\b[^>]*>\s*)?(?:\d{1,4}|[ivxlcdm]{1,12})(?:\s*</sup>)?\s+Translation Studies in the Age of Artificial Intelligence$",
                "",
                line,
                flags=re.IGNORECASE,
            )
            visible_lines.append(line)
        value = "\n".join(visible_lines)
        value = re.sub(r"([A-Za-zÄÖÜäöüß]+)[-‐‑‒–—][ \t]*\n[ \t]*([A-Za-zÄÖÜäöüß]+)", r"\1\2", value)
        value = re.sub(r"!\[[^\]]*\]\([^)]*\)", " ", value)
        value = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", value)
        value = re.sub(r"<sup>\s*(\d+)\s*</sup>", r"[^\1]", value, flags=re.IGNORECASE)
        if notes_role:
            value = re.sub(
                r"^\s*(?:[-*+]\s*)?(\d+)\.\s*",
                r"[^\1]: ",
                value,
                flags=re.MULTILINE,
            )
        value = re.sub(r"\[\^\s*(\d+)\s*\](?::)?", r"[^\1]", value)
        value = re.sub(r"<[^>]+>", " ", value)
        value = re.sub(r"^\s*#{1,6}\s+", "", value, flags=re.MULTILINE)
        value = _POLISH_HORIZONTAL_RULE_RE.sub("", value)
        value = value.replace("**", "").replace("__", "")
        value = value.replace("*", "").replace("_", "")
        value = re.sub(r"\s+", " ", value).strip()
        return re.findall(r"\w+|[^\w\s]", value, flags=re.UNICODE)

    def clean_lines(text: str) -> list[str]:
        return clean_ocr_page_artifacts(str(text or "")).splitlines()

    def prose_blocks(text: str) -> list[str]:
        """Extract substantial prose blocks while ignoring heading-only lines.

        A structural parent heading may legitimately have no body.  If a
        polish worker copies a child's paragraph under that empty parent, the
        same prose block appears twice in the target even though all source
        tokens are still present.  This lightweight check catches that case
        without treating repeated headings or short labels as duplication.
        """
        blocks: list[str] = []
        current: list[str] = []
        heading_pattern = re.compile(r"^\s{0,3}#{1,6}\s+\S")

        def flush() -> None:
            if not current:
                return
            value = "\n".join(current)
            value = re.sub(r"([A-Za-zÄÖÜäöüß]+)[-‐‑‒–—][ \t]*\n[ \t]*([A-Za-zÄÖÜäöüß]+)", r"\1\2", value)
            value = re.sub(r"!\[[^\]]*\]\([^)]*\)", " ", value)
            value = re.sub(r"<sup>\s*\d+\s*</sup>", " ", value, flags=re.IGNORECASE)
            if notes_role:
                value = re.sub(
                    r"^\s*(?:[-*+]\s*)?\d+\.\s*", " ", value, flags=re.MULTILINE
                )
            value = re.sub(r"\[\^\s*\d+\s*\](?::)?", " ", value)
            value = re.sub(r"<[^>]+>", " ", value)
            value = _POLISH_HORIZONTAL_RULE_RE.sub("", value)
            value = re.sub(r"\s+", " ", value).strip()
            word_count = len(re.findall(r"\w+", value, flags=re.UNICODE))
            if len(value) >= 24 or word_count >= 8:
                blocks.append(value)
            current.clear()

        for line in clean_lines(text):
            if heading_pattern.match(line) or _POLISH_HORIZONTAL_RULE_RE.match(line):
                flush()
            elif line.strip():
                current.append(line)
            else:
                flush()
        flush()
        return blocks

    source_tokens = tokens(source_text)
    target_tokens = tokens(target_text)
    matcher = difflib.SequenceMatcher(None, source_tokens, target_tokens, autojunk=False)
    removed_tokens = 0
    removed_characters = 0
    for tag, i1, i2, _j1, _j2 in matcher.get_opcodes():
        if tag in {"delete", "replace"}:
            removed_tokens += i2 - i1
            removed_characters += sum(len(token) for token in source_tokens[i1:i2])

    numeric_token_re = re.compile(r"\d+(?:[./:-]\d+)*")
    source_numbers = [token for token in source_tokens if numeric_token_re.fullmatch(token)]
    target_numbers = [token for token in target_tokens if numeric_token_re.fullmatch(token)]
    target_number_iterator = iter(target_numbers)
    missing_numbers = [
        number
        for number in source_numbers
        if not any(candidate == number for candidate in target_number_iterator)
    ]
    loss_threshold = max(8, math.ceil(len(source_tokens) * 0.02))
    errors: list[str] = []
    if removed_tokens >= loss_threshold:
        ratio = removed_tokens / max(1, len(source_tokens))
        errors.append(
            "polish content integrity loss: "
            f"{removed_tokens}/{len(source_tokens)} source tokens ({ratio:.1%}) disappeared"
        )
    if missing_numbers:
        errors.append(
            "polish content integrity loss: source numeric markers are missing "
            f"from polished output: {missing_numbers[:12]!r}"
        )
    source_blocks = Counter(prose_blocks(source_text))
    target_blocks = Counter(prose_blocks(target_text))
    duplicated_blocks = [
        {
            "text": block[:160],
            "source_count": source_blocks[block],
            "target_count": target_blocks[block],
        }
        for block in sorted(target_blocks)
        if target_blocks[block] > 1 and target_blocks[block] > source_blocks[block]
    ]
    if duplicated_blocks:
        errors.append(
            "polish content integrity duplication: prose block appears more times "
            f"in polished output than in source ({len(duplicated_blocks)} block(s))"
        )
    return {
        "valid": not errors,
        "source_token_count": len(source_tokens),
        "target_token_count": len(target_tokens),
        "removed_token_count": removed_tokens,
        "removed_character_count": removed_characters,
        "removed_token_ratio": round(removed_tokens / max(1, len(source_tokens)), 6),
        "missing_numeric_markers": missing_numbers,
        "duplicated_prose_blocks": duplicated_blocks,
        "errors": errors,
    }


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
    "polish_content_integrity_check",
    "polish_structure_audit",
    "strip_outer_markdown_fences",
    "target_language_ratio_check",
    "translation_diff_summary",
]
