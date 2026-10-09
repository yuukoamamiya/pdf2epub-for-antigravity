"""Materialize reviewed page footnotes as logical chapter-end notes.

The preparation step deliberately stops at a small layout decision contract.
This module is the deterministic half that consumes that contract.  It never
decides whether a numbered block is a footnote: only decisions whose role is a
``footnote_*`` role are moved.  Citations, bibliography entries, quotations,
and ordinary body blocks stay in their original unit.
"""

from __future__ import annotations

import html
import json
import re
from difflib import SequenceMatcher
from collections import defaultdict
from pathlib import Path
from typing import Any, Mapping, Optional

from ..workflow_contracts import atomic_write_text, sha256_file
from .pdf_evidence import pdf_evidence_mode, require_current_consensus


FOOTNOTE_NORMALIZATION_SCHEMA_VERSION = 1
_MOVED_ROLES = frozenset(
    {"footnote_start", "footnote_continuation", "footnote_definition"}
)
_NON_MOVED_ROLES = frozenset({"body", "citation", "bibliography"})
_SAFE_FILENAME_RE = re.compile(r"^[^/\\]+\.md$")
_MARKDOWN_REF_RE = re.compile(r"\[\^(?P<key>[A-Za-z0-9_-]+)\]")
_HTML_SUP_RE_TEMPLATE = r"<sup\b[^>]*>\s*{key}\s*</sup>"
_SUPERSCRIPT_DIGITS = str.maketrans("⁰¹²³⁴⁵⁶⁷⁸⁹", "0123456789")


def _markdown_inventory(directory: Path) -> dict[str, str]:
    return {
        path.name: sha256_file(path)
        for path in sorted(Path(directory).glob("*.md"))
        if path.is_file()
    }


def _safe_name(value: Any) -> str:
    name = str(value or "")
    if not _SAFE_FILENAME_RE.fullmatch(name):
        raise ValueError(f"invalid Markdown unit filename: {name!r}")
    return name


def _plain_block_text(block: Mapping[str, Any]) -> str:
    value = block.get("text")
    if value is None:
        value = block.get("html", "")
    value = html.unescape(str(value or ""))
    value = re.sub(r"<[^>]+>", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def _signature_with_map(value: str) -> tuple[str, list[int]]:
    """Normalize OCR/Markdown text while retaining source offsets.

    OCR Markdown may contain ``<sup>`` tags or light Markdown emphasis around
    the same block text stored in the sidecar.  Ignoring those wrappers lets
    us remove the exact source span without fuzzy deletion of nearby prose.
    """

    value = html.unescape(str(value or ""))
    chars: list[str] = []
    positions: list[int] = []
    index = 0
    while index < len(value):
        char = value[index]
        if char == "<":
            close = value.find(">", index + 1)
            if close >= 0:
                index = close + 1
                continue
        if char in "*_~`":
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


def _is_signature_boundary(text: str, start: int, end: int) -> bool:
    left_ok = (start == 0) or (not text[start - 1].isalnum())
    right_ok = (end >= len(text)) or (not text[end].isalnum())
    return left_ok and right_ok


def _expand_mapped_span(source: str, start: int, end: int) -> tuple[int, int]:
    """Include formatting wrappers removed from the normalized signature.

    The signature matcher deliberately ignores HTML/Markdown wrappers, but
    the edit must not leave a dangling ``</sup>`` or emphasis marker behind.
    Only wrappers adjacent to the matched text are included, and a trailing
    HTML close tag is accepted only when its tag name has an opening tag in
    the matched span.
    """
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


def _find_exact_signature_spans(source: str, needle: str) -> list[tuple[int, int]]:
    source_signature, positions = _signature_with_map(source)
    needle_signature, _ = _signature_with_map(needle)
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
                spans.append(
                    _expand_mapped_span(
                        source,
                        positions[found],
                        positions[last] + 1,
                    )
                )
        cursor = found + 1
    return spans


def _signature_matches(source: str, needle: str) -> list[tuple[int, int]]:
    """Return normalized-signature intervals for an exact block match."""
    source_signature, _positions = _signature_with_map(source)
    needle_signature, _ = _signature_with_map(needle)
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
    """Map a source signature boundary through a character diff.

    OCR correction is normally a character-level edit.  This mapping keeps
    insertions inside a corrected block, while replacements/deletions map to
    the complete changed span.  It is only used to create an exact text
    variant; the final chapter-source lookup remains unique-or-fail.
    """
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
    """Map raw sidecar text to the validated OCR-corrected page text.

    Layout evidence stays in the immutable raw sidecar.  This helper only
    supplies a text variant for footnote-apply after OCR correction changed a
    glyph, so the existing exact/unique source-span safety checks still apply.
    """
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

    raw_signature, _ = _signature_with_map(raw_page)
    block_signature, _ = _signature_with_map(block_text)
    matches = _signature_matches(raw_page, block_text)
    if not block_signature or len(matches) != 1:
        return None
    corrected_signature, _ = _signature_with_map(corrected_page)
    if not corrected_signature:
        return None
    opcodes = SequenceMatcher(
        None,
        raw_signature,
        corrected_signature,
        autojunk=False,
    ).get_opcodes()
    start, stop = matches[0]
    mapped_start = _map_signature_boundary(
        opcodes,
        start,
        end=False,
        target_length=len(corrected_signature),
    )
    mapped_stop = _map_signature_boundary(
        opcodes,
        stop,
        end=True,
        target_length=len(corrected_signature),
    )
    if not 0 <= mapped_start < mapped_stop <= len(corrected_signature):
        return None
    return corrected_signature[mapped_start:mapped_stop].strip()


def _block_text_variants(
    output_dir: Path,
    page: int,
    block: Mapping[str, Any],
) -> list[str]:
    """Return raw text plus an optional validated-correction text variant."""
    raw_text = _plain_block_text(block)
    variants = [raw_text] if raw_text else []
    raw_html = block.get("html")
    if raw_html:
        stripped = html.unescape(str(raw_html))
        stripped = re.sub(r"<[^>]+>", "", stripped)
        compact = re.sub(r"\s+", " ", stripped).strip()
        if compact and compact not in variants:
            variants.append(compact)
    corrected = _corrected_block_text(output_dir, page, raw_text)
    if corrected and corrected not in variants:
        variants.append(corrected)
    return variants


def _load_json(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid {label}: {exc}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be a JSON object")
    return value


def _load_sidecar_block(
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


def _unit_records(output_dir: Path) -> list[dict[str, Any]]:
    progress = _load_json(
        output_dir / "ocr_markdown" / "tree_progress.json",
        "ocr_markdown/tree_progress.json",
    )
    records: list[dict[str, Any]] = []
    for unit_order, unit in enumerate(progress.get("units", []) or []):
        if not isinstance(unit, Mapping):
            continue
        page_range = unit.get("page_range")
        if not isinstance(page_range, list) or len(page_range) != 2:
            raise ValueError("tree_progress unit has an invalid page_range")
        try:
            start_page, end_page = int(page_range[0]), int(page_range[1])
        except (TypeError, ValueError) as exc:
            raise ValueError("tree_progress unit has a non-integer page_range") from exc
        index_path = unit.get("index_path")
        unit_id = str(unit.get("unit_id") or "").strip()
        if unit_id:
            # The emitted refinement unit is the actual footnote scope.  Do
            # not collapse chapter_1.1 and chapter_1.2 into chapter_1: many
            # books restart footnote numbering at those logical boundaries.
            chapter_id = f"toc-unit:{unit_id}"
        elif isinstance(index_path, list) and index_path:
            path_key = ".".join(str(value) for value in index_path)
            chapter_id = f"toc-path:{path_key}"
        else:
            chapter_id = f"unit:{unit.get('file')}"
        names = unit.get("part_files") or [unit.get("file")]
        for part_index, raw_name in enumerate(names, 1):
            name = _safe_name(raw_name)
            records.append(
                {
                    "name": name,
                    "start_page": start_page,
                    "end_page": end_page,
                    "chapter_id": chapter_id,
                    "unit_order": unit_order,
                    "part_index": part_index,
                    "type": str(unit.get("type") or "").strip().lower(),
                    "index_path": index_path,
                }
            )
    if not records:
        raise ValueError("tree_progress.json contains no Markdown units")
    return records


def _candidate_decisions(
    report: Mapping[str, Any],
    validation: Mapping[str, Any],
) -> list[dict[str, Any]]:
    """Combine reviewed decisions with deterministic high-confidence ones."""

    decisions_by_address: dict[tuple[int, str, int], dict[str, Any]] = {}
    for item in validation.get("decisions", []) or []:
        if not isinstance(item, Mapping):
            continue
        try:
            address = (
                int(item["page"]),
                str(item.get("source") or "primary").strip().lower(),
                int(item["block"]),
            )
        except (KeyError, TypeError, ValueError):
            continue
        decisions_by_address[address] = dict(item)

    for page_report in report.get("pages", []) or []:
        if not isinstance(page_report, Mapping):
            continue
        for candidate in page_report.get("candidates", []) or []:
            if not isinstance(candidate, Mapping) or candidate.get("confidence") != "high":
                continue
            try:
                address = (
                    int(candidate["page"]),
                    str(candidate.get("source") or "primary").strip().lower(),
                    int(candidate["block"]),
                )
            except (KeyError, TypeError, ValueError):
                continue
            decisions_by_address.setdefault(
                address,
                {
                    "page": address[0],
                    "source": address[1],
                    "block": address[2],
                    "role": "footnote_start",
                    "key": str(candidate.get("key") or ""),
                    "confidence": "local_high",
                },
            )

    return sorted(
        decisions_by_address.values(),
        key=lambda item: (int(item.get("page", 0)), int(item.get("block", 0))),
    )


def _records_for_page(records: list[dict[str, Any]], page: int) -> list[dict[str, Any]]:
    return [
        record
        for record in records
        if record["start_page"] <= page <= record["end_page"]
    ]


def _locate_block(
    source_texts: Mapping[str, str],
    records: list[dict[str, Any]],
    page: int,
    block_texts: list[str],
) -> tuple[dict[str, Any], tuple[int, int]]:
    expected = _records_for_page(records, page)
    if not expected:
        raise ValueError(f"no refinement unit covers OCR page {page}")
    matches: list[tuple[dict[str, Any], tuple[int, int]]] = []
    seen: set[tuple[str, int, int]] = set()
    for block_text in block_texts:
        for record in expected:
            for span in _find_exact_signature_spans(source_texts[record["name"]], block_text):
                identity = (record["name"], span[0], span[1])
                if identity not in seen:
                    seen.add(identity)
                    matches.append((record, span))
        if matches:
            break
    if len(matches) != 1:
        if not matches:
            raise ValueError(f"could not locate OCR block text on page {page}")
        raise ValueError(f"OCR block text on page {page} matched multiple source spans")
    return matches[0]


def _marker_spans(source: str, key: str) -> list[tuple[int, int, str]]:
    escaped = re.escape(str(key))
    spans: list[tuple[int, int, str]] = []
    for match in re.finditer(
        _HTML_SUP_RE_TEMPLATE.format(key=escaped),
        source,
        flags=re.IGNORECASE,
    ):
        spans.append((match.start(), match.end(), "sup"))
    for match in _MARKDOWN_REF_RE.finditer(source):
        if match.group("key") == str(key):
            spans.append((match.start(), match.end(), "markdown"))
    superscript = str(key).translate(_SUPERSCRIPT_DIGITS)
    if superscript != str(key):
        for match in re.finditer(re.escape(superscript), source):
            spans.append((match.start(), match.end(), "unicode_sup"))
    return sorted(spans)


def _marker_context_matches(source: str, span: tuple[int, int, str], context: str) -> bool:
    """Match a reviewer-provided local phrase around a body marker."""
    normalized_context = re.sub(r"\s+", " ", str(context or "")).strip().casefold()
    if not normalized_context:
        return False
    start, end, _kind = span
    window = source[max(0, start - 120) : min(len(source), end + 120)]
    normalized_window = re.sub(r"\s+", " ", window).strip().casefold()
    return normalized_context in normalized_window


def _secondary_primary_blocks(decision: Mapping[str, Any]) -> list[int]:
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


def _strip_note_key(text: str, key: str | None) -> str:
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


def _apply_edits(source: str, edits: list[tuple[int, int, str]]) -> str:
    ordered = sorted(edits, key=lambda item: (item[0], item[1]))
    previous_end = -1
    for start, end, _replacement in ordered:
        if start < previous_end:
            raise ValueError("overlapping footnote edits")
        previous_end = end
    for start, end, replacement in reversed(ordered):
        source = source[:start] + replacement + source[end:]
    return source


def _write_failure(output_dir: Path, errors: list[str]) -> dict[str, Any]:
    result = {
        "schema_version": FOOTNOTE_NORMALIZATION_SCHEMA_VERSION,
        "status": "human_review_required",
        "valid": False,
        "errors": errors,
    }
    atomic_write_text(
        Path(output_dir) / "footnote_normalization.json",
        json.dumps(result, ensure_ascii=False, indent=2),
    )
    return result


def apply_footnote_normalization(
    output_dir: Path,
    *,
    config: Optional[Mapping[str, Any]] = None,
) -> dict[str, Any]:
    """Create ``footnote_normalized/`` from validated decisions.

    The operation is preflighted before writing the derived stage.  A missing
    body marker, ambiguous source match, cross-chapter continuation, or a
    footnote decision inside bibliography/index content blocks the stage and
    leaves the original OCR Markdown untouched.
    """

    output_dir = Path(output_dir)
    try:
        report = _load_json(output_dir / "footnote_candidates.json", "footnote_candidates.json")
        validation = _load_json(
            output_dir / "footnote_decision_validation.json",
            "footnote_decision_validation.json",
        )
    except ValueError as exc:
        return _write_failure(output_dir, [str(exc)])

    report_mode = str(report.get("ocr_evidence_mode") or "single_ocr")
    validation_mode = str(validation.get("ocr_evidence_mode") or report_mode)
    if config is not None:
        configured_mode = pdf_evidence_mode(output_dir, config)
        if report_mode != configured_mode or validation_mode != configured_mode:
            return _write_failure(
                output_dir,
                [
                    "footnote checkpoints were prepared with a different OCR evidence mode; "
                    "rerun footnote-prepare and footnote-validate",
                ],
            )
        if configured_mode == "two_ocr":
            try:
                require_current_consensus(output_dir, config, stage="footnote")
            except ValueError:
                return _write_failure(
                    output_dir,
                    [
                        "two-OCR footnote checkpoint is stale; rerun ocr-pages and footnote-prepare",
                    ],
                )

    if validation.get("valid") is not True or validation.get("status") not in {
        "validated",
        "no_subagent_review_required",
    }:
        return _write_failure(
            output_dir,
            [
                "footnote decisions are not validated; run footnote-validate "
                "after the Subagent review",
            ],
        )

    source_dir = output_dir / "ocr_markdown"
    target_dir = output_dir / "footnote_normalized"
    if not source_dir.is_dir():
        return _write_failure(output_dir, [f"source directory is missing: {source_dir}"])

    try:
        records = _unit_records(output_dir)
        source_texts = {
            record["name"]: (source_dir / record["name"]).read_text(encoding="utf-8")
            for record in records
        }
        # Duplicate part entries refer to the same file only in malformed
        # historical checkpoints.  Refuse them instead of choosing silently.
        if len(source_texts) != len(records):
            raise ValueError("tree_progress contains duplicate Markdown unit filenames")
    except (OSError, UnicodeError, ValueError) as exc:
        return _write_failure(output_dir, [str(exc)])

    sidecar_cache: dict[int, dict[str, Any]] = {}
    decisions = _candidate_decisions(report, validation)
    edits_by_file: dict[str, list[tuple[int, int, str]]] = defaultdict(list)
    notes_by_chapter: dict[str, list[dict[str, Any]]] = defaultdict(list)
    active_by_chapter_key: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    resolved: list[dict[str, Any]] = []
    preserved_roles: dict[str, int] = defaultdict(int)
    errors: list[str] = []
    scheduled_secondary_primary_removals: set[tuple[int, int]] = set()

    for decision in decisions:
        role = str(decision.get("role") or "").strip()
        source = str(decision.get("source") or "primary").strip().lower()
        try:
            page = int(decision["page"])
            block_index = int(decision["block"])
        except (KeyError, TypeError, ValueError):
            errors.append("decision has an invalid page/block address")
            continue
        if role in _NON_MOVED_ROLES:
            preserved_roles[role] += 1
            continue
        if role == "review_required":
            errors.append(f"unresolved review decision at {page}:{block_index}")
            continue
        if role not in _MOVED_ROLES:
            errors.append(f"unsupported decision role at {page}:{block_index}: {role}")
            continue
        if source not in {"primary", "secondary"}:
            errors.append(f"unsupported footnote source at {page}:{block_index}: {source}")
            continue

        key = str(decision.get("key") or "").strip()
        if not re.fullmatch(r"[A-Za-z0-9_-]+", key):
            errors.append(f"moved footnote at {page}:{block_index} has no valid key")
            continue
        if source == "primary":
            try:
                block = _load_sidecar_block(output_dir, page, block_index, sidecar_cache)
                block_texts = _block_text_variants(output_dir, page, block)
                if not block_texts:
                    raise ValueError("OCR block is empty")
                record, span = _locate_block(source_texts, records, page, block_texts)
            except ValueError as exc:
                errors.append(f"{page}:{block_index}: {exc}")
                continue
        else:
            source_file = str(decision.get("source_file") or "").strip()
            if not source_file or Path(source_file).name != source_file:
                errors.append(
                    f"{page}:{block_index}: secondary footnote has no safe source_file"
                )
                continue
            matching_records = [
                item
                for item in _records_for_page(records, page)
                if item["name"] == source_file
            ]
            if len(matching_records) != 1:
                errors.append(
                    f"{page}:{block_index}: secondary footnote source_file does not uniquely "
                    "identify a refinement unit"
                )
                continue
            record = matching_records[0]
            source_page_text = source_texts[record["name"]]
            span = (len(source_page_text), len(source_page_text))
            primary_disposition = str(
                decision.get("primary_disposition") or ""
            ).strip().lower()
            if primary_disposition not in {"absent", "remove"}:
                errors.append(
                    f"{page}:{block_index}: secondary footnote must declare "
                    "primary_disposition"
                )
                continue
            if primary_disposition == "remove":
                primary_blocks = _secondary_primary_blocks(decision)
                if not primary_blocks:
                    errors.append(
                        f"{page}:{block_index}: secondary footnote marked remove "
                        "without primary block(s)"
                    )
                    continue
                primary_spans: list[tuple[int, int]] = []
                removal_failed = False
                for primary_block_index in primary_blocks:
                    removal_key = (page, primary_block_index)
                    if removal_key in scheduled_secondary_primary_removals:
                        errors.append(
                            f"{page}:{block_index}: primary block {primary_block_index} "
                            "is scheduled for removal more than once"
                        )
                        removal_failed = True
                        break
                    try:
                        primary_block = _load_sidecar_block(
                            output_dir, page, primary_block_index, sidecar_cache
                        )
                        primary_block_texts = _block_text_variants(
                            output_dir, page, primary_block
                        )
                        if not primary_block_texts:
                            raise ValueError("primary OCR block is empty")
                        primary_record, primary_span = _locate_block(
                            source_texts,
                            records,
                            page,
                            primary_block_texts,
                        )
                    except ValueError as exc:
                        errors.append(
                            f"{page}:{block_index}: could not locate primary duplicate "
                            f"block {primary_block_index}: {exc}"
                        )
                        removal_failed = True
                        break
                    if primary_record["name"] != source_file:
                        errors.append(
                            f"{page}:{block_index}: primary duplicate block "
                            f"{primary_block_index} belongs to {primary_record['name']}, "
                            f"not declared source_file {source_file}"
                        )
                        removal_failed = True
                        break
                    primary_spans.append(primary_span)
                if removal_failed:
                    continue
                for primary_block_index, primary_span in zip(
                    primary_blocks, primary_spans
                ):
                    scheduled_secondary_primary_removals.add(
                        (page, primary_block_index)
                    )
                    edits_by_file[source_file].append((primary_span[0], primary_span[1], ""))
                if primary_spans:
                    span = (
                        min(item[0] for item in primary_spans),
                        max(item[1] for item in primary_spans),
                    )

        if record["type"] in {"bibliography", "index"}:
            errors.append(
                f"{page}:{block_index}: footnote decision falls inside "
                f"{record['type']} content; classify it as citation/bibliography or review it"
            )
            continue

        chapter_id = record["chapter_id"]
        if role in {"footnote_start", "footnote_definition"}:
            occurrence = len(active_by_chapter_key[(chapter_id, key)]) + 1
            output_key = key if occurrence == 1 else f"{key}-{occurrence}"
            note = {
                "chapter_id": chapter_id,
                "key": key,
                "output_key": output_key,
                "source_page": page,
                "source_file": record["name"],
                "parts": [],
                "start_span": span,
                "source": source,
                "marker_context": str(decision.get("marker_context") or "").strip(),
            }
            active_by_chapter_key[(chapter_id, key)].append(note)
            notes_by_chapter[chapter_id].append(note)
        else:
            active_notes = active_by_chapter_key.get((chapter_id, key), [])
            if not active_notes:
                errors.append(
                    f"{page}:{block_index}: continuation {key} has no earlier "
                    "footnote start in the same logical chapter"
                )
                continue
            note = active_notes[-1]

        if source == "secondary":
            note_text = _strip_note_key(
                str(decision.get("text") or ""),
                key if role != "footnote_continuation" else None,
            )
        else:
            note_text = _strip_note_key(
                source_texts[record["name"]][span[0] : span[1]],
                key if role != "footnote_continuation" else None,
            )
        if not note_text:
            errors.append(f"{page}:{block_index}: footnote text is empty after removing its key")
            continue
        note["parts"].append(note_text)
        if source == "primary":
            edits_by_file[record["name"]].append((span[0], span[1], ""))
        resolved.append(
            {
                "page": page,
                "source": source,
                "block": block_index,
                "role": role,
                "key": key,
                "output_key": note["output_key"],
                "file": record["name"],
                "chapter_id": chapter_id,
                "span": [span[0], span[1]],
                **(
                    {
                        "primary_disposition": str(
                            decision.get("primary_disposition") or ""
                        ).strip().lower(),
                        "primary_blocks": _secondary_primary_blocks(decision),
                    }
                    if source == "secondary"
                    else {}
                ),
            }
        )

    # A continuation may be physically on a later page after ordinary body
    # text.  The sorted decision stream above follows page/block order, so it
    # naturally joins it to the preceding note without moving the body block.
    record_by_name = {record["name"]: record for record in records}
    records_by_chapter: dict[str, list[dict[str, Any]]] = {}
    for record in records:
        records_by_chapter.setdefault(record["chapter_id"], []).append(record)
    for chapter_records in records_by_chapter.values():
        chapter_records.sort(key=lambda item: (item["unit_order"], item["part_index"]))

    for note in (note for notes in notes_by_chapter.values() for note in notes):
        note_record = record_by_name[note["source_file"]]
        source = source_texts[note["source_file"]]
        start = note["start_span"][0]
        marker_candidates = [
            item for item in _marker_spans(source, note["key"])
            if item[1] <= start
        ]
        target_file = note["source_file"]

        if not marker_candidates:
            # If the note appears at the beginning of a split part, its body marker
            # may reside in an earlier part of the same logical chapter unit.
            sibling_records = [
                rec for rec in records_by_chapter.get(note["chapter_id"], [])
                if (rec["unit_order"], rec["part_index"]) < (note_record["unit_order"], note_record["part_index"])
            ]
            cross_part_candidates: list[tuple[str, tuple[int, int, str]]] = []
            for earlier_record in reversed(sibling_records):
                earlier_source = source_texts[earlier_record["name"]]
                earlier_spans = _marker_spans(earlier_source, note["key"])
                if earlier_spans:
                    cross_part_candidates.extend(
                        (earlier_record["name"], span) for span in earlier_spans
                    )
            if len(cross_part_candidates) == 1:
                target_file, selected = cross_part_candidates[0]
                marker_candidates = [selected]
                source = source_texts[target_file]
            elif len(cross_part_candidates) > 1:
                errors.append(
                    f"{note['source_page']}:{note['source_file']}: ambiguous body "
                    f"footnote marker for key {note['key']} across logical chapter parts"
                )
                continue

        if note.get("source") == "secondary":
            marker_context = str(note.get("marker_context") or "").strip()
            if marker_context:
                marker_candidates = [
                    item
                    for item in marker_candidates
                    if _marker_context_matches(source, item, marker_context)
                ]
            if len(marker_candidates) != 1:
                context_hint = (
                    "; provide a unique marker_context"
                    if len(marker_candidates) > 1
                    else ""
                )
                errors.append(
                    f"{note['source_page']}:{note['source_file']}: secondary-only "
                    f"footnote marker for key {note['key']} is not unique"
                    f"{context_hint}"
                )
                continue

        if not marker_candidates:
            errors.append(
                f"{note['source_page']}:{note['source_file']}: no body footnote marker "
                f"found for key {note['key']} before its definition"
            )
            continue
        marker_start, marker_end, marker_kind = marker_candidates[-1]
        replacement = f"[^{note['output_key']}]"
        if marker_kind == "markdown" and source[marker_start:marker_end] == replacement:
            continue
        edits_by_file[target_file].append(
            (marker_start, marker_end, replacement)
        )

    if errors:
        return _write_failure(output_dir, errors)

    try:
        target_dir.mkdir(parents=True, exist_ok=True)
        transformed: dict[str, str] = {
            name: _apply_edits(source_texts[name], edits)
            for name, edits in edits_by_file.items()
        }
        for name, text in source_texts.items():
            if name not in transformed:
                transformed[name] = text

        # Append to the last physical file of each emitted logical TOC unit.
        # Parts of one oversized unit share the same scope; sibling TOC units
        # do not, so their independently restarted footnote numbers remain
        # independent.
        final_file_by_chapter: dict[str, dict[str, Any]] = {}
        for record in records:
            current = final_file_by_chapter.get(record["chapter_id"])
            if current is None or (record["unit_order"], record["part_index"]) >= (
                current["unit_order"],
                current["part_index"],
            ):
                final_file_by_chapter[record["chapter_id"]] = record
        for chapter_id, notes in notes_by_chapter.items():
            target_record = final_file_by_chapter.get(chapter_id)
            if target_record is None:
                raise ValueError(f"no chapter-end unit found for {chapter_id}")
            definitions = []
            for note in notes:
                joined = " ".join(part for part in note["parts"] if part).strip()
                definitions.append(f"[^{note['output_key']}]: {joined}")
            if definitions:
                current = transformed[target_record["name"]].rstrip()
                transformed[target_record["name"]] = (
                    current + "\n\n" + "\n\n".join(definitions) + "\n"
                )

        for name, text in transformed.items():
            atomic_write_text(target_dir / name, text)
    except (OSError, UnicodeError, ValueError) as exc:
        return _write_failure(output_dir, [f"could not write normalized source: {exc}"])

    source_hashes = _markdown_inventory(source_dir)
    target_hashes = _markdown_inventory(target_dir)
    result = {
        "schema_version": FOOTNOTE_NORMALIZATION_SCHEMA_VERSION,
        "source_kind": report.get("source_kind", "ocr"),
        "ocr_evidence_mode": report_mode,
        "status": "validated",
        "valid": True,
        "source_dir": "ocr_markdown",
        "target_dir": "footnote_normalized",
        "source_sha256": source_hashes,
        "target_sha256": target_hashes,
        "candidate_report_sha256": sha256_file(output_dir / "footnote_candidates.json"),
        "decision_validation_sha256": sha256_file(
            output_dir / "footnote_decision_validation.json"
        ),
        "moved_count": len(resolved),
        "preserved_roles": dict(preserved_roles),
        "notes": [
            {
                "chapter_id": note["chapter_id"],
                "key": note["key"],
                "output_key": note["output_key"],
                "source_page": note["source_page"],
                "part_count": len(note["parts"]),
            }
            for notes in notes_by_chapter.values()
            for note in notes
        ],
        "resolved_blocks": resolved,
    }
    atomic_write_text(
        output_dir / "footnote_normalization.json",
        json.dumps(result, ensure_ascii=False, indent=2),
    )
    return result


def footnote_normalization_status(
    output_dir: Path,
    config: Optional[Mapping[str, Any]] = None,
) -> dict[str, Any]:
    """Explain whether the derived chapter-end source still matches inputs."""

    output_dir = Path(output_dir)
    failures: list[dict[str, str]] = []

    def fail(code: str, detail: str) -> None:
        failures.append({"code": code, "detail": detail})

    try:
        report = _load_json(
            output_dir / "footnote_normalization.json",
            "footnote_normalization.json",
        )
        validation = _load_json(
            output_dir / "footnote_decision_validation.json",
            "footnote_decision_validation.json",
        )
    except (OSError, UnicodeError, ValueError, TypeError) as exc:
        fail("checkpoint_missing_or_invalid", str(exc))
        return {
            "current": False,
            "status": "stale",
            "failures": failures,
            "detail": "footnote normalization checkpoint is missing or invalid; "
            "run footnote-prepare, footnote-validate, and footnote-apply",
        }

    if report.get("valid") is not True or report.get("status") != "validated":
        fail(
            "normalization_not_validated",
            "footnote_normalization.json is not a validated checkpoint",
        )
    if validation.get("valid") is not True:
        fail(
            "decision_validation_failed",
            "footnote_decision_validation.json is missing or invalid",
        )
    report_mode = str(report.get("ocr_evidence_mode") or "single_ocr")
    validation_mode = str(validation.get("ocr_evidence_mode") or report_mode)
    if report_mode != validation_mode:
        fail(
            "evidence_mode_mismatch",
            "footnote normalization and decision validation use different OCR evidence modes",
        )
    if config is not None:
        configured_mode = pdf_evidence_mode(output_dir, config)
        if report_mode != configured_mode:
            fail(
                "configured_evidence_mode_mismatch",
                f"checkpoint uses {report_mode}, current configuration requires {configured_mode}",
            )
        if configured_mode == "two_ocr":
            try:
                require_current_consensus(output_dir, config, stage="footnote")
            except ValueError as exc:
                fail("ocr_consensus_stale", str(exc))
    source_dir = output_dir / "ocr_markdown"
    target_dir = output_dir / "footnote_normalized"
    if report.get("source_sha256") != _markdown_inventory(source_dir):
        fail(
            "source_markdown_changed",
            "ocr_markdown changed after footnote normalization",
        )
    if report.get("target_sha256") != _markdown_inventory(target_dir):
        fail(
            "normalized_output_changed",
            "footnote_normalized changed after footnote normalization",
        )
    if report.get("candidate_report_sha256") != sha256_file(
        output_dir / "footnote_candidates.json"
    ):
        fail(
            "candidate_report_stale",
            "footnote_candidates.json changed after footnote-apply",
        )
    if report.get("decision_validation_sha256") != sha256_file(
        output_dir / "footnote_decision_validation.json"
    ):
        fail(
            "decision_validation_stale",
            "footnote_decision_validation.json changed after footnote-apply",
        )

    current = not failures
    if current:
        detail = "current validated footnote normalization matches all inputs"
    else:
        detail = failures[0]["detail"]
        if any(item["code"] == "candidate_report_stale" for item in failures):
            detail += "; rerun footnote-prepare, footnote-validate, and footnote-apply"
        elif any(item["code"] == "decision_validation_stale" for item in failures):
            detail += "; rerun footnote-validate and footnote-apply"
    return {
        "current": current,
        "status": "validated" if current else "stale",
        "failures": failures,
        "detail": detail,
    }


def footnote_normalization_is_current(
    output_dir: Path,
    config: Optional[Mapping[str, Any]] = None,
) -> bool:
    """Return whether the derived chapter-end source still matches inputs."""

    return bool(footnote_normalization_status(output_dir, config=config)["current"])


__all__ = [
    "FOOTNOTE_NORMALIZATION_SCHEMA_VERSION",
    "apply_footnote_normalization",
    "footnote_normalization_status",
    "footnote_normalization_is_current",
]
