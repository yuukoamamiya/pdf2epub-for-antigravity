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
from typing import Any, Mapping, Optional

from ..workflow_contracts import sha256_file


_MARKDOWN_REF_RE = re.compile(r"\[\^(?P<key>[A-Za-z0-9_-]+)\]")
_HTML_SUP_RE_TEMPLATE = r"<sup\b[^>]*>\s*{key}\s*</sup>"
_HTML_SUP_BLOCK_RE = re.compile(
    r"<sup\b[^>]*>.*?</sup>", re.IGNORECASE | re.DOTALL
)
_AUTO_LINK_RE = re.compile(
    r"<+(?P<url>(?:https?://|mailto:)[^<>\s]+)>+", re.IGNORECASE
)
_SUPERSCRIPT_DIGITS = str.maketrans("⁰¹²³⁴⁵⁶⁷⁸⁹", "0123456789")
_DIGITS_TO_SUPERSCRIPT = str.maketrans("0123456789", "⁰¹²³⁴⁵⁶⁷⁸⁹")
_CIRCLED_CHARS = (
    "①②③④⑤⑥⑦⑧⑨⑩⑪⑫⑬⑭⑮⑯⑰⑱⑲⑳"
    "㉑㉒㉓㉔㉕㉖㉗㉘㉙㉚㉛㉜㉝㉞㉟"
    "㊱㊲㊳㊴㊵㊶㊷㊸㊹㊺㊻㊼㊽㊾㊿"
)
_CIRCLED_DIGITS_MAP = {str(i): char for i, char in enumerate(_CIRCLED_CHARS, 1)}
_ORDERED_LIST_VARIANT = "\x00ordered-list\x00"
_UNORDERED_LIST_VARIANT = "\x00unordered-list\x00"


def plain_block_text(block: Mapping[str, Any]) -> str:
    """Return a compact text representation of one sidecar block."""
    value = block.get("text")
    if value is None:
        value = block.get("html", "")
    value = html.unescape(str(value or ""))
    value = _AUTO_LINK_RE.sub(r"\g<url>", value)
    value = re.sub(r"<[^>]+>", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def _append_decoded_signature(
    decoded: str, position: int, chars: list[str], positions: list[int]
) -> None:
    for char in decoded:
        if char.isspace():
            if chars and chars[-1] != " ":
                chars.append(" ")
                positions.append(position)
        else:
            chars.append(char)
            positions.append(position)


def _append_url_signature(
    value: str, start: int, end: int, chars: list[str], positions: list[int]
) -> None:
    """Append URL content with Markdown escapes/entities mapped to source."""
    index = start
    while index < end:
        if value[index] == "\\" and index + 1 < end and value[index + 1] in string.punctuation:
            index += 1
            chars.append(value[index])
            positions.append(index)
            index += 1
            continue
        entity = re.match(r"&(?:[A-Za-z][A-Za-z0-9]+|#\d+|#x[0-9a-f]+);", value[index:end], re.I)
        if entity:
            decoded = html.unescape(entity.group(0))
            if decoded != entity.group(0):
                _append_decoded_signature(decoded, index, chars, positions)
                index += len(entity.group(0))
                continue
        chars.append(value[index])
        positions.append(index)
        index += 1


def signature_with_map(value: str) -> tuple[str, list[int]]:
    """Normalize OCR/Markdown text while retaining source offsets."""
    value = str(value or "")
    chars: list[str] = []
    positions: list[int] = []
    index = 0
    while index < len(value):
        char = value[index]
        entity = re.match(r"&(?:[A-Za-z][A-Za-z0-9]+|#\d+|#x[0-9a-f]+);", value[index:], re.I)
        if entity:
            decoded = html.unescape(entity.group(0))
            if decoded != entity.group(0):
                _append_decoded_signature(decoded, index, chars, positions)
                index += len(entity.group(0))
                continue
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
                _append_url_signature(
                    value,
                    auto_link_match.start("url"),
                    auto_link_match.end("url"),
                    chars,
                    positions,
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
        if re.match(r"(?:https?://|mailto:)", value[index:], re.IGNORECASE):
            bare_url_match = re.match(
                r"^(?:https?://|mailto:)[^\s<>]+", value[index:], re.IGNORECASE
            )
            if bare_url_match:
                url = bare_url_match.group(0)
                while url and url[-1] in ".,;:!?":
                    url = url[:-1]
                while url.endswith(")") and url.count(")") > url.count("("):
                    url = url[:-1]
                if not url:
                    index += 1
                    continue
                _append_url_signature(value, index, index + len(url), chars, positions)
                index += len(url)
                continue
        if char == ">":
            index += 1
            continue
        if char in "*_~`$":
            index += 1
            continue
        if char.isspace():
            if chars and chars[-1] not in " ([{<":
                chars.append(" ")
                positions.append(index)
        else:
            if char in ".,;:!?)]}" and chars and chars[-1] == " ":
                chars.pop()
                positions.pop()
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
    url_start = bool(re.match(r"(?:https?://|mailto:)", text[start:end], re.I))
    if url_start:
        url_chars = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~:/?#[]@!$&'()*+,;=%"
        wrapped_in_parens = (
            start > 0
            and text[start - 1] == "("
            and end < len(text)
            and text[end] == ")"
            and text[start:end].count("(") == text[start:end].count(")")
        )
        left_ok = start == 0 or text[start - 1] not in url_chars or wrapped_in_parens
        if end >= len(text) or text[end] not in url_chars:
            right_ok = True
        else:
            following = text[end]
            next_char = text[end + 1] if end + 1 < len(text) else ""
            sentence_punctuation = ".,;:!?"
            unmatched_closing_paren = following == ")" and (
                text[start:end].count("(") <= text[start:end].count(")")
                or wrapped_in_parens
            )
            trailing_punctuation = (
                following in sentence_punctuation
                and (not next_char or next_char.isspace() or next_char in "\"'”’)]}")
            )
            right_ok = unmatched_closing_paren or trailing_punctuation
        return left_ok and right_ok
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
    if (
        start > 0
        and end < len(source)
        and source[start - 1] == "<"
        and source[end] == ">"
        and _AUTO_LINK_RE.fullmatch(source[start - 1 : end + 1])
    ):
        start -= 1
        end += 1
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
    if end > 0:
        entity_start = source.rfind("&", 0, end)
        if entity_start >= 0:
            entity = re.match(r"&(?:[A-Za-z][A-Za-z0-9]+|#\d+|#x[0-9a-f]+);", source[entity_start:], re.I)
            if entity and entity_start < end <= entity_start + len(entity.group(0)):
                end = entity_start + len(entity.group(0))
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
    structured_list = False
    if raw_html:
        html_text = str(raw_html)
        list_open = re.search(r"<(ol|ul)\b([^>]*)>", html_text, re.I)
        list_items = re.findall(r"<li\b[^>]*>(.*?)</li\s*>", html_text, re.I | re.S)
        if list_open and list_items:
            structured_list = True
            variants = []
            if re.search(r"<(?:ol|ul)\b", html_text[list_open.end():], re.I):
                list_items = []  # Nested lists have ambiguous source intervals.
            else:
                tag = list_open.group(1).casefold()
                start_match = re.search(r"\bstart\s*=\s*['\"]?(\d+)", list_open.group(2), re.I)
                declared_start = int(start_match.group(1)) if start_match else None
                rendered: list[str] = []
                explicit_numbers: list[int | None] = []
                item_texts: list[str] = []
                for item in list_items:
                    item_text = html.unescape(item)
                    item_text = re.sub(r"</?(?:p|div|span)\b[^>]*>", " ", item_text, flags=re.I)
                    item_text = re.sub(r"<[^>]+>", " ", item_text)
                    item_text = re.sub(r"\s+", " ", item_text).strip()
                    if not item_text:
                        list_items = []
                        break
                    if tag == "ol":
                        explicit = re.match(r"^(\d+)[.)]\s+", item_text)
                        explicit_numbers.append(int(explicit.group(1)) if explicit else None)
                        item_texts.append(item_text)
                    else:
                        item_text = re.sub(r"^[-*+]\s+", "", item_text)
                        rendered.append(f"- {item_text}")
                if tag == "ol" and list_items:
                    has_explicit = [number is not None for number in explicit_numbers]
                    if any(has_explicit) and not all(has_explicit):
                        list_items = []
                    else:
                        number = declared_start or (
                            explicit_numbers[0]
                            if explicit_numbers and explicit_numbers[0] is not None
                            else 1
                        )
                        if declared_start is not None and all(has_explicit) and explicit_numbers[0] != declared_start:
                            list_items = []
                        elif explicit_numbers and all(has_explicit) and any(
                            actual != number + index
                            for index, actual in enumerate(explicit_numbers)
                        ):
                            list_items = []
                        else:
                            for item_text in item_texts:
                                item_text = re.sub(r"^\d+[.)]\s+", "", item_text)
                                rendered.append(f"{number}. {item_text}")
                                number += 1
                if list_items and rendered:
                    prefix = _ORDERED_LIST_VARIANT if tag == "ol" else _UNORDERED_LIST_VARIANT
                    variants.append(prefix + "\n".join(rendered))
                    if tag == "ol":
                        parenthesized = [
                            re.sub(r"^(\d+)\. ", r"\1) ", item)
                            for item in rendered
                        ]
                        variants.append(prefix + "\n".join(parenthesized))
        else:
            stripped = html.unescape(html_text)
            stripped = re.sub(r"<[^>]+>", "", stripped)
            compact = re.sub(r"\s+", " ", stripped).strip()
            if compact and compact not in variants:
                variants.append(compact)
            math_spaced = re.sub(r"(<math>[^<]+</math>)", r" \1 ", html_text)
            if math_spaced != html_text:
                stripped_math = re.sub(r"<[^>]+>", "", html.unescape(math_spaced))
                compact_math = re.sub(r"\s+", " ", stripped_math).strip()
                if compact_math and compact_math not in variants:
                    variants.append(compact_math)
    if not structured_list:
        corrected = _corrected_block_text(output_dir, page, raw_text)
        if not corrected and raw_html:
            corrected = _corrected_block_text(output_dir, page, raw_html)
        if corrected and corrected not in variants:
            variants.append(corrected)
    raw_md_path = Path(output_dir) / "pages" / f"page_{page:03d}.md"
    if raw_md_path.is_file() and not structured_list:
        try:
            def _extract_delimited_key(text: str) -> Optional[str]:
                text = text.strip()
                m_c = re.match(r"^(?:\[\^|<sup[^>]*>)?([①-⑳])", text)
                if m_c:
                    return str(_CIRCLED_CHARS.index(m_c.group(1)) + 1)
                m_d = re.match(r"^(?:\[\^|<sup[^>]*>)?(\d+)(?:\]\s*:?|[.)、，:：\s]|\^|<|$)", text)
                if m_d:
                    return m_d.group(1)
                return None

            page_text = raw_md_path.read_text(encoding="utf-8")
            raw_key = _extract_delimited_key(raw_text)
            for line in page_text.splitlines():
                line = line.strip()
                if not line:
                    continue
                line_key = _extract_delimited_key(line)
                if raw_key and line_key and raw_key != line_key:
                    continue
                if not line_key:
                    continue
                payload = re.sub(r"^(?:\[\^\w+\]|\d+|<sup\b[^>]*>\d+</sup>)[.)、，:：\s]*", "", line).strip()
                if payload and signature_with_map(payload)[0] == signature_with_map(raw_text)[0]:
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

    def is_inside_definition_line(index: int) -> bool:
        line_start = source.rfind("\n", 0, index) + 1
        return bool(re.match(r"^\[\^[^\]\n]+\]\s*:", source[line_start:]))

    for match in re.finditer(
        _HTML_SUP_RE_TEMPLATE.format(key=escaped),
        source,
        flags=re.IGNORECASE,
    ):
        if not is_inside_definition_line(match.start()):
            append_span(match.start(), match.end(), "sup")
    for match in _MARKDOWN_REF_RE.finditer(source):
        if match.group("key") == str(key) and not is_inside_definition_line(match.start()):
            append_span(match.start(), match.end(), "markdown")
    superscript = str(key).translate(_DIGITS_TO_SUPERSCRIPT)
    if superscript != str(key):
        for match in re.finditer(re.escape(superscript), source):
            if not is_inside_html_sup(match.start()) and not is_inside_definition_line(match.start()):
                append_span(match.start(), match.end(), "unicode_sup")
    if str(key) in _CIRCLED_DIGITS_MAP:
        circled = _CIRCLED_DIGITS_MAP[str(key)]
        for match in re.finditer(
            _HTML_SUP_RE_TEMPLATE.format(key=re.escape(circled)),
            source,
            flags=re.IGNORECASE,
        ):
            if not is_inside_definition_line(match.start()):
                append_span(match.start(), match.end(), "sup")
        for match in re.finditer(re.escape(circled), source):
            idx = match.start()
            if idx == 0 or source[idx - 1] == "\n":
                continue
            if is_inside_html_sup(idx) or is_inside_definition_line(idx):
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
    patterns = [
        rf"\[\^{escaped}\]",
        rf"<sup\b[^>]*>\s*{escaped}\s*</sup>",
        escaped,
    ]
    if str(key) in _CIRCLED_DIGITS_MAP:
        circled = re.escape(_CIRCLED_DIGITS_MAP[str(key)])
        patterns.extend([
            rf"<sup\b[^>]*>\s*{circled}\s*</sup>",
            circled,
        ])
    superscript = str(key).translate(_DIGITS_TO_SUPERSCRIPT)
    if superscript != str(key):
        patterns.append(re.escape(superscript))
    joined = "|".join(patterns)
    value = re.sub(
        rf"^\s*(?:{joined})\s*(?:[.)、，:：-]\s*)?",
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
