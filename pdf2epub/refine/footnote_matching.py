"""Locate OCR footnote blocks in refinement Markdown safely.

This module contains only source-span matching and block text normalization.
It never decides a footnote role and never writes a derived Markdown stage.
"""

from __future__ import annotations

import html
import json
import re
import string
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Mapping

from ..workflow_contracts import sha256_file


_MARKDOWN_REF_RE = re.compile(r"\[\^(?P<key>[A-Za-z0-9_-]+)\]")
_HTML_SUP_RE_TEMPLATE = r"<sup\b[^>]*>\s*{key}\s*</sup>"
_HTML_SUP_BLOCK_RE = re.compile(
    r"<sup\b[^>]*>.*?</sup>", re.IGNORECASE | re.DOTALL
)
_AUTO_LINK_RE = re.compile(
    r"<(?P<url>(?:https?://|mailto:)[^>\n]+)>", re.IGNORECASE
)
_SUPERSCRIPT_DIGITS = str.maketrans("⁰¹²³⁴⁵⁶⁷⁸⁹", "0123456789")
_CIRCLED_DIGITS_MAP = {
    "1": "①", "2": "②", "3": "③", "4": "④", "5": "⑤",
    "6": "⑥", "7": "⑦", "8": "⑧", "9": "⑨", "10": "⑩",
    "11": "⑪", "12": "⑫", "13": "⑬", "14": "⑭", "15": "⑮",
    "16": "⑯", "17": "⑰", "18": "⑱", "19": "⑲", "20": "⑳",
}


def plain_block_text(block: Mapping[str, Any]) -> str:
    """Return a compact text representation of one sidecar block."""
    value = block.get("text")
    if value is None:
        value = block.get("html", "")
    value = html.unescape(str(value or ""))
    value = _AUTO_LINK_RE.sub(r"\g<url>", value)
    value = re.sub(r"<[^>]+>", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def signature_with_map(value: str) -> tuple[str, list[int]]:
    """Normalize OCR/Markdown text while retaining source offsets."""
    value = html.unescape(str(value or ""))
    chars: list[str] = []
    positions: list[int] = []
    index = 0
    while index < len(value):
        char = value[index]
        if char == "[" and (index == 0 or value[index - 1] == "\n"):
            definition = re.match(
                r"\[\^([A-Za-z0-9_-]+)\]\s*:\s*",
                value[index:],
            )
            if definition:
                key = definition.group(1)
                key_start = index + 2
                chars.extend(key)
                positions.extend(range(key_start, key_start + len(key)))
                index += len(definition.group(0))
                if index < len(value) and value[index] != "\n":
                    chars.append(" ")
                    positions.append(max(key_start + len(key), index - 1))
                continue
        if char == "[":
            link_match = re.match(r"^\[([^\]\n]+)\]\(([^)\n]+)\)", value[index:])
            if link_match:
                anchor = link_match.group(1)
                anchor_start = index + 1
                for offset, c in enumerate(anchor):
                    if c in "*_~`$":
                        continue
                    if c.isspace():
                        if chars and chars[-1] != " ":
                            chars.append(" ")
                            positions.append(anchor_start + offset)
                    else:
                        chars.append(c)
                        positions.append(anchor_start + offset)
                index += len(link_match.group(0))
                continue
        if char == "\\" and index + 1 < len(value):
            if value[index + 1] in string.punctuation:
                index += 1
                continue
        if char == "<":
            auto_link_match = _AUTO_LINK_RE.match(value, index)
            if auto_link_match:
                url = auto_link_match.group("url")
                url_start = auto_link_match.start("url")
                chars.extend(url)
                positions.extend(
                    range(url_start, url_start + len(url))
                )
                index = auto_link_match.end()
                continue
            if re.match(r"^<https?://", value[index:]):
                index += 1
                continue
            tag_match = re.match(r"^</?[A-Za-z][\w:-]*\b[^>]*>", value[index:])
            if tag_match:
                index += len(tag_match.group(0))
                continue
            close = value.find(">", index + 1)
            if close >= 0:
                index = close + 1
                continue
        if char == ">" and index > 0 and value[index - 1] not in " \n":
            index += 1
            continue
        if char in "*_~`$":
            index += 1
            continue
        if char.isspace():
            if chars and chars[-1] != " ":
                chars.append(" ")
                positions.append(index)
        else:
            chars.append(char)
            positions.append(index)
        index += 1

    start = 0
    end = len(chars)
    while start < end and chars[start] == " ":
        start += 1
    while end > start and chars[end - 1] == " ":
        end -= 1
    return "".join(chars[start:end]), positions[start:end]


def _expand_markdown_definition_prefix(
    source: str, start: int, end: int
) -> tuple[int, int]:
    line_start = source.rfind("\n", 0, start) + 1
    prefix = re.match(r"\[\^[A-Za-z0-9_-]+\]\s*:\s*", source[line_start:])
    if prefix and line_start <= start <= line_start + len(prefix.group(0)):
        return line_start, end
    return start, end


def _is_cjk(ch: str) -> bool:
    return ("\u4e00" <= ch <= "\u9fff") or ("\u3040" <= ch <= "\u30ff")


def _is_signature_boundary(text: str, start: int, end: int) -> bool:
    left_ok = (
        (start == 0)
        or (not text[start - 1].isalnum())
        or _is_cjk(text[start - 1])
        or (start < len(text) and _is_cjk(text[start]))
    )
    right_ok = (
        (end >= len(text))
        or (not text[end].isalnum())
        or _is_cjk(text[end])
        or (end > 0 and _is_cjk(text[end - 1]))
    )
    return left_ok and right_ok


def _expand_mapped_span(source: str, start: int, end: int) -> tuple[int, int]:
    """Include formatting wrappers removed from a normalized signature."""
    opening_tags = {
        match.group(1).casefold()
        for match in re.finditer(
            r"<\s*([A-Za-z][\w:-]*)\b[^>]*>",
            source[start:end],
        )
    }
    while start > 0:
        if source[start - 1] in "*_~`":
            start -= 1
            continue
        if source[start - 1] == ">":
            tag_start = source.rfind("<", 0, start)
            if tag_start >= 0:
                match = re.fullmatch(
                    r"<\s*([A-Za-z][\w:-]*)\b[^>]*>",
                    source[tag_start:start],
                )
                if match:
                    opening_tags.add(match.group(1).casefold())
                    start = tag_start
                    continue
        break

    while end < len(source):
        if source[end] in "*_~`":
            end += 1
            continue
        if source[end] == "<":
            tag_end = source.find(">", end + 1)
            if tag_end >= 0:
                match = re.fullmatch(
                    r"<\s*/\s*([A-Za-z][\w:-]*)\s*>",
                    source[end : tag_end + 1],
                )
                if match and match.group(1).casefold() in opening_tags:
                    end = tag_end + 1
                    continue
        break
    return start, end


def find_exact_signature_spans(source: str, needle: str) -> list[tuple[int, int]]:
    """Return source spans for unique-boundary exact block matches."""
    source_signature, positions = signature_with_map(source)
    needle_signature, _ = signature_with_map(needle)
    if not source_signature or not needle_signature:
        return []
    spans: list[tuple[int, int]] = []
    cursor = 0
    while True:
        found = source_signature.find(needle_signature, cursor)
        if found < 0:
            break
        end = found + len(needle_signature)
        if _is_signature_boundary(source_signature, found, end):
            last = end - 1
            if last < len(positions):
                mapped = _expand_mapped_span(
                    source,
                    positions[found],
                    positions[last] + 1,
                )
                spans.append(_expand_markdown_definition_prefix(source, *mapped))
        cursor = found + 1
    return spans


def signature_matches(source: str, needle: str) -> list[tuple[int, int]]:
    """Return normalized-signature intervals for an exact block match."""
    source_signature, _positions = signature_with_map(source)
    needle_signature, _ = signature_with_map(needle)
    if not source_signature or not needle_signature:
        return []
    matches: list[tuple[int, int]] = []
    cursor = 0
    while True:
        found = source_signature.find(needle_signature, cursor)
        if found < 0:
            break
        end = found + len(needle_signature)
        if _is_signature_boundary(source_signature, found, end):
            matches.append((found, end))
        cursor = found + 1
    return matches


def _map_signature_boundary(
    opcodes: list[tuple[str, int, int, int, int]],
    index: int,
    *,
    end: bool,
    target_length: int,
) -> int:
    if index <= 0:
        index = 0
    if not opcodes or index >= opcodes[-1][2]:
        return target_length

    if end:
        mapped = None
        for tag, i1, i2, j1, j2 in opcodes:
            if i1 == i2 == index:
                mapped = j2
                continue
            if i1 < index <= i2:
                mapped = j2 if tag != "equal" else j1 + (index - i1)
                break
        return target_length if mapped is None else max(0, min(target_length, mapped))

    for tag, i1, i2, j1, j2 in opcodes:
        if i1 == i2 == index:
            return max(0, min(target_length, j1))
        if i1 <= index < i2:
            mapped = j1 if tag != "equal" else j1 + (index - i1)
            return max(0, min(target_length, mapped))
    return target_length


def _corrected_block_text(
    output_dir: Path,
    page: int,
    block_text: str,
) -> str | None:
    """Map raw sidecar text to a validated OCR-corrected page variant."""
    raw_path = Path(output_dir) / "pages" / f"page_{page:03d}.md"
    corrected_path = (
        Path(output_dir) / "ocr_corrected_pages" / "validated" / raw_path.name
    )
    report_path = Path(output_dir) / "ocr-correct_validation.json"
    try:
        report = json.loads(report_path.read_text(encoding="utf-8"))
        raw_page = raw_path.read_text(encoding="utf-8")
        corrected_page = corrected_path.read_text(encoding="utf-8")
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    if not isinstance(report, Mapping) or report.get("all_passed") is not True:
        return None
    source_hashes = report.get("source_sha256")
    target_hashes = report.get("target_sha256")
    if not isinstance(source_hashes, Mapping) or not isinstance(target_hashes, Mapping):
        return None
    if (
        source_hashes.get(raw_path.name) != sha256_file(raw_path)
        or target_hashes.get(corrected_path.name) != sha256_file(corrected_path)
    ):
        return None

    block_signature, _ = signature_with_map(block_text)
    matches = signature_matches(raw_page, block_text)
    if not block_signature or len(matches) != 1:
        return None
    corrected_signature, _ = signature_with_map(corrected_page)
    if not corrected_signature:
        return None
    opcodes = SequenceMatcher(
        None,
        signature_with_map(raw_page)[0],
        corrected_signature,
        autojunk=False,
    ).get_opcodes()
    start, stop = matches[0]
    mapped_start = _map_signature_boundary(
        opcodes, start, end=False, target_length=len(corrected_signature)
    )
    mapped_stop = _map_signature_boundary(
        opcodes, stop, end=True, target_length=len(corrected_signature)
    )
    if not 0 <= mapped_start < mapped_stop <= len(corrected_signature):
        return None
    return corrected_signature[mapped_start:mapped_stop].strip()


def block_text_variants(
    output_dir: Path,
    page: int,
    block: Mapping[str, Any],
) -> list[str]:
    """Return raw text plus validated correction and source-format variants."""
    raw_text = plain_block_text(block)
    variants = [raw_text] if raw_text else []
    raw_html = block.get("html")
    if raw_html:
        stripped = html.unescape(str(raw_html))
        stripped = re.sub(r"<[^>]+>", "", stripped)
        compact = re.sub(r"\s+", " ", stripped).strip()
        if compact and compact not in variants:
            variants.append(compact)
        math_spaced = re.sub(r"(<math>[^<]+</math>)", r" \1 ", str(raw_html))
        if math_spaced != raw_html:
            stripped_math = re.sub(r"<[^>]+>", "", html.unescape(math_spaced))
            compact_math = re.sub(r"\s+", " ", stripped_math).strip()
            if compact_math and compact_math not in variants:
                variants.append(compact_math)
    corrected = _corrected_block_text(output_dir, page, raw_text)
    if not corrected and raw_html:
        corrected = _corrected_block_text(output_dir, page, raw_html)
    if corrected and corrected not in variants:
        variants.append(corrected)
    raw_md_path = Path(output_dir) / "pages" / f"page_{page:03d}.md"
    if raw_md_path.is_file():
        try:
            for line in reversed(raw_md_path.read_text(encoding="utf-8").splitlines()):
                line = line.strip()
                if line and re.match(r"^(?:\[\^\w+\]|\d+\b|<sup\b)", line):
                    if line not in variants:
                        variants.append(line)
        except (OSError, UnicodeDecodeError):
            pass
    return variants


def _load_json(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid {label}: {exc}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be a JSON object")
    return value


def load_sidecar_block(
    output_dir: Path,
    page: int,
    block_index: int,
    cache: dict[int, dict[str, Any]],
) -> Mapping[str, Any]:
    if page not in cache:
        cache[page] = _load_json(
            output_dir / "pages" / f"page_{page:03d}.ocr.json",
            f"OCR sidecar for page {page}",
        )
    blocks = cache[page].get("blocks")
    if not isinstance(blocks, list) or not 0 <= block_index < len(blocks):
        raise ValueError(f"OCR sidecar has no block {page}:{block_index}")
    block = blocks[block_index]
    if not isinstance(block, Mapping):
        raise ValueError(f"OCR block {page}:{block_index} is not an object")
    return block


def marker_spans(source: str, key: str) -> list[tuple[int, int, str]]:
    escaped = re.escape(str(key))
    spans: list[tuple[int, int, str]] = []
    html_sup_ranges = [
        (match.start(), match.end())
        for match in _HTML_SUP_BLOCK_RE.finditer(source)
    ]

    def append_span(start: int, end: int, kind: str) -> None:
        # One visible marker must produce one candidate.  In particular, a
        # circled digit inside ``<sup class=...>`` is also found by the raw
        # circled-digit scan; retaining both spans makes cross-part matching
        # look ambiguous even though the source contains one marker.
        if any(existing_start == start and existing_end == end for existing_start, existing_end, _ in spans):
            return
        spans.append((start, end, kind))

    def is_inside_html_sup(index: int) -> bool:
        return any(start <= index < end for start, end in html_sup_ranges)

    for match in re.finditer(
        _HTML_SUP_RE_TEMPLATE.format(key=escaped),
        source,
        flags=re.IGNORECASE,
    ):
        append_span(match.start(), match.end(), "sup")
    for match in _MARKDOWN_REF_RE.finditer(source):
        if match.group("key") == str(key):
            append_span(match.start(), match.end(), "markdown")
    superscript = str(key).translate(_SUPERSCRIPT_DIGITS)
    if superscript != str(key):
        for match in re.finditer(re.escape(superscript), source):
            if not is_inside_html_sup(match.start()):
                append_span(match.start(), match.end(), "unicode_sup")
    if str(key) in _CIRCLED_DIGITS_MAP:
        circled = _CIRCLED_DIGITS_MAP[str(key)]
        for match in re.finditer(
            _HTML_SUP_RE_TEMPLATE.format(key=re.escape(circled)),
            source,
            flags=re.IGNORECASE,
        ):
            append_span(match.start(), match.end(), "sup")
        for match in re.finditer(re.escape(circled), source):
            idx = match.start()
            if idx == 0 or source[idx - 1] == "\n":
                continue
            if is_inside_html_sup(idx):
                continue
            append_span(match.start(), match.end(), "unicode_circled")
    return sorted(spans)


def marker_context_matches(
    source: str, span: tuple[int, int, str], context: str
) -> bool:
    """Match a reviewer-provided local phrase around a body marker."""
    normalized_context = re.sub(r"\s+", " ", str(context or "")).strip().casefold()
    if not normalized_context:
        return False
    start, end, _kind = span
    window = source[max(0, start - 120) : min(len(source), end + 120)]
    normalized_window = re.sub(r"\s+", " ", window).strip().casefold()
    return normalized_context in normalized_window


def secondary_primary_blocks(decision: Mapping[str, Any]) -> list[int]:
    """Normalize the explicit primary blocks named by a secondary decision."""
    raw_blocks = decision.get("primary_blocks")
    if raw_blocks is None:
        raw_block = decision.get("primary_block")
        raw_blocks = [raw_block] if raw_block is not None else []
    if not isinstance(raw_blocks, list):
        return []
    blocks: list[int] = []
    for raw_block in raw_blocks:
        try:
            block = int(raw_block)
        except (TypeError, ValueError):
            return []
        if block < 0 or block in blocks:
            return []
        blocks.append(block)
    return blocks


def strip_note_key(text: str, key: str | None) -> str:
    value = re.sub(r"\s+", " ", str(text or "")).strip()
    if not key:
        return value
    escaped = re.escape(str(key))
    value = re.sub(
        rf"^\s*(?:\[\^{escaped}\]|<sup\b[^>]*>\s*{escaped}\s*</sup>|{escaped})\s*"
        rf"(?:[.)、，:：-]\s*)?",
        "",
        value,
        count=1,
        flags=re.IGNORECASE,
    )
    return value.strip()


def apply_edits(source: str, edits: list[tuple[int, int, str]]) -> str:
    ordered = sorted(edits, key=lambda item: (item[0], item[1]))
    previous_end = -1
    for start, end, _replacement in ordered:
        if start < previous_end:
            raise ValueError(
                f"overlapping footnote edits: start {start} < previous_end {previous_end}; "
                f"overlapping text: {repr(source[start:min(len(source), previous_end + 30)])}"
            )
        previous_end = end
    for start, end, replacement in reversed(ordered):
        source = source[:start] + replacement + source[end:]
    return source


__all__ = [
    "apply_edits",
    "block_text_variants",
    "find_exact_signature_spans",
    "load_sidecar_block",
    "marker_context_matches",
    "marker_spans",
    "plain_block_text",
    "secondary_primary_blocks",
    "signature_matches",
    "signature_with_map",
    "strip_note_key",
]
