"""Prepare the footnote review hand-off and sparse unit contexts.

Candidate detection remains in :mod:`footnote_prepare`; this module owns the
filesystem contract presented to the workspace Subagent.
"""

from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from typing import Any, Mapping, Optional

from ..workflow_contracts import atomic_write_json, atomic_write_text, sha256_file
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
        (bool(report["review_required"]) or bool(report.get("continuation_reviews")))
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
