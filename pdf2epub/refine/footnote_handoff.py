"""Prepare the footnote review hand-off and sparse unit contexts.

Candidate detection remains in :mod:`footnote_prepare`; this module owns the
filesystem contract presented to the workspace Subagent.
"""

from __future__ import annotations

import json
import hashlib
import re
from copy import deepcopy
from pathlib import Path
from typing import Any, Mapping, Optional

from ..workflow_contracts import atomic_write_json, atomic_write_text, sha256_file
from ..footnote_markdown import collect_footnote_definition_blocks
from .endnote_review import (
    AMBIGUITIES_FILENAME,
    DECISIONS_FILENAME,
    collect_endnote_definition_ambiguities,
    write_endnote_definition_ambiguities,
)
from .footnote_contracts import FOOTNOTE_PREPARE_SCHEMA_VERSION, FOOTNOTE_UNIT_CONTEXT_PROJECTION_VERSION
from .footnote_structure import load_refined_units as _load_refined_units
from .footnote_prepare import prepare_footnote_candidates
from .footnote_review_packet import validate_review_packet, write_review_packet
from .footnote_page_evidence import page_sidecar_index
from .footnote_status import unit_records
from .footnote_status import markdown_inventory
from .footnote_structure import FOOTNOTE_STRUCTURE_EXEMPT_TYPES
from .footnote_visual import ensure_visual_review_images

def _unit_context_path(output_dir: Path, source_name: str) -> Path:
    safe_name = Path(str(source_name)).name
    if (
        not safe_name
        or safe_name in {".", ".."}
        or safe_name != str(source_name)
        or Path(safe_name).suffix.lower() != ".md"
    ):
        raise ValueError(f"Invalid refinement unit filename: {source_name}")
    return output_dir / "footnote_contexts" / f"{safe_name}.json"


def _project_unit_contexts(
    report: Mapping[str, Any],
    packet_data: Mapping[str, Any],
    refined_units: list[Mapping[str, Any]],
    page_index: Mapping[int, Mapping[str, Any]],
    review_packet: str,
) -> dict[str, dict[str, Any]]:
    """Build canonical sparse projections from already validated, explicit inputs."""
    page_reports = report.get("pages", [])
    cross_windows = report.get("cross_page_windows", [])
    unit_contexts: dict[str, dict[str, Any]] = {}
    for unit in refined_units:
        names = [str(name) for name in (unit.get("part_files") or [unit.get("file")]) if name]
        if not names:
            raise ValueError("refined footnote unit is missing its scoped source filename")
        if len(names) != len(set(names)):
            raise ValueError(f"refined footnote unit {names} contains duplicate source filenames")
        for name in names:
            _unit_context_path(Path("."), name)
            if name in unit_contexts:
                raise ValueError(f"duplicate refined footnote source unit filename: {name}")
        page_range = unit.get("page_range")
        if not isinstance(page_range, list) or len(page_range) != 2:
            raise ValueError(f"refined footnote unit {names} has no valid page range")
        try:
            start_page, end_page = int(page_range[0]), int(page_range[1])
        except (TypeError, ValueError):
            raise ValueError(f"refined footnote unit {names} has a nonnumeric page range: {page_range}")
        known_pages = page_index.keys()
        if (
            start_page < 1 or end_page < start_page or not known_pages
            or end_page > max(known_pages)
            or end_page - start_page + 1 > len(page_index)
        ):
            raise ValueError(f"invalid refined page range for footnote context {names}: {page_range}")
        windows = [
            _compact_window_context(item) for item in cross_windows
            if any(start_page <= int(page) <= end_page for page in item.get("pages", []))
        ]
        continuation_reviews = [
            _compact_window_context(item) for item in report.get("continuation_reviews", [])
            if any(start_page <= int(page) <= end_page for page in item.get("pages", []))
        ]
        evidence_pages = set(range(start_page, end_page + 1))
        for window in [*windows, *continuation_reviews]:
            evidence_pages.update(
                int(page) for page in window.get("pages", [])
                if str(page).lstrip("-").isdigit()
            )
        pages = [
            _compact_page_context(item) for item in page_reports
            if int(item.get("page", -1)) in evidence_pages
        ]
        by_evidence_page = {int(item["page"]): item for item in pages if str(item.get("page", "")).isdigit()}
        visual_inventory = report.get("visual_evidence", {}).get("files", {}) if isinstance(report.get("visual_evidence"), Mapping) else {}
        for page_number in sorted(evidence_pages):
            page_context = by_evidence_page.setdefault(page_number, {
                "page": page_number,
                "sidecar": f"pages/page_{page_number:03d}.ocr.json",
            })
            image = visual_inventory.get(str(page_number), visual_inventory.get(page_number)) if isinstance(visual_inventory, Mapping) else None
            if isinstance(image, str) and not page_context.get("visual_file"):
                page_context["visual_file"] = image
        pages = [by_evidence_page[number] for number in sorted(by_evidence_page)]
        for page_context in pages:
            evidence = page_index.get(int(page_context["page"]), {})
            primary = evidence.get("primary")
            secondary = evidence.get("secondary")
            if not primary:
                raise ValueError(f"footnote primary sidecar missing for page {page_context['page']}")
            page_context["sidecar"] = primary["path"]
            page_context["sidecar_sha256"] = primary["sha256"]
            if secondary:
                page_context["secondary_sidecar"] = secondary["path"]
                page_context["secondary_sidecar_sha256"] = secondary["sha256"]
            else:
                page_context.pop("secondary_sidecar", None)
                page_context.pop("secondary_sidecar_sha256", None)
        # Each part is a distinct Markdown permission boundary. Page evidence
        # is retained as one shared read-only projection, but never lists its
        # sibling parts as authorized source units.
        refs: set[str] = set()
        for page_context in pages:
            refs.update(str(ref) for ref in page_context.get("candidate_refs", []))
            for region in page_context.get("footnote_regions", []):
                refs.update(str(ref) for ref in region.get("candidate_refs", []))
        for window in [*windows, *continuation_reviews]:
            for key in ("left_candidate_refs", "right_candidate_refs"):
                refs.update(str(ref) for ref in window.get(key, []))
            if window.get("source_note_ref"):
                refs.add(str(window["source_note_ref"]))
        packet_candidates = packet_data.get("candidates", {})
        page_by_number = {int(page["page"]): page for page in pages if str(page.get("page", "")).isdigit()}
        local_facts = {}
        if isinstance(packet_candidates, Mapping):
            for ref in sorted(refs):
                fact = packet_candidates.get(ref)
                if not isinstance(fact, Mapping):
                    continue
                page_context = page_by_number.get(int(fact.get("page", 0)), {})
                local_facts[ref] = _unit_fact_projection(fact, page_context)
        if refs - set(local_facts):
            raise ValueError(
                "footnote unit context contains candidate refs missing from its packet: "
                + ", ".join(sorted(refs - set(local_facts))[:10])
            )
        context = {
            "schema_version": FOOTNOTE_PREPARE_SCHEMA_VERSION,
            "projection_version": FOOTNOTE_UNIT_CONTEXT_PROJECTION_VERSION,
            "task": "footnote-prepare",
            "source_kind": report.get("source_kind"),
            "ocr_evidence_mode": report.get("ocr_evidence_mode"),
            "footnote_mode": report.get("footnote_mode"),
            "source_packet": {
                "path": review_packet,
                "usage": "provenance_only_do_not_open_for_unit_work",
            },
            "source_units": [],
            "page_range": [start_page, end_page],
            "pages": pages,
            "cross_page_windows": windows,
            "continuation_reviews": continuation_reviews,
            "decisions": [
                item
                for item in report.get("decisions", [])
                if isinstance(item, Mapping)
                and start_page <= int(item.get("page", -1)) <= end_page
            ],
            "candidates": local_facts,
        }
        for name in names:
            unit_context = deepcopy(context)
            unit_context["source_units"] = [name]
            unit_contexts[name] = unit_context
    return unit_contexts


def _write_unit_contexts(
    output_dir: Path,
    report: Mapping[str, Any],
    *,
    review_packet: Optional[str] = None,
    review_packet_sha256: Optional[str] = None,
) -> dict[str, str]:
    """Persist canonical sparse contexts and return their relative paths."""
    if review_packet is None or review_packet_sha256 is None:
        try:
            current_manifest = json.loads((output_dir / "footnote_subagent_manifest.json").read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            current_manifest = {}
        review_packet = review_packet or current_manifest.get("review_packet")
        review_packet_sha256 = review_packet_sha256 or current_manifest.get("review_packet_sha256")
    if not isinstance(review_packet, str) or not isinstance(review_packet_sha256, str):
        raise ValueError("footnote review packet provenance is missing; rerun footnote-prepare")
    try:
        (output_dir / review_packet).resolve().relative_to(output_dir.resolve())
    except ValueError as exc:
        raise ValueError("footnote review packet path escapes output directory") from exc
    try:
        packet_data = json.loads((output_dir / review_packet).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"footnote review packet is unreadable; rerun footnote-prepare: {exc}") from exc
    if not isinstance(packet_data, Mapping):
        raise ValueError("footnote review packet is invalid; rerun footnote-prepare")
    page_index = page_sidecar_index(output_dir, report)
    projected = _project_unit_contexts(
        report, packet_data, _load_refined_units(output_dir), page_index, review_packet,
    )
    result = {}
    for name, context in projected.items():
        path = _unit_context_path(output_dir, name)
        atomic_write_json(path, context)
        result[name] = path.relative_to(output_dir).as_posix()
    return result


def _compact_page_context(page: Mapping[str, Any]) -> dict[str, Any]:
    """Keep review facts and evidence paths without serializing scan payloads."""
    projected = {
        key: page[key] for key in (
            "page", "sidecar", "secondary_sidecar", "visual_file",
            "consensus_visual_review", "consensus_status",
        ) if key in page
    }
    refs = []
    for field, default_source in (("candidates", "primary"), ("audit_candidates", "primary"), ("secondary_only_candidates", "secondary")):
        for row in page.get(field, []) or []:
            if isinstance(row, Mapping):
                refs.append(f"{int(row.get('page', page.get('page', 0)))}:{row.get('source', default_source)}:{int(row.get('block', -1))}")
    projected["candidate_refs"] = sorted(set(refs))
    projected["footnote_regions"] = [
        {key: region[key] for key in ("region_id", "disposition", "reasons", "region_visual_file") if key in region}
        | {"candidate_refs": [
            f"{int(block.get('page', page.get('page', 0)))}:{block.get('source', 'primary')}:{int(block.get('block', -1))}"
            for block in region.get("blocks", []) if isinstance(block, Mapping)
        ]}
        for region in page.get("footnote_regions", [])
        if isinstance(region, Mapping) and region.get("disposition") == "visual_audit_required"
    ]
    return projected


def _unit_fact_projection(fact: Mapping[str, Any], page_context: Mapping[str, Any]) -> dict[str, Any]:
    """Project a packet fact into the least text required by one unit."""
    source = str(fact.get("source") or "primary")
    projected = {
        key: fact[key] for key in (
            "page", "source", "block", "key", "label", "bbox",
            "confidence", "local_role", "marker_label", "review_reason",
            "text_ending", "body_flow_warning",
        ) if key in fact
    }
    if fact.get("text") and (not fact.get("key") or str(fact.get("local_role") or "").endswith("continuation")):
        projected["text_excerpt"] = str(fact["text"])[:240]
    projected["source_sidecar"] = page_context.get(
        "secondary_sidecar" if source == "secondary" else "sidecar"
    )
    return projected


def _compact_window_context(window: Mapping[str, Any]) -> dict[str, Any]:
    """Retain every adjacent-page evidence path and exact source address."""
    result = {key: window[key] for key in (
        "pages", "kind", "status", "source_note", "visual_files",
        "source_note_ref",
        "sidecars", "reason", "task_id", "expected_next_page",
        "terminal_page", "continuation_evidence", "gap_evidence",
    ) if key in window}
    for field in ("left_candidates", "right_candidates"):
        rows = window.get(field)
        if isinstance(rows, list):
            result[field.removesuffix("_candidates") + "_candidate_refs"] = [
                f"{int(row.get('page', 0))}:{row.get('source', 'primary')}:{int(row.get('block', -1))}"
                for row in rows if isinstance(row, Mapping)
            ]
    return result


def _endnote_ambiguity_section(ambiguity_count: int) -> str:
    """Return the focused centralized-endnote review instructions."""
    return f"""
## Ambiguous centralized endnote definitions

The local endnote pass found {ambiguity_count} ambiguous centralized endnote
boundary item(s). This includes numbered lines and inline numeric superscripts
separated from note text by at least two spaces. Read `{AMBIGUITIES_FILENAME}`
and decide only those items; do not send every Notes line to review.

For each item, classify `line_text` with the surrounding context:
- `definition`: the `N.`/`N)`/`N：` marker starts a real endnote definition.
- `continuation`: the number is a page number, citation locator, or other printed
  number inside the preceding note/sentence, so this line belongs to the previous
  note and must not create a new definition.

For `kind: inline`, identity includes the exact original `line` and one-based
`column` of the superscript digits. Copy both `kind: "inline"` and `column`
exactly. `definition` starts a new endnote at that superscript; `continuation`
keeps the superscript and following text attached to the preceding note. Several
items may share one source line and each needs its own decision.

Use the previous/next lines and the scope key when present. A previous line ending
in `:` or `,` is the typical ambiguous case. Do not modify the body Markdown.
Write only valid JSON to `{DECISIONS_FILENAME}` with this shape:

```json
{{
  "schema_version": 2,
  "decisions": [
    {{
      "file": "chapter_10.part1.md",
      "line": 774,
      "kind": "line",
      "column": 1,
      "role": "continuation",
      "confidence": "high",
      "reason": "The number is a page locator; the sentence continues."
    }}
  ]
}}
```

Every manifest item needs exactly one decision. `review_required` is not an answer
here; the local apply stage fails closed until each item is `definition` or
`continuation`.
"""


def _focused_endnote_prompt(book_title: str, ambiguity_count: int) -> str:
    """Build a compact prompt used when only endnote boundaries need review."""
    return f"""# Centralized endnote definition review

Book: {book_title}

Page-footnote review is not applicable for this footnotes.mode. Only the
centralized endnote ambiguity review below is required; no page-footnote decision
file is needed. Do not reread or rewrite the whole book.
{_endnote_ambiguity_section(ambiguity_count)}"""


def _no_review_prompt(book_title: str, report: Mapping[str, Any]) -> str:
    """Build the minimal prompt used when no Subagent decision is required."""
    if report.get("footnote_mode") == "disabled":
        detail = (
            "Footnote and centralized endnote processing is disabled by "
            "footnotes.mode. No Subagent review or decision file is required."
        )
    else:
        detail = (
            "No page-footnote review or centralized endnote ambiguity review is "
            "required. No Subagent decision file is needed."
        )
    return f"# Footnote review\n\nBook: {book_title}\n\n{detail}\n"


def prepare_footnote_subagent(
    output_dir: Path,
    *,
    book_title: str,
    config: Optional[Mapping[str, Any]] = None,
    mode: Optional[str] = None,
    bottom_ratio: Optional[float] = None,
    bottom_intersection_ratio: Optional[float] = None,
    context_blocks: Optional[int] = None,
    auto_accept: Optional[bool] = None,
    native_max_font_ratio: Optional[float] = None,
) -> dict[str, Path]:
    """Write the compact Subagent hand-off for ambiguous layout windows."""
    output_dir = Path(output_dir)
    report = prepare_footnote_candidates(
        output_dir,
        config=config,
        mode=mode,
        bottom_ratio=bottom_ratio,
        bottom_intersection_ratio=bottom_intersection_ratio,
        context_blocks=context_blocks,
        auto_accept=auto_accept,
        native_max_font_ratio=native_max_font_ratio,
    )
    # Re-run the deterministic result audit on every prepare. Existing review
    # resolutions are reusable only for the exact task and source block hash.
    try:
        records = unit_records(output_dir)
    except (OSError, ValueError):
        # Older library-only tests and narrow preparation callers may not
        # provide a refined unit map. The normal page decision workflow remains
        # available; source-wide result auditing starts once scope exists.
        records = []
    source_texts = {
        str(record["name"]): (output_dir / "ocr_markdown" / str(record["name"]))
        .read_text(encoding="utf-8").replace("\r\n", "\n").replace("\r", "\n")
        for record in records
        if (output_dir / "ocr_markdown" / str(record["name"])).is_file()
    }
    prior_review_data = {}
    try:
        prior_review_data = json.loads((output_dir / "footnote_decisions.json").read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        pass
    prior_audit_reviews = prior_review_data.get("result_audit_reviews", []) if isinstance(prior_review_data, Mapping) else []
    audit = {
        "schema_version": 1,
        "status": "validated",
        "valid": True,
        "mode": str(report.get("footnote_mode") or "footnotes"),
        "source_sha256": markdown_inventory(output_dir / "ocr_markdown") if records else {},
        "target_sha256": None,
        "candidate_report_sha256": None,
        "decision_validation_sha256": None,
        "findings": [],
        "reviewed_candidate_tasks": [],
        "resolved": [],
        "suppressed": [],
    }
    # A failed apply may discover a target-only seam (for example body text
    # absorbed by a note). Carry that exact finding into the next prepare only
    # while its original source inventory is unchanged, so the existing visual
    # review path can resolve it instead of rediscovering forever.
    prior_result_audit_path = output_dir / "footnote_result_audit.json"
    try:
        prior_result_audit = json.loads(prior_result_audit_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        prior_result_audit = {}
    current_target_inventory = markdown_inventory(output_dir / "footnote_normalized")
    if (
        isinstance(prior_result_audit, Mapping)
        and prior_result_audit.get("source_sha256") == markdown_inventory(output_dir / "ocr_markdown")
        and isinstance(prior_result_audit.get("findings"), list)
    ):
        known = {str(item.get("task_id")) for item in audit.get("findings", []) if isinstance(item, Mapping)}
        for prior in prior_result_audit["findings"]:
            if isinstance(prior, Mapping) and prior.get("task_id") and str(prior["task_id"]) not in known:
                if (
                    str(prior.get("evidence_origin") or "") == "normalized_target"
                    and prior_result_audit.get("target_sha256") != current_target_inventory
                ):
                    continue
                carried = dict(prior)
                origin = str(carried.get("evidence_origin") or "")
                if origin == "source_markdown" or (not origin and str(carried.get("line") or "0").isdigit() and int(carried.get("line") or 0) > 0):
                    evidence_file = output_dir / "ocr_markdown" / str(carried.get("file") or "")
                    carried["evidence_origin"] = "source_markdown"
                    carried["evidence_file_sha256"] = sha256_file(evidence_file) if evidence_file.is_file() else None
                else:
                    carried["evidence_origin"] = "normalized_target"
                    carried["evidence_target_sha256"] = (prior_result_audit.get("target_sha256") or {}).get(str(carried.get("file") or ""))
                audit.setdefault("findings", []).append(carried)
                known.add(str(prior["task_id"]))
    # Pull in direct recall candidates found in generic OCR ``Text`` blocks.
    # These are already exact sidecar-addressable blocks and are always sent
    # for visual classification, never accepted from layout/text evidence.
    unit_by_page = {}
    for unit in records:
        for page in range(int(unit.get("start_page", 0)), int(unit.get("end_page", 0)) + 1):
            unit_by_page.setdefault(page, unit)
    try:
        exact_sidecars = page_sidecar_index(output_dir, report)
    except (OSError, ValueError) as exc:
        if (
            any(isinstance(item, Mapping) and item.get("code") == "suspected_numbered_note_definition" for item in audit.get("findings", []))
            or any(
                isinstance(candidate, Mapping) and candidate.get("leading_superscript_review") is True
                for page_row in report.get("pages", []) or []
                if isinstance(page_row, Mapping)
                for candidate in page_row.get("candidates", []) or []
            )
        ):
            raise ValueError(f"cannot resolve exact result-audit sidecar inventory: {exc}") from exc
        exact_sidecars = {}
    if (
        isinstance(prior_result_audit, Mapping)
        and prior_result_audit.get("source_sha256") == markdown_inventory(output_dir / "ocr_markdown")
        and prior_result_audit.get("target_sha256") == markdown_inventory(output_dir / "footnote_normalized")
    ):
        audit["reviewed_candidate_tasks"] = [
            dict(item) for item in prior_result_audit.get("admitted_candidates", [])
            if isinstance(item, Mapping)
        ]
        for saved in prior_result_audit.get("admitted_candidates", []) or []:
            if not isinstance(saved, Mapping) or not isinstance(saved.get("source_address"), Mapping):
                continue
            address = saved["source_address"]
            try:
                page = int(address["page"])
                source = str(address.get("source") or "primary")
                block_index = int(address["block"])
            except (KeyError, TypeError, ValueError):
                continue
            sidecar_entry = exact_sidecars.get(page, {}).get(source)
            if (
                not isinstance(sidecar_entry, Mapping)
                or str(sidecar_entry.get("path")) != str(address.get("sidecar"))
                or str(sidecar_entry.get("sha256")) != str(address.get("sidecar_sha256"))
            ):
                continue
            try:
                sidecar_data = json.loads((output_dir / str(sidecar_entry["path"])).read_text(encoding="utf-8"))
                block_data = sidecar_data["blocks"][block_index]
            except (OSError, UnicodeError, json.JSONDecodeError, KeyError, IndexError, TypeError):
                continue
            page_row = next((row for row in report.get("pages", []) or [] if isinstance(row, dict) and int(row.get("page", 0)) == page), None)
            if not isinstance(page_row, dict):
                continue
            key = str(saved.get("key") or "")
            block_text = str(block_data.get("text") or block_data.get("html") or "") if isinstance(block_data, Mapping) else ""
            candidate = {
                "page": page, "source": source, "block": block_index,
                "order": block_data.get("order", block_index) if isinstance(block_data, Mapping) else block_index,
                "label": str(block_data.get("label") or "Text") if isinstance(block_data, Mapping) else "Text",
                "bbox": block_data.get("bbox") if isinstance(block_data, Mapping) else None,
                "key": key, "confidence": "review", "disposition": "review_required",
                "text": block_text[:240], "result_audit_admitted": True,
                "review_reason": "preserved_exact_result_audit_admission",
            }
            field = "secondary_only_candidates" if source == "secondary" else "candidates"
            candidates = page_row.setdefault(field, [])
            if not any(isinstance(item, Mapping) and int(item.get("block", -1)) == block_index and str(item.get("source") or "primary") == source for item in candidates):
                candidates.append(candidate)

    # Truncated source/target definitions need exact owning-page evidence and
    # the following physical page when available. Reassociate by the complete
    # source definition signature, never by a chapter-wide page range.
    for audit_task in audit.get("findings", []):
        if not isinstance(audit_task, dict) or audit_task.get("code") != "normalized_note_truncated":
            continue
        if audit_task.get("source_address"):
            continue
        file = str(audit_task.get("file") or "")
        key = str(audit_task.get("key") or "")
        source_text = source_texts.get(file, "")
        definitions, _ = collect_footnote_definition_blocks(source_text.splitlines())
        body = next((body for _, (definition_key, body) in definitions.items() if str(definition_key) == key), "")
        signature = "".join(char.casefold() for char in body if char.isalnum())
        if not signature:
            audit_task["context_status"] = "truncated_definition_signature_unavailable"
            continue
        record = next((item for item in records if str(item.get("name") or "") == file), {})
        matches = []
        for page in range(int(record.get("start_page") or 0), int(record.get("end_page") or 0) + 1):
            for source in ("primary", "secondary"):
                sidecar_entry = exact_sidecars.get(page, {}).get(source)
                if not isinstance(sidecar_entry, Mapping):
                    continue
                try:
                    sidecar_data = json.loads((output_dir / str(sidecar_entry["path"])).read_text(encoding="utf-8"))
                except (OSError, UnicodeError, json.JSONDecodeError, KeyError, TypeError):
                    continue
                for block_index, block_data in enumerate(sidecar_data.get("blocks", [])):
                    if not isinstance(block_data, Mapping):
                        continue
                    block_text = str(block_data.get("text") or block_data.get("html") or "")
                    block_signature = "".join(char.casefold() for char in block_text if char.isalnum())
                    if block_signature and (
                        signature == block_signature
                        or (len(signature) >= 30 and len(block_signature) >= 30
                            and (signature in block_signature or block_signature in signature))
                    ):
                        matches.append((page, source, block_index, block_data, sidecar_entry))
        if len(matches) != 1:
            audit_task["context_status"] = "truncated_definition_page_mapping_ambiguous" if matches else "truncated_definition_page_mapping_unavailable"
            continue
        page, source, block_index, block_data, sidecar_entry = matches[0]
        audit_task["source_task_id"] = str(audit_task.get("task_id") or "")
        audit_task["source_evidence_sha256"] = str(audit_task.get("evidence_sha256") or "")
        block_digest = hashlib.sha256(json.dumps(
            {"sidecar_sha256": sidecar_entry["sha256"], "block": block_data},
            ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        ).encode("utf-8")).hexdigest()
        audit_task["task_id"] = "fra-" + hashlib.sha256(
            f"result-audit-truncated-sidecar\0{file}\0{key}\0{block_digest}".encode("utf-8")
        ).hexdigest()[:20]
        audit_task["evidence_origin"] = "source_sidecar"
        audit_task["evidence_sha256"] = block_digest
        audit_task["source_address"] = {
            "page": page, "source": source, "block": block_index,
            "sidecar": sidecar_entry["path"], "sidecar_sha256": sidecar_entry["sha256"],
        }
        audit_task["candidate_refs"] = [{"page": page, "source": source, "block": block_index}]
        audit_task["pages"] = [page]
        audit_task["context_status"] = "exact_truncated_definition_page_resolved"
    known_task_ids = {str(item.get("task_id")) for item in audit.get("findings", [])}
    for page_row in report.get("pages", []) or []:
        if not isinstance(page_row, Mapping):
            continue
        page = int(page_row.get("page", 0))
        for candidate in page_row.get("candidates", []) or []:
            if (
                not isinstance(candidate, Mapping)
                or candidate.get("leading_superscript_review") is not True
                or str(candidate.get("label") or "").strip().casefold() != "text"
            ):
                continue
            source = str(candidate.get("source") or "primary")
            block_index = int(candidate.get("block", -1))
            sidecar_entry = exact_sidecars.get(page, {}).get(source)
            if not isinstance(sidecar_entry, Mapping):
                raise ValueError(f"result-audit candidate has no exact {source} sidecar mapping for page {page}")
            sidecar_rel = str(sidecar_entry.get("path") or "")
            sidecar_path = output_dir / sidecar_rel
            try:
                sidecar_data = json.loads(sidecar_path.read_text(encoding="utf-8"))
                block_data = sidecar_data["blocks"][block_index]
            except (OSError, UnicodeError, json.JSONDecodeError, KeyError, IndexError, TypeError):
                continue
            block_digest = hashlib.sha256(json.dumps(
                {"sidecar_sha256": sidecar_entry.get("sha256"), "block": block_data},
                ensure_ascii=False, sort_keys=True, separators=(",", ":"),
            ).encode("utf-8")).hexdigest()
            unit = unit_by_page.get(page, {})
            unit_names = [str(name) for name in (unit.get("part_files") or [unit.get("name")]) if name]
            block_text = str(block_data.get("text") or block_data.get("html") or "")
            block_signature = "".join(char.casefold() for char in block_text if char.isalnum())
            exact_source_names = []
            if len(block_signature) >= 65:
                for name in unit_names:
                    text = source_texts.get(name, "")
                    source_signature = "".join(char.casefold() for char in text if char.isalnum())
                    if block_signature[:65] in source_signature:
                        exact_source_names.append(name)
            file = exact_source_names[0] if len(exact_source_names) == 1 else ""
            identity = f"leading_superscript:{file}:{page}:{source}:{block_index}:{block_digest}"
            task_id = "fra-" + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:20]
            if task_id in known_task_ids:
                continue
            audit.setdefault("findings", []).append({
                "task_id": task_id,
                "code": "leading_superscript_note_candidate",
                "file": file,
                "line": None,
                "page": page,
                "key": str(candidate.get("key") or ""),
                "evidence_sha256": block_digest,
                "evidence_excerpt": str(candidate.get("text") or "")[:280],
                "detail": "A generic OCR Text block begins with an explicit numeric superscript and needs visual role classification.",
                "requires_visual_review": True,
                "source_address": {"page": page, "source": source, "block": block_index, "sidecar": sidecar_rel, "sidecar_sha256": sidecar_entry.get("sha256")},
                "unit_scope": str(unit_by_page.get(page, {}).get("chapter_id") or file),
                "candidate_refs": [{"page": page, "source": source, "block": block_index}],
            })
            known_task_ids.add(task_id)

    # A failed apply can identify a numbered note-like block that the normal
    # geometric candidate rules did not admit (for example an OCR `Text`
    # block parsed as an ordered list). Re-admit only when a long source line
    # matches that exact generic sidecar block; the visual decision remains
    # required and the admitted candidate participates in the ordinary plan.
    for audit_task in audit.get("findings", []):
        if not isinstance(audit_task, Mapping) or audit_task.get("code") not in {
            "suspected_numbered_note_definition", "unexplained_numeric_superscript",
            "unexplained_unicode_superscript", "scoped_numbering_gap",
        }:
            continue
        file = str(audit_task.get("file") or "")
        source_text = source_texts.get(file, "")
        source_lines = source_text.splitlines()
        line_number = int(audit_task.get("line") or 0)
        source_line = source_lines[line_number - 1].strip() if 0 < line_number <= len(source_lines) else ""
        key = str(audit_task.get("key") or "")
        if not key:
            continue
        numbered_lines = []
        for candidate_line in source_lines:
            numbered = re.match(r"^\s*(?:[-*+]\s+)?(?P<key>\d{1,3})[.)](?:\\)?\s+", candidate_line)
            if numbered and numbered.group("key") == key:
                numbered_lines.append(candidate_line.strip())
        if audit_task.get("code") == "suspected_numbered_note_definition" and source_line:
            candidate_sources = [source_line]
        else:
            candidate_sources = numbered_lines
        if not candidate_sources:
            audit_task["context_status"] = "supplemental_numbered_source_line_unavailable"
            continue
        source_signatures = {
            "".join(char.casefold() for char in candidate_source if char.isalnum()): candidate_source
            for candidate_source in candidate_sources
        }
        record = next((item for item in records if str(item.get("name") or "") == file), {})
        if str(record.get("type") or "") in FOOTNOTE_STRUCTURE_EXEMPT_TYPES:
            audit_task["context_status"] = "structure_exempt_unit"
            continue
        matches = []
        for page in range(int(record.get("start_page") or 0), int(record.get("end_page") or 0) + 1):
            for source in ("primary", "secondary"):
                sidecar_entry = exact_sidecars.get(page, {}).get(source)
                if not isinstance(sidecar_entry, Mapping):
                    continue
                try:
                    sidecar_data = json.loads((output_dir / str(sidecar_entry["path"])).read_text(encoding="utf-8"))
                except (OSError, UnicodeError, json.JSONDecodeError, KeyError, TypeError):
                    continue
                for block_index, block_data in enumerate(sidecar_data.get("blocks", [])):
                    if not isinstance(block_data, Mapping) or str(block_data.get("label") or "").strip().casefold() != "text":
                        continue
                    block_text = str(block_data.get("text") or block_data.get("html") or "")
                    block_signature = "".join(char.casefold() for char in block_text if char.isalnum())
                    exact_match = block_signature in source_signatures
                    long_containment = any(
                        len(signature) >= 40 and len(block_signature) >= 40
                        and (signature in block_signature or block_signature in signature)
                        for signature in source_signatures
                    )
                    if not exact_match and not long_containment:
                        continue
                    source_match = next((candidate_source for signature, candidate_source in source_signatures.items() if exact_match and signature == block_signature), source_line)
                    matches.append((page, source, block_index, block_data, sidecar_entry, block_text, source_match))
        if len(matches) != 1:
            audit_task["context_status"] = "supplemental_source_signature_ambiguous" if matches else "supplemental_source_signature_unmatched"
            continue
        page, source, block_index, block_data, sidecar_entry, block_text, source_line = matches[0]
        block_digest = hashlib.sha256(json.dumps(
            {"sidecar_sha256": sidecar_entry["sha256"], "block": block_data},
            ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        ).encode("utf-8")).hexdigest()
        page_row = next((row for row in report.get("pages", []) or [] if isinstance(row, Mapping) and int(row.get("page", 0)) == page), None)
        if not isinstance(page_row, dict):
            continue
        candidate = {
            "page": page, "source": source, "block": block_index,
            "order": block_data.get("order", block_index), "label": "Text",
            "bbox": block_data.get("bbox"), "key": key, "confidence": "review",
            "disposition": "review_required", "text": block_text[:240],
            "result_audit_admitted": True,
            "review_reason": "exact_source_signature_match_to_unclassified_numbered_note_block",
        }
        candidate_field = "secondary_only_candidates" if source == "secondary" else "candidates"
        candidates = page_row.setdefault(candidate_field, [])
        if not any(isinstance(item, Mapping) and (int(item.get("block", -1)), str(item.get("source") or "primary")) == (block_index, source) for item in candidates):
            candidates.append(candidate)
        audit_task["key"] = key
        audit_task["task_id"] = "fra-" + hashlib.sha256(
            f"result-audit-sidecar\0{audit_task.get('code')}\0{file}\0{line_number}\0{block_digest}".encode("utf-8")
        ).hexdigest()[:20]
        audit_task["evidence_origin"] = "source_sidecar"
        audit_task["source_address"] = {
            "page": page, "source": source, "block": block_index,
            "sidecar": sidecar_entry["path"], "sidecar_sha256": sidecar_entry["sha256"],
        }
        audit_task["evidence_sha256"] = block_digest
        audit_task["evidence_excerpt"] = block_text[:280]
        audit_task["candidate_refs"] = [{"page": page, "source": source, "block": block_index}]
        audit_task["pages"] = [page]
        audit_task["context_status"] = "supplemental_candidate_admitted"
    audit["findings"].sort(key=lambda item: str(item.get("task_id")))
    prior_outcomes = {
        str(item.get("task_id")): str(item.get("outcome") or "")
        for item in prior_audit_reviews if isinstance(item, Mapping)
    } if isinstance(prior_audit_reviews, list) else {}
    for task in audit["findings"]:
        task["review_attempt"] = 2 if prior_outcomes.get(str(task.get("task_id"))) == "review_required" else 1
    audit["valid"] = not audit["findings"]
    audit["status"] = "review_required" if audit["findings"] else "validated"
    if audit.get("findings"):
        first_by_file = {str(record["name"]): record for record in records}
        for audit_task in audit["findings"]:
            record = first_by_file.get(str(audit_task.get("file")), {})
            refs = audit_task.get("candidate_refs")
            if not isinstance(refs, list) or not refs:
                neighbor_keys = audit_task.get("neighbor_keys")
                keys = {str(key) for key in neighbor_keys} if isinstance(neighbor_keys, list) else set()
                keys.update(str(audit_task.get("key") or "").split(","))
                keys.discard("")
                start, end = int(record.get("start_page") or 0), int(record.get("end_page") or 0)
                refs = []
                for page_row in report.get("pages", []) or []:
                    page_number = int(page_row.get("page", 0))
                    task_page = int(audit_task.get("page") or 0)
                    if task_page and page_number != task_page:
                        continue
                    if not task_page and not start <= page_number <= end:
                        continue
                    for candidate in page_row.get("candidates", []) or []:
                        if isinstance(candidate, Mapping) and str(candidate.get("key") or "") in keys:
                            refs.append({"page": page_number, "source": str(candidate.get("source") or "primary"), "block": int(candidate.get("block", -1))})
                audit_task["candidate_refs"] = refs
            pages_for_task = sorted({int(ref["page"]) for ref in refs if isinstance(ref, Mapping) and ref.get("page")})
            if not pages_for_task and audit_task.get("source_address", {}).get("page"):
                pages_for_task = [int(audit_task["source_address"]["page"])]
            if audit_task.get("code") == "normalized_note_truncated" and pages_for_task:
                owner_pages = list(pages_for_task)
                for owner_page in owner_pages:
                    if owner_page + 1 in exact_sidecars:
                        pages_for_task.append(owner_page + 1)
                pages_for_task = sorted(set(pages_for_task))
                audit_task["related_pages"] = pages_for_task
            if not pages_for_task:
                audit_task["context_status"] = "adjacent_note_page_mapping_unavailable"
                audit_task["context_error"] = "No exact sidecar candidate page was found for this result-audit task; review cannot proceed until the source-to-page linkage is restored."
            elif audit_task.get("context_status") != "supplemental_candidate_admitted":
                audit_task["context_status"] = "exact_adjacent_note_pages_resolved"
            audit_task["pages"] = pages_for_task
        pages = sorted({int(page) for task in audit["findings"] for page in task.get("pages", [])})
        image_evidence = ensure_visual_review_images(output_dir, set(pages), config) if pages else {"files": {}}
        image_files = image_evidence.get("files", {}) if isinstance(image_evidence, Mapping) else {}
        manifest_path = output_dir / "ocr_review_images.json"
        manifest_hash = sha256_file(manifest_path) if manifest_path.is_file() else None
        try:
            image_manifest_data = json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.is_file() else {}
        except (OSError, ValueError):
            image_manifest_data = {}
        for task in audit["findings"]:
            record = first_by_file.get(str(task.get("file")), {})
            task_pages = [int(page) for page in task.get("pages", [])]
            task["visual_files"] = {}
            for page in task_pages:
                image_path = image_files.get(str(page))
                image_sha = sha256_file(output_dir / str(image_path)) if image_path and (output_dir / str(image_path)).is_file() else None
                if image_path and image_sha:
                    task["visual_files"][str(page)] = {"path": str(image_path), "sha256": image_sha}
            task["visual_image_manifest_sha256"] = manifest_hash
            task["source_pdf_sha256"] = image_manifest_data.get("source_sha256") if isinstance(image_manifest_data, Mapping) else None
            task["unit_scope"] = str(record.get("chapter_id") or task.get("file"))
            if not task.get("candidate_refs"):
                page_row = next((item for item in report.get("pages", []) or [] if int(item.get("page", -1)) == page), {})
                refs = []
                for source_field, default_source in (("candidates", "primary"), ("audit_candidates", "primary"), ("secondary_only_candidates", "secondary")):
                    for candidate in page_row.get(source_field, []) or []:
                        if isinstance(candidate, Mapping) and str(candidate.get("key") or "") == str(task.get("key") or ""):
                            refs.append({"page": page, "source": str(candidate.get("source") or default_source), "block": int(candidate.get("block", -1))})
                task["candidate_refs"] = [ref for ref in refs if ref["block"] >= 0]
    report["result_audit"] = audit
    packet_path, packet_sha256, preparation_evidence_sha256 = write_review_packet(output_dir, report)
    unit_context_files = _write_unit_contexts(
        output_dir, report,
        review_packet=packet_path,
        review_packet_sha256=packet_sha256,
    )
    if report["footnote_mode"] == "disabled":
        ambiguities = {
            "schema_version": 1,
            "items": [],
            "source_sha256": {},
        }
    else:
        ambiguities = collect_endnote_definition_ambiguities(output_dir)
    ambiguity_path = write_endnote_definition_ambiguities(output_dir, ambiguities)
    ambiguity_items = ambiguities.get("items", [])
    ambiguity_count = len(ambiguity_items) if isinstance(ambiguity_items, list) else 0
    report["endnote_ambiguity_count"] = ambiguity_count
    report["endnote_definition_ambiguities_sha256"] = sha256_file(ambiguity_path)
    atomic_write_json(output_dir / "footnote_candidates.json", report)
    page_review_required = (
        (bool(report["review_required"]) or bool(report.get("continuation_reviews")) or bool(audit.get("findings")))
        and report["footnote_mode"] != "disabled"
    )
    manifest = {
        "schema_version": FOOTNOTE_PREPARE_SCHEMA_VERSION,
        "task": "footnote-prepare",
        "book_title": book_title,
        "source_kind": report["source_kind"],
        "ocr_evidence_mode": report["ocr_evidence_mode"],
        "footnote_mode": report["footnote_mode"],
        "configured_footnote_mode": report.get("configured_footnote_mode"),
        "footnote_mode_source": report.get("footnote_mode_source"),
        "warnings": report.get("warnings", []),
        "candidate_report": "footnote_candidates.json",
        "review_packet": packet_path,
        "review_packet_sha256": packet_sha256,
        "preparation_evidence_sha256": preparation_evidence_sha256,
        "review_pages": report["review_pages"],
        "high_confidence_candidate_count": report["high_confidence_candidate_count"],
        "review_candidate_count": report["review_candidate_count"],
        "soft_continuation_context_count": report[
            "soft_continuation_context_count"
        ],
        "soft_continuation_context_pages": report[
            "soft_continuation_context_pages"
        ],
        "bottom_detection": report["bottom_detection"],
        "suspected_missed_count": report["suspected_missed_count"],
        "suspected_missed_pages": report["suspected_missed_pages"],
        "structure_excluded_pages": report["structure_excluded_pages"],
        "visual_evidence": report["visual_evidence"],
        "visual_audit_manifest": report["visual_audit_manifest"],
        "visual_audit_task_count": report["visual_audit_task_count"],
        "auto_accept": report["auto_accept"],
        "bottom_intersection_ratio": report["bottom_intersection_ratio"],
        "native_max_font_ratio": report["native_max_font_ratio"],
        "native_font_profile": report.get("native_font_profile", {}),
        "cross_page_window_count": len(report["cross_page_windows"]),
        "continuation_reviews": report.get("continuation_reviews", []),
        "result_audit_tasks": audit.get("findings", []),
        "result_audit_version": audit.get("schema_version"),
        "result_audit_status": audit.get("status"),
        "consensus_visual_review_pages": report["consensus_visual_review_pages"],
        "unit_context_files": unit_context_files,
        "unit_context_sha256": {
            name: sha256_file(output_dir / relative)
            for name, relative in unit_context_files.items()
        },
        "unit_context_sha256": {
            name: sha256_file(output_dir / relative)
            for name, relative in unit_context_files.items()
        },
        "decision_file": (
            "footnote_decisions.json" if page_review_required else None
        ),
        "page_review_required": page_review_required,
        "endnote_ambiguity_count": ambiguity_count,
        "endnote_ambiguities": AMBIGUITIES_FILENAME,
        "endnote_decision_file": DECISIONS_FILENAME,
        "endnote_definition_file": AMBIGUITIES_FILENAME,
        "status": (
            "pending_review"
            if page_review_required or ambiguity_count
            else "no_subagent_review_required"
        ),
    }
    manifest_path = output_dir / "footnote_subagent_manifest.json"
    atomic_write_json(manifest_path, manifest)

    review_pages = report["review_pages"]
    page_reports = {
        int(item["page"]): item
        for item in report.get("pages", [])
        if isinstance(item, Mapping) and str(item.get("page", "")).isdigit()
    }
    page_lines = []
    for page in review_pages:
        page_report = page_reports.get(int(page), {})
        primary = f"`{page_report.get('sidecar', f'pages/page_{int(page):03d}.ocr.json')}`"
        secondary = page_report.get("secondary_sidecar")
        line = f"- primary: {primary}"
        if secondary:
            line += f"; secondary: `{secondary}`"
        if page_report.get("consensus_visual_review"):
            line += " (OCR consensus marked this page for visual review)"
        if page_report.get("consensus_status") == "disagree":
            line += " (primary/secondary footnote candidate evidence differs)"
        if page_report.get("visual_file"):
            line += f"; visual: `{page_report['visual_file']}`"
        region_lines = []
        for region in page_report.get("footnote_regions", []) or []:
            if not isinstance(region, Mapping) or region.get("disposition") != "visual_audit_required":
                continue
            region_line = str(region.get("region_id"))
            if region.get("region_visual_file"):
                region_line += f" crop=`{region['region_visual_file']}`"
            reasons = ", ".join(str(item) for item in region.get("reasons", []) or [])
            if reasons:
                region_line += f" ({reasons})"
            region_lines.append(region_line)
        if region_lines:
            line += "; audit regions: " + ", ".join(region_lines)
        page_lines.append(line)
    page_text = "\n".join(page_lines) or "- 无需 Subagent 复核；仅保留本地高置信度候选。"
    source_guidance = (
        (
            "For native-text pages, use the recorded coordinates and font metadata as layout evidence. "
            "Candidates marked confidence=high have already passed the conservative local rule: an earlier "
            "same-page superscript reference matches the key and the note text is materially smaller than "
            "the body font. Do not create a second decision for those blocks. Review only confidence=review "
            "blocks, and distinguish them from citations, page furniture, and ordinary numbered prose."
            if report["source_kind"] == "native_text"
            else "If a page is marked as an OCR-consensus visual review, treat the local label as untrusted even when it says `Footnote`; compare the primary and secondary sidecars and the page image before deciding."
        )
        if report["footnote_mode"] == "footnotes"
        else (
            "Page-footnote extraction is not part of this footnotes.mode. "
            "Review only page items explicitly listed here and do not classify "
            "ordinary Notes content as a page footnote."
        )
    )
    if ambiguity_count:
        source_guidance += (
            " Page-footnote review follows the page instructions; centralized "
            "endnote ambiguities use the final section and write "
            f"`{DECISIONS_FILENAME}`."
        )
    prompt = f"""# Footnote review\n\nBook: {book_title}\n\nRead `footnote_subagent_manifest.json`, `footnote_review_packet.json`, and `footnote_visual_audit_manifest.json`. The compact packet is hash-bound by the manifest. `footnote_candidates.json` remains the audit and recovery source; do not load it for routine review. Review exact page/source/block references and region IDs in the packet. Retain and inspect every listed adjacent-page window, primary/secondary sidecar, full-page image, and gap-review image even when no continuation candidate was emitted. Do not reread or rewrite the whole book. {source_guidance}\n\nCandidate page sidecars and visual evidence:\n{page_text}\n\nFor every listed review page, open the full-page `visual` PNG first. It is authoritative for the overall body/footnote boundary, page furniture, reading order, and cross-page context. When a `crop` PNG is present, open it as a zoomed supplement for small text and block-level details; never use the crop alone to decide whether a region belongs to the page footer or body. If the full-page image is unavailable, use the crop and sidecar coordinates.\n\nA region audit is one visual decision for the area, but the output must still contain one block decision for every listed review/audit block so the deterministic apply stage can address exact sidecar coordinates. Treat the page image as authoritative for region boundaries, continuations, headers, footers, and OCR omissions.\n\nUse these meanings strictly:\n- `footnote_start`: the beginning of a real page footnote; it will be moved to the end of its logical chapter.\n- `footnote_continuation`: text continuing a real footnote from an earlier page; it will be joined to that footnote.\n- `footnote_definition`: a complete footnote definition already presented as a note block.\n- `citation`: an in-text citation, quoted source, parenthetical/numeric reference, or other scholarly reference; it must stay in the body and must never be moved.\n- `bibliography`: a reference-list/bibliography entry; it must stay in place and must never be moved.\n- `body`: ordinary prose or an uncertain block that is not a footnote.\n\nWrite one `region_audits` entry for every audit region, with its exact `region_id`, a `disposition` of `footnote_region`, `body_region`, or `review_required`, and nested block decisions. A `review_required` region or block is unresolved and will block the workflow. Also keep a flat `decisions` array for compatibility; it may repeat the nested decisions, but do not emit duplicates. For each review/audit block assign exactly one role and keep its page, source, and block number. `source` defaults to `primary`; use `source: "secondary"` only for a candidate listed under `secondary_only_candidates`. For `footnote_start` and `footnote_definition`, copy the visible numeric key when present. For `footnote_continuation`, provide the key of the footnote it continues. A numeric marker alone is not enough to call something a footnote: if it is a citation or reference, use `citation` or `bibliography`.\n\nWrite only valid JSON to `footnote_decisions.json` with this shape:\n\n```json\n{{\n  "schema_version": {FOOTNOTE_PREPARE_SCHEMA_VERSION},\n  "region_audits": [\n    {{\n      "region_id": "page-125-region-01",\n      "disposition": "footnote_region",\n      "decisions": [\n        {{\n          "page": 125,\n          "source": "primary",\n          "block": 8,\n          "role": "footnote_continuation",\n          "key": "36",\n          "confidence": "high"\n        }}\n      ]\n    }}\n  ],\n  "decisions": []\n}}\n```\n\nIf a block or region is genuinely ambiguous, use `role` or `disposition: "review_required"` and explain it in `reason`; do not guess.\n"""
    prompt = prompt.replace(
        "Candidate page sidecars and visual evidence:",
        "When a `visual:` or `crop=` path is present, open that matching PNG and use it as the visual authority. "
        "Do not infer a footnote from the label alone. The report's "
        "`bottom_detection.suspected_missed_count` is a recall warning: inspect those pages' lower region "
        "even when no candidate was emitted.\n\nCandidate page sidecars and visual evidence:",
    )
    prompt += (
        "\n\nMarker identity contract: for moved definitions, copy the candidate's exact safe `key` and exact visible `marker_label`. "
        "`marker_label` preserves printed spelling and case (`*`, `**`, `†`, `‡`, `a`, `A`); `key` is internal ASCII identity. "
        "Never substitute one for the other. A bare `a)` may be a list item, and bare `*` may be multiplication or Markdown emphasis; "
        "classify only with explicit note syntax/label and page-image evidence. If multiple body markers could match, provide an exact short `marker_context`; apply refuses to guess. "
        "If the same chapter contains multiple identical non-numeric markers (for example several `*`, `**`, or `†`), every `footnote_start`/`footnote_definition` decision must include a precise `marker_context` with the surrounding text that locates its marker uniquely; otherwise `footnote-validate` fails immediately. "
        "Continuations use the start's internal `key`.\n\n"
    )
    prompt += (
        "\n\nFor every secondary-only moved decision, also include "
        "`primary_disposition`: `absent` when the Chandra/refinement text truly "
        "does not contain this note, or `remove` when it contains a duplicate "
        "primary block. For `remove`, include `primary_blocks` (or one integer "
        "`primary_block`) so the local stage can delete only the reviewed block. "
        "If the body contains more than one possible marker with this key, include "
        "`marker_context` with a short exact surrounding phrase; the local stage "
        "will refuse to guess between repeated markers.\n\n"
        "Do not reread or rewrite the whole book.\n"
    )
    if report.get("continuation_reviews"):
        continuation_task_lines = []
        for task in report.get("continuation_reviews", []) or []:
            if not isinstance(task, Mapping):
                continue
            source_note = task.get("source_note", {})
            image_paths = ", ".join(
                f"page {page}: `{image.get('path')}`"
                for page, image in (task.get("visual_files", {}) or {}).items()
                if isinstance(image, Mapping) and image.get("path")
            )
            continuation_task_lines.append(
                f"- `{task.get('task_id')}` source page {source_note.get('page')}, "
                f"key `{source_note.get('key')}`, ending `{source_note.get('text_ending')}`; "
                f"review pages {task.get('pages')}: {image_paths or 'required image unavailable'}"
            )
        prompt += (
            "\n\n## Missing continuation review tasks\n\n"
            "For each `continuation_reviews` task, open every listed full-page image. "
            "Review only the exact source note and every listed page. Add exactly one object "
            "per task to `continuation_reviews` in `footnote_decisions.json`, keyed by the exact "
            "`task_id`. Use `outcome: truncated_source_exception` only when the page image shows "
            "the source itself ends there or the hyphen is intentional, or `confirmed_not_a_footnote` "
            "when the image shows the candidate is ordinary body text. Explain the image evidence "
            "in `reason`. Use `outcome: recovered_continuation` only when the next page "
            "image visibly contains the missing continuation; include the exact visible text and "
            "the existing note `key`. Never infer text from context or OCR alone. Each record must "
            "include `evidence_pages` listing every task page. A task on the final physical PDF page "
            "can only be `truncated_source_exception` or `confirmed_not_a_footnote`. No ordinary block decision clears "
            "this task.\n\nTasks and exact image paths:\n"
            + "\n".join(continuation_task_lines)
            + "\n"
        )
    if report["footnote_mode"] == "disabled":
        prompt = _no_review_prompt(book_title, report)
        if report.get("warnings"):
            prompt += "\nConfigured mode warning: " + " ".join(
                str(item.get("message")) for item in report["warnings"]
                if isinstance(item, Mapping) and item.get("message")
            ) + "\n"
    elif page_review_required:
        if ambiguity_count:
            prompt += _endnote_ambiguity_section(ambiguity_count)
    elif ambiguity_count:
        prompt = _focused_endnote_prompt(book_title, ambiguity_count)
    else:
        prompt = _no_review_prompt(book_title, report)
    if audit.get("findings") and report["footnote_mode"] != "disabled":
        task_lines = []
        for task in audit["findings"]:
            refs = task.get("candidate_refs", [])
            ref_text = ", ".join(
                f"{ref['page']}:{ref['source']}:{ref['block']}" for ref in refs
            ) or "no existing page/block candidate; do not mark confirmed_footnote"
            images = ", ".join(
                f"page {page}: {item.get('path')}" for page, item in (task.get("visual_files") or {}).items()
            ) or "required page image unavailable"
            task_lines.append(
                f"- task_id={task['task_id']} evidence_sha256={task['evidence_sha256']} "
                f"file={task['file']} line={task['line']} scope={task.get('unit_scope')} "
                f"excerpt={task['evidence_excerpt']!r}; candidates=[{ref_text}]; images=[{images}]"
            )
        prompt += (
            "\n\n## Post-result audit findings\n\n"
            "For each exact source block below, compare its cited full-page image and sidecar context. "
            "Do not use words or numbering alone to decide footnote semantics. Return exactly one "
            "`result_audit_reviews` object per task with task_id, exact evidence_sha256, outcome, "
            "reason, and every evidence page. Allowed outcomes: `legitimate_non_footnote` for a "
            "citation/formula/ordinary body block; `truncated_source_exception` only when the image "
            "shows the source itself is intentionally incomplete; `confirmed_footnote` only when an "
            "exact listed candidate_ref is selected and the same page/block is also assigned a "
            "matching footnote role in the ordinary `decisions` array. Never invent a missing note or "
            "accept a confirmed note without that exact candidate decision. Preserve any existing "
            "nested Markdown footnote references in the source paragraph.\n\n"
            + "\n".join(task_lines)
            + "\n\nUse these objects in the existing `footnote_decisions.json`; keep its normal schema_version.\n"
        )
    prompt_path = output_dir / "footnote_subagent_prompt.md"
    atomic_write_text(prompt_path, prompt)
    return {
        "report": output_dir / "footnote_candidates.json",
        "manifest": manifest_path,
        "prompt": prompt_path,
        "review_packet": output_dir / packet_path,
    }


def load_footnote_unit_contexts(output_dir: Path) -> dict[str, Path]:
    """Load current sparse context projections for Markdown hand-offs."""
    output_dir = Path(output_dir)
    manifest_path = output_dir / "footnote_subagent_manifest.json"
    if not manifest_path.exists():
        return {}
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"footnote handoff manifest is unreadable; rerun footnote-prepare: {exc}") from exc
    if not isinstance(manifest, Mapping) or manifest.get("schema_version") != FOOTNOTE_PREPARE_SCHEMA_VERSION:
        raise ValueError(
            "footnote handoff schema is outdated or invalid; rerun footnote-prepare "
            "and footnote-validate to regenerate current unit contexts"
        )
    values = manifest.get("unit_context_files", {}) if isinstance(manifest, dict) else {}
    if not isinstance(values, Mapping):
        raise ValueError("footnote unit context inventory is invalid; rerun footnote-prepare")
    expected_context_hashes = manifest.get("unit_context_sha256")
    if not isinstance(expected_context_hashes, Mapping) or set(expected_context_hashes) != set(values):
        raise ValueError("footnote unit context hash inventory is missing or stale; rerun footnote-prepare")
    root = output_dir.resolve()
    for name, relative in values.items():
        if not isinstance(name, str) or not isinstance(relative, str):
            raise ValueError("footnote unit context inventory contains invalid names or paths; rerun footnote-prepare")
        expected_relative = _unit_context_path(output_dir, name).relative_to(output_dir).as_posix()
        if Path(relative).is_absolute() or Path(relative).as_posix() != expected_relative:
            raise ValueError(f"footnote unit context path is invalid for {name}; rerun footnote-prepare")
        try:
            (output_dir / relative).resolve().relative_to(root)
        except ValueError as exc:
            raise ValueError(f"footnote unit context path escapes output directory for {name}") from exc
    packet = validate_review_packet(output_dir, manifest)
    # The report is the independent page inventory. A worker context cannot
    # authenticate its own evidence paths by changing both a page and a fact.
    try:
        report = json.loads((output_dir / "footnote_candidates.json").read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"footnote candidate report is unreadable; rerun footnote-prepare: {exc}") from exc
    expected_contexts = _project_unit_contexts(
        report, packet, _load_refined_units(output_dir),
        page_sidecar_index(output_dir, report), manifest["review_packet"],
    )
    if set(values) != set(expected_contexts) or set(expected_context_hashes) != set(expected_contexts):
        raise ValueError("footnote unit context inventory differs from current refined mapping; rerun footnote-prepare and footnote-validate (existing decisions are retained)")
    result: dict[str, Path] = {}
    for name, relative in values.items():
        if not isinstance(name, str) or not isinstance(relative, str):
            raise ValueError("footnote unit context inventory contains invalid names or paths; rerun footnote-prepare")
        expected_relative = _unit_context_path(output_dir, name).relative_to(output_dir).as_posix()
        if Path(relative).is_absolute() or Path(relative).as_posix() != expected_relative:
            raise ValueError(f"footnote unit context path is invalid for {name}; rerun footnote-prepare")
        path = (output_dir / relative).resolve()
        try:
            path.relative_to(root)
        except ValueError as exc:
            raise ValueError(f"footnote unit context path escapes output directory for {name}") from exc
        if not path.is_file() or sha256_file(path) != expected_context_hashes.get(name):
            raise ValueError(f"footnote unit context {name} is missing or changed; rerun footnote-prepare")
        try:
            context = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ValueError(f"footnote unit context {name} is invalid; rerun footnote-prepare: {exc}") from exc
        if (
            not isinstance(context, Mapping)
            or context.get("schema_version") != FOOTNOTE_PREPARE_SCHEMA_VERSION
            or context.get("projection_version") != FOOTNOTE_UNIT_CONTEXT_PROJECTION_VERSION
            or context.get("source_units") != [name]
        ):
            raise ValueError(f"footnote unit context {name} has a stale projection contract or source scope; rerun footnote-prepare and footnote-validate (existing decisions are retained)")
        if context != expected_contexts.get(name):
            raise ValueError(f"footnote unit context {name} differs from canonical report/packet projection; rerun footnote-prepare and footnote-validate (existing decisions are retained)")
        result[name] = path
    return result

__all__ = [
    "load_footnote_unit_contexts",
    "prepare_footnote_subagent",
]
