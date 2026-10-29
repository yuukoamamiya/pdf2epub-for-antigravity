"""Prepare the footnote review hand-off and sparse unit contexts.

Candidate detection remains in :mod:`footnote_prepare`; this module owns the
filesystem contract presented to the workspace Subagent.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping, Optional

from ..workflow_contracts import atomic_write_json, atomic_write_text, sha256_file
from .endnote_review import (
    AMBIGUITIES_FILENAME,
    DECISIONS_FILENAME,
    collect_endnote_definition_ambiguities,
    write_endnote_definition_ambiguities,
)
from .footnote_contracts import FOOTNOTE_PREPARE_SCHEMA_VERSION
from .footnote_structure import load_refined_units as _load_refined_units
from .footnote_prepare import prepare_footnote_candidates

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


def _write_unit_contexts(
    output_dir: Path,
    report: Mapping[str, Any],
) -> dict[str, str]:
    """Write sparse per-unit projections so workers do not load the book report."""
    page_reports = report.get("pages", [])
    cross_windows = report.get("cross_page_windows", [])
    unit_context_files: dict[str, str] = {}
    for unit in _load_refined_units(output_dir):
        names = [str(name) for name in (unit.get("part_files") or [unit.get("file")]) if name]
        page_range = unit.get("page_range")
        if not isinstance(page_range, list) or len(page_range) != 2:
            continue
        try:
            start_page, end_page = int(page_range[0]), int(page_range[1])
        except (TypeError, ValueError):
            continue
        pages = [
            item for item in page_reports
            if start_page <= int(item.get("page", -1)) <= end_page
        ]
        windows = [
            item for item in cross_windows
            if any(start_page <= int(page) <= end_page for page in item.get("pages", []))
        ]
        context = {
            "schema_version": FOOTNOTE_PREPARE_SCHEMA_VERSION,
            "task": "footnote-prepare",
            "source_units": names,
            "page_range": [start_page, end_page],
            "pages": pages,
            "cross_page_windows": windows,
            "decisions": [
                item
                for item in report.get("decisions", [])
                if isinstance(item, Mapping)
                and start_page <= int(item.get("page", -1)) <= end_page
            ],
        }
        for name in names:
            context_path = _unit_context_path(output_dir, name)
            atomic_write_json(context_path, context)
            unit_context_files[name] = context_path.relative_to(output_dir).as_posix()
    return unit_context_files


def _endnote_ambiguity_section(ambiguity_count: int) -> str:
    """Return the focused centralized-endnote review instructions."""
    return f"""
## Ambiguous centralized endnote definitions

The local endnote pass found {ambiguity_count} numbered definition line(s) whose
preceding boundary is unclear. Read `{AMBIGUITIES_FILENAME}` and decide only those
items; do not send every Notes line to review.

For each item, classify `line_text` with the surrounding context:
- `definition`: the `N.`/`N)`/`N：` marker starts a real endnote definition.
- `continuation`: the number is a page number, citation locator, or other printed
  number inside the preceding note/sentence, so this line belongs to the previous
  note and must not create a new definition.

Use the previous/next lines and the scope key when present. A previous line ending
in `:` or `,` is the typical ambiguous case. Do not modify the body Markdown.
Write only valid JSON to `{DECISIONS_FILENAME}` with this shape:

```json
{{
  "schema_version": 1,
  "decisions": [
    {{
      "file": "chapter_10.part1.md",
      "line": 774,
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
    unit_context_files = _write_unit_contexts(output_dir, report)
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
        bool(report["review_required"]) and report["footnote_mode"] != "disabled"
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
        "candidate_report": "footnote_candidates.json",
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
        "consensus_visual_review_pages": report["consensus_visual_review_pages"],
        "unit_context_files": unit_context_files,
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
    prompt = f"""# Footnote review\n\nBook: {book_title}\n\nRead `footnote_subagent_manifest.json`, `footnote_candidates.json`, and `footnote_visual_audit_manifest.json`. The local script first inferred page-level bottom-note regions. Review only the regions listed as `visual_audit_required` and their blocks marked `confidence: review`; blocks marked `confidence: high` are already accepted by the deterministic local rule and do not need a decision. Do not reread or rewrite the whole book. {source_guidance}\n\nCandidate page sidecars and visual evidence:\n{page_text}\n\nFor every listed review page, open the full-page `visual` PNG first. It is authoritative for the overall body/footnote boundary, page furniture, reading order, and cross-page context. When a `crop` PNG is present, open it as a zoomed supplement for small text and block-level details; never use the crop alone to decide whether a region belongs to the page footer or body. If the full-page image is unavailable, use the crop and sidecar coordinates.\n\nA region audit is one visual decision for the area, but the output must still contain one block decision for every listed review/audit block so the deterministic apply stage can address exact sidecar coordinates. Treat the page image as authoritative for region boundaries, continuations, headers, footers, and OCR omissions.\n\nUse these meanings strictly:\n- `footnote_start`: the beginning of a real page footnote; it will be moved to the end of its logical chapter.\n- `footnote_continuation`: text continuing a real footnote from an earlier page; it will be joined to that footnote.\n- `footnote_definition`: a complete footnote definition already presented as a note block.\n- `citation`: an in-text citation, quoted source, parenthetical/numeric reference, or other scholarly reference; it must stay in the body and must never be moved.\n- `bibliography`: a reference-list/bibliography entry; it must stay in place and must never be moved.\n- `body`: ordinary prose or an uncertain block that is not a footnote.\n\nWrite one `region_audits` entry for every audit region, with its exact `region_id`, a `disposition` of `footnote_region`, `body_region`, or `review_required`, and nested block decisions. A `review_required` region or block is unresolved and will block the workflow. Also keep a flat `decisions` array for compatibility; it may repeat the nested decisions, but do not emit duplicates. For each review/audit block assign exactly one role and keep its page, source, and block number. `source` defaults to `primary`; use `source: "secondary"` only for a candidate listed under `secondary_only_candidates`. For `footnote_start` and `footnote_definition`, copy the visible numeric key when present. For `footnote_continuation`, provide the key of the footnote it continues. A numeric marker alone is not enough to call something a footnote: if it is a citation or reference, use `citation` or `bibliography`.\n\nWrite only valid JSON to `footnote_decisions.json` with this shape:\n\n```json\n{{\n  "schema_version": {FOOTNOTE_PREPARE_SCHEMA_VERSION},\n  "region_audits": [\n    {{\n      "region_id": "page-125-region-01",\n      "disposition": "footnote_region",\n      "decisions": [\n        {{\n          "page": 125,\n          "source": "primary",\n          "block": 8,\n          "role": "footnote_continuation",\n          "key": "36",\n          "confidence": "high"\n        }}\n      ]\n    }}\n  ],\n  "decisions": []\n}}\n```\n\nIf a block or region is genuinely ambiguous, use `role` or `disposition: "review_required"` and explain it in `reason`; do not guess.\n"""
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
    if report["footnote_mode"] == "disabled":
        prompt = _no_review_prompt(book_title, report)
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
    }


def load_footnote_unit_contexts(output_dir: Path) -> dict[str, Path]:
    """Load current sparse context projections for Markdown hand-offs."""
    output_dir = Path(output_dir)
    manifest_path = output_dir / "footnote_subagent_manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {}
    values = manifest.get("unit_context_files", {}) if isinstance(manifest, dict) else {}
    if not isinstance(values, Mapping):
        return {}
    result: dict[str, Path] = {}
    root = output_dir.resolve()
    for name, relative in values.items():
        if not isinstance(name, str) or not isinstance(relative, str):
            continue
        path = (output_dir / relative).resolve()
        try:
            path.relative_to(root)
        except ValueError:
            continue
        if path.is_file():
            result[name] = path
    return result

__all__ = [
    "load_footnote_unit_contexts",
    "prepare_footnote_subagent",
]
