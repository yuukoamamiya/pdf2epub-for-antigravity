"""Materialize reviewed page footnotes as logical chapter-end notes.

The preparation step deliberately stops at a small layout decision contract.
This module is the deterministic half that consumes that contract.  It never
decides whether a numbered block is a footnote: only decisions whose role is a
``footnote_*`` role are moved.  Citations, bibliography entries, quotations,
and ordinary body blocks stay in their original unit.
"""

from __future__ import annotations

import re
from collections import defaultdict
from pathlib import Path
from typing import Any, Mapping, Optional

from ..workflow_contracts import atomic_write_json, atomic_write_text, sha256_file
from ..footnote_keys import (
    canonical_numeric_label,
    is_valid_footnote_key,
    marker_label_from_key,
    output_marker_key,
)
from .pdf_evidence import pdf_evidence_mode, require_current_consensus
from .footnote_options import (
    EFFECTIVE_FOOTNOTE_MODES,
    resolve_effective_footnote_mode,
)
from .footnote_structure import FOOTNOTE_STRUCTURE_EXEMPT_TYPES
from .footnote_matching import (
    apply_edits as _apply_edits,
    block_text_variants as _block_text_variants,
    find_exact_signature_spans as _find_exact_signature_spans,
    _load_json,
    load_sidecar_block as _load_sidecar_block,
    marker_context_matches as _marker_context_matches,
    marker_spans as _marker_spans,
    secondary_primary_blocks as _secondary_primary_blocks,
    strip_note_key as _strip_note_key,
)
from .footnote_status import (
    FOOTNOTE_NORMALIZATION_SCHEMA_VERSION,
    markdown_inventory as _markdown_inventory,
    unit_records as _unit_records,
)
from .footnote_contracts import FOOTNOTE_PREPARE_SCHEMA_VERSION
from .endnote_normalization import normalize_endnotes
from .endnote_review import (
    AMBIGUITIES_FILENAME,
    DECISIONS_FILENAME,
    load_endnote_definition_ambiguities,
    load_endnote_definition_decisions,
)
from .endnote_scopes import build_endnote_scope_directory


_MOVED_ROLES = frozenset(
    {"footnote_start", "footnote_continuation", "footnote_definition"}
)
_NON_MOVED_ROLES = frozenset({"body", "citation", "bibliography"})


def _split_multi_sups(text: str) -> str:
    """Split comma-separated superscript markers like <sup>52, 53</sup> into individual <sup> tags."""
    def repl(m: re.Match) -> str:
        inner = m.group(1)
        m_symbol = re.fullmatch(r"([A-Za-z0-9_-]+)\s*(\\\*+|\*+|[†‡§]+)", inner.strip())
        if m_symbol:
            return f"<sup>{m_symbol.group(1)}</sup>{m_symbol.group(2)}"
        parts = [p.strip() for p in re.split(r"[,;]\s*", inner) if p.strip()]
        if len(parts) > 1 and all(re.fullmatch(r"[A-Za-z0-9_-]+", p) for p in parts):
            return "".join(f"<sup>{p}</sup>" for p in parts)
        return m.group(0)

    return re.sub(r"<sup\b[^>]*>(.*?)</sup>", repl, text, flags=re.IGNORECASE | re.DOTALL)


def _format_footnote_definition(key: str, content: str) -> str:
    """Nest multiline Markdown blocks under a generated footnote definition."""
    lines = content.strip().splitlines()
    if not lines:
        return f"[^{key}]:"
    if re.match(r"^(?:>|[-*+]\s+|\d+[.)]\s+|\|.*\||```|~~~)", lines[0].lstrip()):
        nested = [f"    {line}" if line.strip() else "" for line in lines]
        return f"[^{key}]:\n" + "\n".join(nested)
    nested = [
        f"    {line}" if line.strip() else ""
        for line in lines[1:]
    ]
    return f"[^{key}]: {lines[0]}" + (
        "\n" + "\n".join(nested) if nested else ""
    )


def _join_footnote_parts(parts: list[str]) -> str:
    """Soft-wrap prose fragments while preserving explicit Markdown blocks."""
    result = ""
    block_start = re.compile(
        r"^(?:>|[-*+]\s+|\d+[.)]\s+|\|.*\||```|~~~|"
        r"</?(?:blockquote|table|pre|ul|ol)\b)",
        re.IGNORECASE,
    )
    for part in (value.strip() for value in parts if value and value.strip()):
        if not result:
            result = part
            continue
        separator = "\n\n" if block_start.match(part.lstrip()) else "\n"
        result += separator + part
    return result.strip()


def _candidate_decisions(
    report: Mapping[str, Any],
    validation: Mapping[str, Any],
) -> list[dict[str, Any]]:
    """Combine reviewed decisions with deterministic high-confidence ones."""

    decisions_by_address: dict[tuple[int, str, int], dict[str, Any]] = {}
    high_confidence_addresses: set[tuple[int, str, int]] = set()
    for page_report in report.get("pages", []) or []:
        if not isinstance(page_report, Mapping):
            continue
        for candidate in page_report.get("candidates", []) or []:
            if not isinstance(candidate, Mapping) or candidate.get("confidence") != "high":
                continue
            try:
                high_confidence_addresses.add(
                    (
                        int(candidate["page"]),
                        str(candidate.get("source") or "primary").strip().lower(),
                        int(candidate["block"]),
                    )
                )
            except (KeyError, TypeError, ValueError):
                continue
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
        # High-confidence candidates are already accepted by the local,
        # deterministic rule.  Older handoffs sometimes repeated them as
        # ``body`` decisions; allowing that stale review output to override the
        # local result silently dropped valid page footnotes.
        if address not in high_confidence_addresses or item.get("role") == "footnote_continuation":
            decisions_by_address[address] = dict(item)

    for page_report in report.get("pages", []) or []:
        if not isinstance(page_report, Mapping):
            continue
        for candidate in page_report.get("candidates", []) or []:
            if not isinstance(candidate, Mapping) or candidate.get("confidence") != "high":
                continue
            cand_bbox = candidate.get("bbox")
            cand_label = str(candidate.get("label") or "").strip().lower()
            if cand_label != "footnote" and cand_bbox and float(cand_bbox[1]) < 0.45:
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
                    "role": str(candidate.get("local_role") or "footnote_start"),
                    "key": str(candidate.get("key") or ""),
                    **(
                        {"marker_label": str(candidate.get("marker_label") or "")}
                        if candidate.get("marker_label")
                        else {}
                    ),
                    **(
                        {"requires_marker_context": True}
                        if candidate.get("requires_marker_context") is True
                        else {}
                    ),
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
    *,
    claimed_spans: Optional[set[tuple[str, int, int]]] = None,
    pages_dir: Optional[Path] = None,
) -> tuple[dict[str, Any], tuple[int, int]]:
    expected = _records_for_page(records, page)
    if not expected:
        raise ValueError(f"no refinement unit covers OCR page {page}")
    matches: list[tuple[dict[str, Any], tuple[int, int]]] = []
    seen: set[tuple[str, int, int]] = set()
    for block_text in block_texts:
        structured_kind = None
        if block_text.startswith("\x00ordered-list\x00"):
            structured_kind = "ordered"
            block_text = block_text[len("\x00ordered-list\x00") :]
        elif block_text.startswith("\x00unordered-list\x00"):
            structured_kind = "unordered"
            block_text = block_text[len("\x00unordered-list\x00") :]
        for record in expected:
            for span in _find_exact_signature_spans(source_texts[record["name"]], block_text):
                if structured_kind and not _is_complete_markdown_list(
                    source_texts[record["name"]], span, structured_kind
                ):
                    continue
                identity = (record["name"], span[0], span[1])
                if claimed_spans and identity in claimed_spans:
                    continue
                if identity not in seen:
                    seen.add(identity)
                    matches.append((record, span))
        if matches:
            break
    if len(matches) > 1:
        if pages_dir is not None:
            page_md = Path(pages_dir) / f"page_{page:03d}.md"
            if page_md.is_file():
                try:
                    other_lines = [
                        l.strip()
                        for l in page_md.read_text(encoding="utf-8").splitlines()
                        if len(l.strip()) > 15
                        and not l.startswith("#")
                        and not l.startswith(("①", "②", "③", "④", "⑤", "⑥", "⑦", "⑧", "⑨", "⑩"))
                    ]
                except (OSError, UnicodeDecodeError):
                    other_lines = []
                if other_lines:
                    file_scores: dict[str, int] = {}
                    for r, _span in matches:
                        fn = r["name"]
                        st = source_texts[fn]
                        file_scores[fn] = sum(1 for l in other_lines[:5] if l[:20] in st)
                    max_s = max(file_scores.values()) if file_scores else 0
                    if max_s > 0:
                        matches = [m for m in matches if file_scores[m[0]["name"]] == max_s]
                    if len(matches) > 1 and all(m[0]["name"] == matches[0][0]["name"] for m in matches):
                        target_text = source_texts[matches[0][0]["name"]]
                        page_positions = [target_text.find(l[:20]) for l in other_lines if target_text.find(l[:20]) != -1]
                        if page_positions:
                            avg_pos = sum(page_positions) / len(page_positions)
                            matches.sort(key=lambda m: abs(m[1][0] - avg_pos))
                            matches = [matches[0]]
    if len(matches) != 1:
        if not matches:
            raise ValueError(f"could not locate OCR block text on page {page}")
        raise ValueError(f"OCR block text on page {page} matched multiple source spans")
    return matches[0]


def _is_complete_markdown_list(
    source: str, span: tuple[int, int], kind: str
) -> bool:
    start, end = span
    if start and source[start - 1] != "\n":
        return False
    if end < len(source) and source[end] != "\n":
        return False
    lines = source[start:end].splitlines()
    if not lines:
        return False
    pattern = (
        r"[ \t]*\d+[.)][ \t]+\S"
        if kind == "ordered"
        else r"[ \t]*[-*+][ \t]+\S"
    )
    return all(re.match(pattern, line) for line in lines)


def _write_failure(output_dir: Path, errors: list[str]) -> dict[str, Any]:
    result = {
        "schema_version": FOOTNOTE_NORMALIZATION_SCHEMA_VERSION,
        "status": "human_review_required",
        "valid": False,
        "errors": errors,
    }
    atomic_write_json(Path(output_dir) / "footnote_normalization.json", result)
    return result


def _write_success_report(output_dir: Path, result: dict[str, Any]) -> dict[str, Any]:
    """Commit the validated normalization checkpoint after all outputs exist."""
    atomic_write_json(Path(output_dir) / "footnote_normalization.json", result)
    return result


def _write_normalized_units(
    source_dir: Path,
    target_dir: Path,
    transformed: Mapping[str, str],
) -> tuple[dict[str, str], dict[str, str]]:
    """Atomically materialize converted units and return source/target hashes."""
    target_dir.mkdir(parents=True, exist_ok=True)
    for name, text in transformed.items():
        atomic_write_text(target_dir / name, text)
    source_hashes = _markdown_inventory(source_dir)
    target_hashes = _markdown_inventory(target_dir)
    extra_files = sorted(set(target_hashes) - set(source_hashes))
    if extra_files:
        raise ValueError(
            "footnote_normalized contains stale Markdown units not present "
            f"in ocr_markdown: {', '.join(extra_files[:20])}"
        )
    return source_hashes, target_hashes


def _append_chapter_end_definitions(
    transformed: dict[str, str],
    records: list[dict[str, Any]],
    notes_by_chapter: Mapping[str, list[dict[str, Any]]],
) -> None:
    """Render each chapter's reviewed notes at its last physical unit."""
    final_file_by_chapter: dict[str, dict[str, Any]] = {}
    for record in records:
        current = final_file_by_chapter.get(record["chapter_id"])
        if current is None or (record["unit_order"], record["part_index"]) >= (
            current["unit_order"], current["part_index"]
        ):
            final_file_by_chapter[record["chapter_id"]] = record
    for chapter_id, notes in notes_by_chapter.items():
        target_record = final_file_by_chapter.get(chapter_id)
        if target_record is None:
            raise ValueError(f"no chapter-end unit found for {chapter_id}")
        definitions = [
            _format_footnote_definition(
                note["output_key"], _join_footnote_parts(note["parts"])
            )
            for note in notes
        ]
        if definitions:
            current = transformed[target_record["name"]].rstrip()
            transformed[target_record["name"]] = (
                current + "\n\n" + "\n\n".join(definitions) + "\n"
            )


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
    if report.get("schema_version") != FOOTNOTE_PREPARE_SCHEMA_VERSION:
        return _write_failure(
            output_dir,
            ["footnote candidate report uses an older marker contract; rerun footnote-prepare"],
        )
    if validation.get("schema_version") != FOOTNOTE_PREPARE_SCHEMA_VERSION:
        return _write_failure(
            output_dir,
            ["footnote decision validation uses an older marker contract; rerun footnote-validate"],
        )
    report_footnote_mode = str(
        report.get("footnote_mode") or "footnotes"
    ).strip().lower()
    if report_footnote_mode not in EFFECTIVE_FOOTNOTE_MODES:
        return _write_failure(
            output_dir,
            [f"unsupported footnote mode in candidate report: {report_footnote_mode}"],
        )
    validation_mode = str(validation.get("ocr_evidence_mode") or report_mode)
    validation_footnote_mode = str(
        validation.get("footnote_mode") or report_footnote_mode
    ).strip().lower()
    if validation_footnote_mode != report_footnote_mode:
        return _write_failure(
            output_dir,
            [
                "footnote candidate and decision checkpoints use different "
                "footnotes.mode values; rerun footnote-prepare and footnote-validate",
            ],
        )
    if config is not None:
        try:
            configured_mode = pdf_evidence_mode(output_dir, config)
        except ValueError as exc:
            return _write_failure(output_dir, [f"invalid PDF evidence metadata: {exc}"])
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
    try:
        resolution = resolve_effective_footnote_mode(output_dir, config)
    except ValueError as exc:
        return _write_failure(output_dir, [str(exc)])
    if report_footnote_mode != resolution["effective"]:
        return _write_failure(
            output_dir,
            [
                "footnote checkpoints were prepared with a different "
                "footnotes.mode; rerun footnote-prepare and footnote-validate",
            ],
        )
    recorded_configured = report.get("configured_footnote_mode")
    if (
        isinstance(recorded_configured, str)
        and recorded_configured.strip().lower() != resolution["configured"]
    ):
        return _write_failure(
            output_dir,
            [
                "footnote checkpoints were prepared with a different configured "
                "footnotes.mode; rerun footnote-prepare and footnote-validate",
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

    endnote_definition_decisions: Optional[list[dict[str, Any]]] = None
    endnote_ambiguities_sha256: Optional[str] = None
    endnote_decisions_sha256: Optional[str] = None
    try:
        ambiguity_manifest = load_endnote_definition_ambiguities(output_dir)
    except ValueError as exc:
        return _write_failure(output_dir, [str(exc)])
    ambiguity_items = (
        ambiguity_manifest.get("items", [])
        if isinstance(ambiguity_manifest, Mapping)
        else []
    )
    if isinstance(ambiguity_items, list) and ambiguity_items:
        endnote_validation = validation.get("endnote_definitions")
        if (
            not isinstance(endnote_validation, Mapping)
            or endnote_validation.get("valid") is not True
        ):
            return _write_failure(
                output_dir,
                [
                    "centralized endnote definition decisions are not validated; "
                    "run footnote-validate after reviewing "
                    f"{AMBIGUITIES_FILENAME}",
                ],
            )
        try:
            decisions_data = load_endnote_definition_decisions(output_dir)
        except ValueError as exc:
            return _write_failure(output_dir, [str(exc)])
        if decisions_data is None:
            return _write_failure(
                output_dir,
                [f"{DECISIONS_FILENAME} is missing; run footnote-validate"],
            )
        raw_decisions = decisions_data.get("decisions")
        if not isinstance(raw_decisions, list):
            return _write_failure(
                output_dir,
                [f"{DECISIONS_FILENAME} must contain a decisions array"],
            )
        endnote_definition_decisions = [
            dict(item) for item in raw_decisions if isinstance(item, Mapping)
        ]
        endnote_ambiguities_sha256 = sha256_file(
            output_dir / AMBIGUITIES_FILENAME
        )
        endnote_decisions_sha256 = sha256_file(output_dir / DECISIONS_FILENAME)

    source_dir = output_dir / "ocr_markdown"
    target_dir = output_dir / "footnote_normalized"
    if not source_dir.is_dir():
        return _write_failure(output_dir, [f"source directory is missing: {source_dir}"])

    try:
        records = _unit_records(output_dir)
        source_texts = {
            record["name"]: _split_multi_sups((source_dir / record["name"]).read_text(encoding="utf-8"))
            for record in records
        }
        # Duplicate part entries refer to the same file only in malformed
        # historical checkpoints.  Refuse them instead of choosing silently.
        if len(source_texts) != len(records):
            raise ValueError("tree_progress contains duplicate Markdown unit filenames")
    except (OSError, UnicodeError, ValueError) as exc:
        return _write_failure(output_dir, [str(exc)])

    transformed: dict[str, str] = dict(source_texts)
    resolved: list[dict[str, Any]] = []
    preserved_roles: dict[str, int] = defaultdict(int)
    endnote_report: dict[str, Any] = {
        "status": "disabled" if report_footnote_mode == "disabled" else "not_started",
        "valid": True,
        "source_files": [],
        "definition_keys": [],
        "definition_count": 0,
        "normalized_definition_count": 0,
        "converted_reference_count": 0,
        "reference_counts": {},
        "orphan_definition_keys": [],
        "scope_mode": "global",
        "scope_count": 0,
        "scopes": [],
        "scoped_keys": [],
        "double_numbered_definition_count": 0,
        "duplicate_definition_occurrences": [],
        "orphan_scoped_definitions": [],
        "errors": [],
    }

    sidecar_cache: dict[int, dict[str, Any]] = {}
    decisions = (
        _candidate_decisions(report, validation)
        if report_footnote_mode == "footnotes"
        else []
    )
    edits_by_file: dict[str, list[tuple[int, int, str]]] = defaultdict(list)
    notes_by_chapter: dict[str, list[dict[str, Any]]] = defaultdict(list)
    active_by_chapter_key: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    errors: list[str] = []
    scheduled_secondary_primary_removals: set[tuple[int, int]] = set()
    claimed_primary_spans: set[tuple[str, int, int]] = set()

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
        if not is_valid_footnote_key(key):
            key = canonical_numeric_label(key) or key
        marker_label = str(
            decision.get("marker_label")
            or marker_label_from_key(key)
            or key
        ).strip()
        if not is_valid_footnote_key(key):
            errors.append(f"moved footnote at {page}:{block_index} has no valid key")
            continue
        if source == "primary":
            try:
                block = _load_sidecar_block(output_dir, page, block_index, sidecar_cache)
                block_texts = _block_text_variants(output_dir, page, block)
                if not block_texts:
                    raise ValueError("OCR block is empty")
                record, span = _locate_block(
                    source_texts,
                    records,
                    page,
                    block_texts,
                    claimed_spans=claimed_primary_spans,
                    pages_dir=output_dir / "pages",
                )
                claimed_primary_spans.add((record["name"], span[0], span[1]))
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
                            claimed_spans=claimed_primary_spans,
                            pages_dir=output_dir / "pages",
                        )
                        claimed_primary_spans.add(
                            (primary_record["name"], primary_span[0], primary_span[1])
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

        if record["type"] in FOOTNOTE_STRUCTURE_EXEMPT_TYPES:
            errors.append(
                f"{page}:{block_index}: footnote decision falls inside "
                f"{record['type']} content; classify it as citation/bibliography or review it"
            )
            continue

        chapter_id = record["chapter_id"]
        if role in {"footnote_start", "footnote_definition"}:
            occurrence = len(active_by_chapter_key[(chapter_id, key)]) + 1
            output_key = output_marker_key(key, marker_label, occurrence)
            note = {
                "chapter_id": chapter_id,
                "key": key,
                "marker_label": marker_label,
                "output_key": output_key,
                "source_page": page,
                "source_file": record["name"],
                "parts": [],
                "start_span": span,
                "source": source,
                "marker_context": str(decision.get("marker_context") or "").strip(),
                "requires_marker_context": decision.get("requires_marker_context") is True,
            }
            active_by_chapter_key[(chapter_id, key)].append(note)
            notes_by_chapter[chapter_id].append(note)
        else:
            active_notes = active_by_chapter_key.get((chapter_id, key), [])
            if not active_notes:
                for earlier_record in reversed(records):
                    if earlier_record["start_page"] <= page <= earlier_record["end_page"] or earlier_record["end_page"] == page - 1:
                        earlier_cid = earlier_record["chapter_id"]
                        if earlier_cid != chapter_id and (earlier_cid, key) in active_by_chapter_key:
                            active_notes = active_by_chapter_key[(earlier_cid, key)]
                            chapter_id = earlier_cid
                            break
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
                marker_label if role != "footnote_continuation" else None,
            )
        else:
            note_text = _strip_note_key(
                source_texts[record["name"]][span[0] : span[1]],
                key if role != "footnote_continuation" else None,
                marker_label if role != "footnote_continuation" else None,
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
                "marker_label": marker_label,
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

    all_notes = [note for notes in list(notes_by_chapter.values()) for note in list(notes)]
    for note in all_notes:
        note_record = record_by_name[note["source_file"]]
        source = source_texts[note["source_file"]]
        start = note["start_span"][0]
        claimed_spans = {
            (s, e) for s, e, _ in edits_by_file.get(note["source_file"], [])
        }
        marker_candidates = [
            item for item in _marker_spans(source, note["key"], note["marker_label"])
            if item[1] <= start and (item[0], item[1]) not in claimed_spans
        ]
        target_file = note["source_file"]

        if not marker_candidates:
            # If the note appears at the beginning of a split part, its body marker
            # may reside in an earlier part of the same logical chapter unit.
            sibling_records = [
                rec for rec in records_by_chapter.get(note["chapter_id"], [])
                if (rec["unit_order"], rec["part_index"]) < (note_record["unit_order"], note_record["part_index"])
            ]
            for earlier_record in reversed(sibling_records):
                earlier_source = source_texts[earlier_record["name"]]
                earlier_spans = _marker_spans(earlier_source, note["key"], note["marker_label"])
                claimed_spans = {
                    (start, end)
                    for start, end, _ in edits_by_file.get(earlier_record["name"], [])
                }
                earlier_spans = [
                    span
                    for span in earlier_spans
                    if (span[0], span[1]) not in claimed_spans
                ]
                if earlier_spans:
                    target_file = earlier_record["name"]
                    marker_candidates = [earlier_spans[-1]]
                    source = source_texts[target_file]
                    break

            if not marker_candidates:
                # If a section begins mid-page, page footnotes belonging to the
                # previous section's trailing body text may be placed at the bottom
                # of the shared page. Check immediately preceding chapter covering this page.
                preceding_records = [
                    rec for rec in records
                    if rec["end_page"] >= note["source_page"] and rec["unit_order"] < note_record["unit_order"]
                ]
                if preceding_records:
                    preceding_records.sort(
                        key=lambda r: (r["unit_order"], r["part_index"]),
                        reverse=True,
                    )
                    for prec_record in preceding_records:
                        prec_source = source_texts[prec_record["name"]]
                        prec_spans = _marker_spans(prec_source, note["key"], note["marker_label"])
                        claimed_spans = {
                            (start, end)
                            for start, end, _ in edits_by_file.get(prec_record["name"], [])
                        }
                        prec_spans = [
                            span
                            for span in prec_spans
                            if (span[0], span[1]) not in claimed_spans
                        ]
                        if prec_spans:
                            target_file = prec_record["name"]
                            marker_candidates = [prec_spans[-1]]
                            source = source_texts[target_file]
                            old_chapter_id = note["chapter_id"]
                            new_chapter_id = prec_record["chapter_id"]
                            if old_chapter_id != new_chapter_id:
                                if note in notes_by_chapter[old_chapter_id]:
                                    notes_by_chapter[old_chapter_id].remove(note)
                                new_occurrence = len(active_by_chapter_key[(new_chapter_id, note["key"])]) + 1
                                note["output_key"] = output_marker_key(
                                    note["key"], note["marker_label"], new_occurrence
                                )
                                note["chapter_id"] = new_chapter_id
                                active_by_chapter_key[(new_chapter_id, note["key"])].append(note)
                                notes_by_chapter[new_chapter_id].append(note)
                                for res_item in resolved:
                                    if (
                                        res_item.get("file") == note["source_file"]
                                        and res_item.get("page") == note["source_page"]
                                        and res_item.get("key") == note["key"]
                                    ):
                                        res_item["chapter_id"] = new_chapter_id
                                        res_item["output_key"] = note["output_key"]
                            break

        marker_context = str(note.get("marker_context") or "").strip()
        if marker_context:
            marker_candidates = [
                item
                for item in marker_candidates
                if _marker_context_matches(source, item, marker_context)
            ]
        marker_is_typed = marker_label_from_key(note["key"]) is not None
        if (
            note.get("source") == "secondary"
            or note.get("requires_marker_context")
            or bool(marker_context)
            or (marker_is_typed and len(marker_candidates) > 1)
        ):
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

    transformed = {
        name: _apply_edits(source_texts[name], edits)
        for name, edits in edits_by_file.items()
    }
    for name, text in source_texts.items():
        if name not in transformed:
            transformed[name] = text

    # Parts share a chapter scope; sibling TOC units keep independent numbering.
    _append_chapter_end_definitions(transformed, records, notes_by_chapter)

    if report_footnote_mode != "disabled":
        scope_directory: Optional[dict[str, Any]] = None
        scope_error: Optional[str] = None
        try:
            toc_tree = _load_json(output_dir / "toc_tree.json", "toc_tree.json")
            progress = _load_json(
                output_dir / "ocr_markdown" / "tree_progress.json",
                "ocr_markdown/tree_progress.json",
            )
            scope_directory = build_endnote_scope_directory(
                toc_tree, progress, transformed
            )
        except (OSError, UnicodeError, ValueError, TypeError) as exc:
            scope_error = str(exc)
        try:
            transformed, endnote_report = normalize_endnotes(
                transformed,
                {record["name"]: record["type"] for record in records},
                scopes=scope_directory,
                definition_decisions=endnote_definition_decisions,
            )
        except (OSError, UnicodeError, ValueError) as exc:
            detail = f"could not normalize centralized endnotes: {exc}"
            if scope_directory is None and scope_error:
                detail += (
                    "; per-chapter endnote scopes are unavailable: "
                    f"{scope_error}"
                )
            return _write_failure(output_dir, [detail])

    try:
        source_hashes, target_hashes = _write_normalized_units(
            source_dir, target_dir, transformed
        )
    except (OSError, UnicodeError, ValueError) as exc:
        return _write_failure(output_dir, [f"could not write normalized source: {exc}"])
    result = {
        "schema_version": FOOTNOTE_NORMALIZATION_SCHEMA_VERSION,
        "source_kind": report.get("source_kind", "ocr"),
        "ocr_evidence_mode": report_mode,
        "footnote_mode": report_footnote_mode,
        "configured_footnote_mode": report.get("configured_footnote_mode"),
        "footnote_mode_source": report.get("footnote_mode_source"),
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
        "endnote_definition_ambiguities_sha256": endnote_ambiguities_sha256,
        "endnote_definition_decisions_sha256": endnote_decisions_sha256,
        "moved_count": len(resolved),
        "preserved_roles": dict(preserved_roles),
        "notes": [
            {
                "chapter_id": note["chapter_id"],
                "key": note["key"],
                "marker_label": note["marker_label"],
                "output_key": note["output_key"],
                "source_page": note["source_page"],
                "part_count": len(note["parts"]),
            }
            for notes in notes_by_chapter.values()
            for note in notes
        ],
        "resolved_blocks": resolved,
        "endnotes": endnote_report,
        "tree_progress_sha256": sha256_file(
            output_dir / "ocr_markdown" / "tree_progress.json"
        ),
        "toc_tree_sha256": (
            sha256_file(output_dir / "toc_tree.json")
            if (output_dir / "toc_tree.json").is_file()
            else None
        ),
    }
    return _write_success_report(output_dir, result)


__all__ = [
    "FOOTNOTE_NORMALIZATION_SCHEMA_VERSION",
    "apply_footnote_normalization",
]
