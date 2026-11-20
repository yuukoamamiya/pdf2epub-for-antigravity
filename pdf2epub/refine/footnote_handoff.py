"""Prepare the footnote review hand-off and sparse unit contexts.

Candidate detection remains in :mod:`footnote_prepare`; this module owns the
filesystem contract presented to the workspace Subagent.
"""

from __future__ import annotations

import json
import hashlib
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Optional

from ..workflow_contracts import atomic_write_json, atomic_write_text, sha256_file
from ..subagent_runtime import resolve_subagent_model
from ..footnote_markdown import collect_footnote_definition_blocks
from .endnote_review import (
    AMBIGUITIES_FILENAME,
    DECISIONS_FILENAME,
    collect_endnote_definition_ambiguities,
    write_endnote_definition_ambiguities,
)
from .footnote_contracts import FOOTNOTE_PREPARE_SCHEMA_VERSION
from .footnote_prepare import prepare_footnote_candidates
from .footnote_review_packet import write_review_packet
from .footnote_page_evidence import page_sidecar_index
from .footnote_polish_review import polish_semantic_issue_tasks
from .footnote_source import unit_records, markdown_inventory
from .footnote_structure import FOOTNOTE_STRUCTURE_EXEMPT_TYPES
from .footnote_visual import ensure_visual_review_images
from .footnote_matching import block_text_variants
from .footnote_result_audit import AUDIT_VERSION, _task_id

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


def _dry_run_preflight_audit_findings(
    output_dir: Path,
    report: Mapping[str, Any],
    source_snapshot: Any,
    *,
    config: Optional[Mapping[str, Any]] = None,
) -> list[dict[str, Any]]:
    """Run an in-memory dry-run normalization + result audit on auto-accepted candidates.

    Only findings anchored to ``source_markdown`` (stable ``ocr_markdown/*.md``
    hashes) are surfaced so the first visual review wave can resolve residual
    source markers in one pass without waiting for a failed ``footnote-apply``.
    """
    if str(report.get("footnote_mode") or "") != "footnotes":
        return []
    auto_decisions: list[dict[str, Any]] = []
    for page_row in report.get("pages", []) or []:
        if not isinstance(page_row, Mapping):
            continue
        page_num = page_row.get("page")
        for cand in page_row.get("candidates", []) or []:
            if not isinstance(cand, Mapping) or cand.get("disposition") != "auto_accepted":
                continue
            role = str(cand.get("role") or cand.get("local_role") or "").strip()
            if not role:
                continue
            auto_decisions.append({
                "page": int(cand.get("page", page_num or 0)),
                "source": str(cand.get("source") or "primary"),
                "block": int(cand.get("block", -1)),
                "role": role,
                "key": cand.get("key"),
                "marker_label": cand.get("marker_label"),
            })
    if not auto_decisions:
        return []
    try:
        from .footnote_normalization_plan import (
            FootnoteNormalizationPlanningInput,
            build_footnote_normalization_plan,
        )
        from .footnote_result_audit import audit_footnote_result

        planning = build_footnote_normalization_plan(FootnoteNormalizationPlanningInput(
            output_dir=output_dir,
            report=report,
            decisions={"decisions": auto_decisions, "continuation_reviews": []},
            source=source_snapshot,
            footnote_mode="footnotes",
        ))
        if not planning.valid or planning.edit_plan is None:
            return []
        edit_plan = planning.edit_plan
        records = list(source_snapshot.records)
        final_file_by_chapter: dict[str, dict[str, Any]] = {}
        for rec in records:
            ch = str(rec.get("chapter_id") or "")
            cur = final_file_by_chapter.get(ch)
            if cur is None or (rec.get("unit_order", 0), rec.get("part_index", 0)) >= (
                cur.get("unit_order", 0), cur.get("part_index", 0)
            ):
                final_file_by_chapter[ch] = rec
        expected_notes = [
            {
                "chapter_id": ch,
                "target_file": str(
                    edit_plan.note_target_files.get((ch, str(note.get("output_key") or note.get("key") or "")))
                    or final_file_by_chapter.get(ch, {}).get("name")
                    or ""
                ),
                "output_key": str(note.get("output_key") or note.get("key") or ""),
                "parts": list(note.get("parts") or []),
            }
            for ch, notes in edit_plan.notes_by_chapter.items()
            for note in notes
        ]
        dry_audit = audit_footnote_result(
            source_snapshot.source_texts,
            edit_plan.transformed_texts,
            records,
            expected_notes=expected_notes,
            resolved_blocks=list(edit_plan.resolved_blocks),
            footnote_mode="footnotes",
        )
        surfaced: list[dict[str, Any]] = []
        for finding in dry_audit.get("findings", []) or []:
            if not isinstance(finding, Mapping) or not finding.get("task_id"):
                continue
            if str(finding.get("evidence_origin") or "") != "source_markdown":
                continue
            item = dict(finding)
            evidence_file = output_dir / "ocr_markdown" / str(item.get("file") or "")
            item["evidence_file_sha256"] = sha256_file(evidence_file) if evidence_file.is_file() else None
            item["preflight_dry_run"] = True
            surfaced.append(item)
        return surfaced
    except Exception:
        return []


def _render_review_prompt(
    book_title: str,
    report: Mapping[str, Any],
    *,
    ambiguity_count: int,
    page_review_required: bool,
    audit: Mapping[str, Any],
    recovery_required: bool,
    polish_semantic_blockers: list[Any],
    model: str,
) -> str:
    """Render the footnote review prompt for the current prepared state."""
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
        if any(task.get("code") == "polish_semantic_issue" for task in audit.get("findings", [])):
            prompt += (
                "\n\nFor a `polish_semantic_issue`, compare the exact source excerpt, OCR sidecar, and cited page image. "
                "If one OCR block mixes body and footnote text, replace that block's whole-block decision with `role: \"body\", override: true` and a `segments` array. "
                "Each segment must contain its exact OCR `text` and a `role`; moved segments also need their exact `key` and visible `marker_label` when applicable. "
                "List segments in physical reading order. The apply stage moves only exact footnote segments and leaves body text at its source position; it rejects duplicate, overlapping, stale, or non-unique excerpts. "
                "Do not use the polished target excerpt to infer source ownership.\n"
            )
    if recovery_required:
        recovery_rows = []
        for blocker in polish_semantic_blockers:
            if not isinstance(blocker, Mapping):
                continue
            recovery_rows.append(
                f"- file `{blocker.get('file')}`, issue `{blocker.get('issue_id') or 'unknown'}`, "
                f"kind `{blocker.get('kind')}`, stage `{blocker.get('mapping_stage') or 'review validation'}`: "
                f"{blocker.get('message')}. Recovery: {blocker.get('recovery')}"
            )
        prompt += (
            "\n\n## Polish semantic recovery required\n\n"
            "This handoff contains unresolved Polish semantic review blockers. Do not invent a page/block address, "
            "do not add a footnote decision for a blocker, and do not treat it as a normal visual candidate. "
            "Have the original Polish Reviewer update the existing semantic sidecar, preserving the unresolved issue "
            "until its exact excerpt maps; then rerun `footnote-prepare`. Other listed exact visual tasks remain independently actionable.\n\n"
            + "\n".join(recovery_rows)
            + "\n"
        )
    prompt += f"\nRecommended model: `{model}`.\n"
    return prompt


@dataclass(frozen=True)
class _ManifestInputs:
    """Inputs for the prepared footnote manifest."""

    output_dir: Path
    report: Mapping[str, Any]
    audit: Mapping[str, Any]
    model: str
    book_title: str
    packet_path: str
    packet_sha256: str
    preparation_evidence_sha256: str
    unit_context_files: Mapping[str, str]
    ambiguity_count: int
    page_review_required: bool
    polish_semantic_blockers: list[Any]
    recovery_required: bool


def _build_prepare_manifest(inputs: _ManifestInputs) -> dict[str, Any]:
    """Build the prepared footnote manifest without file I/O."""
    output_dir = inputs.output_dir
    report = inputs.report
    audit = inputs.audit
    model = inputs.model
    book_title = inputs.book_title
    packet_path = inputs.packet_path
    packet_sha256 = inputs.packet_sha256
    preparation_evidence_sha256 = inputs.preparation_evidence_sha256
    unit_context_files = inputs.unit_context_files
    ambiguity_count = inputs.ambiguity_count
    page_review_required = inputs.page_review_required
    polish_semantic_blockers = inputs.polish_semantic_blockers
    recovery_required = inputs.recovery_required
    manifest = {
        "schema_version": FOOTNOTE_PREPARE_SCHEMA_VERSION,
        "task": "footnote-prepare",
        "model": model,
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
        "polish_semantic_diagnostics": audit.get("polish_semantic_diagnostics", []),
        "polish_semantic_blockers": polish_semantic_blockers,
        "recovery_required": recovery_required,
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
            "recovery_required" if recovery_required
            else "pending_review"
            if page_review_required or ambiguity_count
            else "no_subagent_review_required"
        ),
    }
    return manifest


def _carry_prior_audit_findings(
    prior_result_audit: Mapping[str, Any],
    audit: dict[str, Any],
    *,
    output_dir: Path,
    current_target_inventory: Mapping[str, Any],
    records: list[Mapping[str, Any]],
    report: Mapping[str, Any],
    config: Optional[Mapping[str, Any]],
) -> None:
    """Carry exact findings from the previous audit while their evidence is unchanged."""
    if (
        isinstance(prior_result_audit, Mapping)
        and prior_result_audit.get("source_sha256") == markdown_inventory(output_dir / "ocr_markdown")
        and isinstance(prior_result_audit.get("findings"), list)
    ):
        known = {str(item.get("task_id")) for item in audit.get("findings", []) if isinstance(item, Mapping)}
        for prior in prior_result_audit["findings"]:
            if isinstance(prior, Mapping) and prior.get("task_id") and str(prior["task_id"]) not in known:
                origin = str(prior.get("evidence_origin") or "")
                if origin not in {"source_markdown", "normalized_target"}:
                    # Sidecar-addressed findings are rebuilt from the current
                    # page inventory below; carrying the old task would retain
                    # stale addresses and visual evidence.
                    continue
                if (
                    origin == "normalized_target"
                    and prior_result_audit.get("target_sha256") != current_target_inventory
                ):
                    continue
                carried = dict(prior)
                if origin == "source_markdown":
                    evidence_file = output_dir / "ocr_markdown" / str(carried.get("file") or "")
                    carried["evidence_origin"] = "source_markdown"
                    carried["evidence_file_sha256"] = sha256_file(evidence_file) if evidence_file.is_file() else None
                else:
                    carried["evidence_origin"] = "normalized_target"
                    carried["evidence_target_sha256"] = (prior_result_audit.get("target_sha256") or {}).get(str(carried.get("file") or ""))
                audit.setdefault("findings", []).append(carried)
                known.add(str(prior["task_id"]))
        prior_applied = prior_result_audit.get("applied_result_audit_reviews")
        prior_applied_tasks = prior_result_audit.get("applied_result_audit_review_tasks")
        audit["_prior_applied_reviews"] = prior_applied
        audit["_prior_applied_review_tasks"] = prior_applied_tasks
    elif not current_target_inventory and records and str(report.get("footnote_mode") or "") == "footnotes":
        known = {str(item.get("task_id")) for item in audit.get("findings", []) if isinstance(item, Mapping)}
        try:
            from .footnote_source import load_footnote_source_snapshot
            preflight_snapshot = load_footnote_source_snapshot(output_dir)
        except Exception:
            preflight_snapshot = None
        if preflight_snapshot is not None:
            for finding in _dry_run_preflight_audit_findings(
                output_dir, report, preflight_snapshot, config=config,
            ):
                tid = str(finding.get("task_id") or "")
                if tid and tid not in known:
                    audit.setdefault("findings", []).append(finding)
                    known.add(tid)


def _attach_polish_semantic_tasks(
    audit: dict[str, Any],
    report: dict[str, Any],
    *,
    output_dir: Path,
    records: list[Mapping[str, Any]],
    source_texts: Mapping[str, str],
    exact_sidecars: Mapping[Any, Any],
) -> None:
    """Attach polish semantic review tasks and their exact page blocks."""
    if records:
        polish_tasks, polish_diagnostics = polish_semantic_issue_tasks(
            output_dir, records, source_texts, exact_sidecars, report,
        )
        audit["findings"].extend(polish_tasks)
        audit["polish_semantic_diagnostics"] = polish_diagnostics
        stale_semantic_files = sorted({
            str(item.get("file")) for item in polish_diagnostics
            if isinstance(item, Mapping) and item.get("kind") == "stale_contract"
        })
        mapping_blockers = [
            item for item in polish_diagnostics
            if isinstance(item, Mapping) and item.get("kind") in {
                "current_issue_mapping_failure", "invalid_review",
            }
        ]
        audit["polish_semantic_stale_files"] = stale_semantic_files
        audit["polish_semantic_blockers"] = mapping_blockers
        if stale_semantic_files:
            audit.setdefault("warnings", []).append(
                "Polish semantic contracts are stale for " + ", ".join(stale_semantic_files)
                + "; rerun polish --resume, then footnote-prepare."
            )
        if mapping_blockers:
            audit.setdefault("warnings", []).append(
                "Current unresolved Polish semantic issues could not be mapped to exact primary OCR blocks; "
                "the result audit is blocked. Have the original Polish Reviewer revise the listed source_excerpt "
                "in its existing semantic sidecar, then rerun footnote-prepare."
            )
        for task in polish_tasks:
            address = task.get("source_address", {})
            page = int(address.get("page", 0))
            page_row = next((row for row in report.get("pages", []) or [] if int(row.get("page", -1)) == page), None)
            if not isinstance(page_row, dict):
                continue
            try:
                sidecar_data = json.loads((output_dir / str(address["sidecar"])).read_text(encoding="utf-8"))
                block = sidecar_data["blocks"][int(address["block"])]
            except (OSError, UnicodeError, json.JSONDecodeError, KeyError, IndexError, TypeError):
                continue
            candidate = {
                "page": page,
                "source": str(address.get("source") or "primary"),
                "block": int(address.get("block", -1)),
                "order": block.get("order", int(address.get("block", -1))),
                "label": str(block.get("label") or "Text"),
                "bbox": block.get("bbox"),
                "key": "",
                "confidence": "review",
                "disposition": "review_required",
                "text": (block_text_variants(output_dir, page, block) or [str(task.get("evidence_excerpt") or "")])[0],
                "result_audit_admitted": True,
                "review_reason": "polish_semantic_issue_requires_visual_ownership_review",
            }
            field = "secondary_only_candidates" if candidate["source"] == "secondary" else "candidates"
            rows = page_row.setdefault(field, [])
            if not any(isinstance(row, Mapping) and int(row.get("block", -1)) == candidate["block"] and str(row.get("source") or "primary") == candidate["source"] for row in rows):
                rows.append(candidate)


def _restore_admitted_candidates(
    audit: dict[str, Any],
    report: dict[str, Any],
    *,
    output_dir: Path,
    prior_result_audit: Mapping[str, Any],
    exact_sidecars: Mapping[Any, Any],
    prior_review_data: Mapping[str, Any],
) -> None:
    """Re-admit candidates that a previous run approved and whose sidecar is unchanged."""
    if (
        isinstance(prior_result_audit, Mapping)
        and prior_result_audit.get("source_sha256") == markdown_inventory(output_dir / "ocr_markdown")
        and prior_result_audit.get("target_sha256") == markdown_inventory(output_dir / "footnote_normalized")
    ):
        audit["reviewed_candidate_tasks"] = [
            dict(item) for item in prior_result_audit.get("admitted_candidates", [])
            if isinstance(item, Mapping) and not item.get("preserved_candidate_only")
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
                page_row = {"page": page, "candidates": [], "secondary_only_candidates": []}
                report.setdefault("pages", []).append(page_row)
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
        decided_addresses = {
            (int(d["page"]), str(d.get("source") or "primary"), int(d["block"]))
            for d in (prior_review_data.get("decisions") or [])
            if isinstance(d, Mapping)
            and str(d.get("page", "")).lstrip("-").isdigit()
            and str(d.get("block", "")).lstrip("-").isdigit()
        } if isinstance(prior_review_data, Mapping) else set()
        if decided_addresses:
            try:
                prior_candidate_report = json.loads((output_dir / "footnote_candidates.json").read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError):
                prior_candidate_report = {}
            for prior_page in prior_candidate_report.get("pages", []) or [] if isinstance(prior_candidate_report, Mapping) else []:
                if not isinstance(prior_page, Mapping):
                    continue
                for prior_field in ("candidates", "secondary_only_candidates"):
                    for prior_cand in prior_page.get(prior_field, []) or []:
                        if not isinstance(prior_cand, Mapping) or not prior_cand.get("result_audit_admitted"):
                            continue
                        try:
                            p_page = int(prior_cand["page"])
                            p_source = str(prior_cand.get("source") or "primary")
                            p_block = int(prior_cand["block"])
                        except (KeyError, TypeError, ValueError):
                            continue
                        if (p_page, p_source, p_block) not in decided_addresses:
                            continue
                        if not isinstance(exact_sidecars.get(p_page, {}).get(p_source), Mapping):
                            continue
                        p_row = next((row for row in report.get("pages", []) or [] if isinstance(row, dict) and int(row.get("page", 0)) == p_page), None)
                        if not isinstance(p_row, dict):
                            p_row = {"page": p_page, "candidates": [], "secondary_only_candidates": []}
                            report.setdefault("pages", []).append(p_row)
                        target_field = "secondary_only_candidates" if p_source == "secondary" else "candidates"
                        target_list = p_row.setdefault(target_field, [])
                        if not any(isinstance(item, Mapping) and int(item.get("block", -1)) == p_block and str(item.get("source") or "primary") == p_source for item in target_list):
                            target_list.append(dict(prior_cand))



def _resolve_truncated_definitions(
    audit: dict[str, Any],
    *,
    output_dir: Path,
    records: list[Mapping[str, Any]],
    source_texts: Mapping[str, str],
    exact_sidecars: Mapping[Any, Any],
) -> None:
    """Bind truncated-definition tasks to their exact sidecar block."""
    for audit_task in audit.get("findings", []):
        if not isinstance(audit_task, dict) or audit_task.get("code") not in {
            "normalized_note_truncated",
            "normalized_note_open_boundary_at_physical_page_break",
        }:
            continue
        if audit_task.get("source_address"):
            continue
        file = str(audit_task.get("file") or "")
        key = str(audit_task.get("key") or "")
        source_text = source_texts.get(file, "")
        evidence_text = source_text
        if audit_task.get("evidence_origin") == "normalized_target":
            normalized_path = output_dir / "footnote_normalized" / file
            if normalized_path.is_file():
                evidence_text = normalized_path.read_text(encoding="utf-8")
        definitions, _ = collect_footnote_definition_blocks(evidence_text.splitlines())
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
        audit_task["pages"] = sorted({page, *(int(value) for value in audit_task.get("pages", []) if str(value).isdigit())})
        audit_task["context_status"] = "exact_truncated_definition_page_resolved"


def _admit_leading_superscripts(
    audit: dict[str, Any],
    report: Mapping[str, Any],
    *,
    output_dir: Path,
    exact_sidecars: Mapping[Any, Any],
    unit_by_page: Mapping[int, Mapping[str, Any]],
    source_texts: Mapping[str, str],
    known_task_ids: set[str],
) -> None:
    """Admit generic Text blocks that begin with an explicit numeric superscript."""
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



def _add_supplemental_note_tasks(
    audit: dict[str, Any],
    *,
    source_texts: Mapping[str, str],
) -> None:
    """Add separately scoped definition tasks for same-key numbered source lines."""
    # A residual body marker and a short same-key numbered definition are
    # separate evidence. Preserve the exact marker task, while creating a
    # separately scoped definition task so the numbered candidate can be
    # classified from its own OCR block (for example a short "17. Ebd.").
    existing_note_tasks = {
        (str(item.get("file") or ""), str(item.get("key") or ""))
        for item in audit.get("findings", []) if isinstance(item, Mapping)
        and item.get("code") == "suspected_numbered_note_definition"
    }
    supplemental_note_tasks: list[dict[str, Any]] = []
    for marker_task in list(audit.get("findings", [])):
        if not isinstance(marker_task, Mapping) or marker_task.get("code") != "unexplained_numeric_superscript":
            continue
        file = str(marker_task.get("file") or "")
        key = str(marker_task.get("key") or "")
        if not file or not key or (file, key) in existing_note_tasks:
            continue
        source_lines = source_texts.get(file, "").splitlines()
        for line_number, source_line in enumerate(source_lines, 1):
            numbered = re.match(r"^\s*(?:[-*+]\s+)?(?P<key>\d{1,3})[.)](?:\\)?\s+", source_line)
            if not numbered or numbered.group("key") != key:
                continue
            excerpt = source_line.strip()
            evidence_hash = hashlib.sha256(excerpt.encode("utf-8")).hexdigest()
            identity = "supplemental-numbered-definition=" + evidence_hash
            supplemental_note_tasks.append({
                **dict(marker_task),
                "task_id": _task_id("suspected_numbered_note_definition", file, line_number, evidence_hash, identity),
                "code": "suspected_numbered_note_definition",
                "line": line_number,
                "evidence_sha256": evidence_hash,
                "evidence_excerpt": excerpt,
                "evidence_origin": "normalized_target",
                "detail": f"Numbered block {key} remains a possible unnormalized note definition.",
                "candidate_refs": [],
                "pages": [],
                "source_address": None,
            })
    audit.setdefault("findings", []).extend(supplemental_note_tasks)



def _bind_supplemental_candidates(
    audit: dict[str, Any],
    report: dict[str, Any],
    *,
    output_dir: Path,
    records: list[Mapping[str, Any]],
    source_texts: Mapping[str, str],
    exact_sidecars: Mapping[Any, Any],
) -> None:
    """Re-admit numbered notes only when a long source line matches one exact block."""
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
        if (
            audit_task.get("evidence_origin") == "normalized_target"
            and audit_task.get("code") in {
            "suspected_numbered_note_definition",
            "unexplained_numeric_superscript",
            "unexplained_unicode_superscript",
            }
            and source_line
        ):
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
        # Preserve the original semantic finding identity alongside the
        # sidecar address used for visual-review freshness. The resulting
        # exception must match both this exact finding and this exact block.
        audit_task["source_task_id"] = str(audit_task.get("task_id") or "")
        audit_task["source_evidence_sha256"] = str(audit_task.get("evidence_sha256") or "")
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


def _bind_heading_tasks(
    audit: dict[str, Any],
    *,
    output_dir: Path,
    exact_sidecars: Mapping[Any, Any],
    selected_page_dir: Path,
) -> None:
    """Bind heading-in-note tasks to their exact sidecar and page evidence."""
    from .footnote_source import project_footnote_source_text

    # A TOC-title-in-note result task already carries the exact source block
    # selected by the edit plan. Bind that task to the current page sidecar so
    # its visual exception can use the ordinary validation/apply gate.
    for audit_task in audit.get("findings", []):
        if not isinstance(audit_task, dict) or audit_task.get("code") not in {
            "confirmed_toc_heading_in_note", "suspected_heading_in_note",
        }:
            continue
        refs = audit_task.get("candidate_refs")
        if not isinstance(refs, list) or len(refs) != 1 or not isinstance(refs[0], Mapping):
            audit_task["context_status"] = "heading_source_address_unavailable"
            continue
        ref = refs[0]
        try:
            page, source, block_index = int(ref["page"]), str(ref.get("source") or "primary"), int(ref["block"])
        except (KeyError, TypeError, ValueError):
            audit_task["context_status"] = "heading_source_address_invalid"
            continue
        sidecar_entry = exact_sidecars.get(page, {}).get(source)
        if not isinstance(sidecar_entry, Mapping):
            audit_task["context_status"] = "heading_sidecar_unavailable"
            continue
        try:
            sidecar_data = json.loads((output_dir / str(sidecar_entry["path"])).read_text(encoding="utf-8"))
            block_data = sidecar_data["blocks"][block_index]
        except (OSError, UnicodeError, json.JSONDecodeError, KeyError, IndexError, TypeError):
            audit_task["context_status"] = "heading_sidecar_block_unavailable"
            continue
        block_digest = hashlib.sha256(json.dumps(
            {"sidecar_sha256": sidecar_entry["sha256"], "block": block_data},
            ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        ).encode("utf-8")).hexdigest()
        source_file = str(audit_task.get("source_file") or "")
        source_path = output_dir / "ocr_markdown" / source_file if source_file else None
        if not source_file or Path(source_file).name != source_file or source_path is None or not source_path.is_file():
            audit_task["context_status"] = "heading_source_markdown_unavailable"
            continue
        audit_task["source_task_id"] = str(audit_task.get("task_id") or "")
        audit_task["source_evidence_sha256"] = str(audit_task.get("evidence_sha256") or "")
        audit_task["evidence_origin"] = "source_sidecar"
        audit_task["evidence_sha256"] = block_digest
        audit_task["source_address"] = {
            "page": page, "source": source, "block": block_index,
            "sidecar": str(sidecar_entry["path"]),
            "sidecar_sha256": str(sidecar_entry["sha256"]),
        }
        audit_task["evidence_source_file"] = source_file
        audit_task["evidence_file_sha256"] = sha256_file(source_path)
        page_markdown = selected_page_dir / f"page_{page:03d}.md"
        if page_markdown.is_file():
            audit_task["evidence_page_file"] = page_markdown.resolve().relative_to(output_dir.resolve()).as_posix()
            audit_task["evidence_page_sha256"] = sha256_file(page_markdown)
        span = audit_task.get("source_span")
        try:
            start, end = int(span[0]), int(span[1])
            source_bytes = source_path.read_bytes()
            projected_source = project_footnote_source_text(source_bytes)
            audit_task["evidence_source_span_sha256"] = hashlib.sha256(
                projected_source[start:end].encode("utf-8")
            ).hexdigest()
        except (TypeError, ValueError, IndexError, OSError, UnicodeError):
            audit_task["context_status"] = "heading_source_span_unverifiable"
            audit_task.pop("evidence_source_span_sha256", None)
            continue
        audit_task["context_status"] = "exact_heading_source_page_resolved"


def _link_marker_candidates(
    audit: dict[str, Any],
) -> None:
    """Link same-scope marker and definition tasks without moving the body block."""
    note_candidates_by_scope_key: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for audit_task in audit.get("findings", []):
        if not isinstance(audit_task, Mapping) or audit_task.get("code") != "suspected_numbered_note_definition":
            continue
        scope_key = (str(audit_task.get("unit_scope") or audit_task.get("file") or ""), str(audit_task.get("key") or ""))
        note_candidates_by_scope_key.setdefault(scope_key, []).extend(
            dict(ref) for ref in audit_task.get("candidate_refs", []) if isinstance(ref, Mapping)
        )
    for audit_task in audit.get("findings", []):
        if not isinstance(audit_task, dict) or audit_task.get("code") not in {
            "unexplained_numeric_superscript", "unexplained_unicode_superscript",
        }:
            continue
        scope_key = (str(audit_task.get("unit_scope") or audit_task.get("file") or ""), str(audit_task.get("key") or ""))
        linked = note_candidates_by_scope_key.get(scope_key, [])
        refs = audit_task.setdefault("candidate_refs", [])
        known = {
            (int(ref.get("page", -1)), str(ref.get("source") or "primary"), int(ref.get("block", -1)))
            for ref in refs if isinstance(ref, Mapping)
        }
        for ref in linked:
            address = (int(ref.get("page", -1)), str(ref.get("source") or "primary"), int(ref.get("block", -1)))
            if address not in known:
                refs.append(ref)
                known.add(address)


def _annotate_review_attempts(
    audit: dict[str, Any],
    prior_audit_reviews: Any,
) -> None:
    """Apply the review-attempt counter and recompute the audit status."""
    audit["findings"].sort(key=lambda item: str(item.get("task_id")))
    prior_outcomes = {
        str(item.get("task_id")): str(item.get("outcome") or "")
        for item in prior_audit_reviews if isinstance(item, Mapping)
    } if isinstance(prior_audit_reviews, list) else {}
    for task in audit["findings"]:
        task["review_attempt"] = 2 if prior_outcomes.get(str(task.get("task_id"))) == "review_required" else 1
    audit["valid"] = not audit["findings"] and not audit.get("polish_semantic_blockers")
    audit["status"] = (
        "blocked" if audit.get("polish_semantic_blockers")
        else "review_required" if audit["findings"] else "validated"
    )


def _populate_visual_context(
    audit: dict[str, Any],
    report: dict[str, Any],
    *,
    output_dir: Path,
    records: list[Mapping[str, Any]],
    exact_sidecars: Mapping[Any, Any],
    config: Optional[Mapping[str, Any]],
) -> None:
    """Resolve candidate refs, page sets and visual evidence for every audit task."""
    if audit.get("findings"):
        first_by_file = {str(record["name"]): record for record in records}
        for audit_task in audit["findings"]:
            record = first_by_file.get(str(audit_task.get("file")), {})
            refs = audit_task.get("candidate_refs")
            if not isinstance(refs, list) or not refs:
                neighbor_keys = audit_task.get("neighbor_keys")
                keys = {str(key) for key in neighbor_keys} if isinstance(neighbor_keys, list) else set()
                raw_keys = str(audit_task.get("key") or "").split(",")
                keys.update(raw_keys)
                for k in raw_keys:
                    m = re.match(r"^fnm-number-(\d+)", k)
                    if m:
                        keys.add(m.group(1))
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
            if not pages_for_task and (audit_task.get("source_address") or {}).get("page"):
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
            task_refs = audit_task.get("candidate_refs")
            if audit_task.get("code") == "polish_semantic_issue" and isinstance(task_refs, list):
                pages_by_number = {
                    int(p.get("page", 0)): p
                    for p in report.get("pages", []) or []
                    if isinstance(p, dict) and str(p.get("page", "")).isdigit()
                }
                for ref in task_refs:
                    if not isinstance(ref, Mapping):
                        continue
                    try:
                        ref_page = int(ref["page"])
                        ref_block = int(ref["block"])
                    except (KeyError, TypeError, ValueError):
                        continue
                    ref_source = str(ref.get("source") or "primary").strip().lower()
                    page_row = pages_by_number.get(ref_page)
                    if not isinstance(page_row, dict):
                        page_row = {"page": ref_page, "candidates": [], "secondary_only_candidates": []}
                        report.setdefault("pages", []).append(page_row)
                        pages_by_number[ref_page] = page_row
                    candidate_field = "secondary_only_candidates" if ref_source == "secondary" else "candidates"
                    candidates_list = page_row.setdefault(candidate_field, [])
                    if any(
                        isinstance(item, Mapping)
                        and (int(item.get("block", -1)), str(item.get("source") or "primary").strip().lower()) == (ref_block, ref_source)
                        for item in candidates_list
                    ):
                        continue
                    sidecar_entry = (exact_sidecars.get(ref_page) or {}).get(ref_source)
                    if not isinstance(sidecar_entry, Mapping):
                        continue
                    try:
                        sidecar_data = json.loads((output_dir / str(sidecar_entry["path"])).read_text(encoding="utf-8"))
                    except (OSError, UnicodeError, json.JSONDecodeError, KeyError, TypeError):
                        continue
                    blocks = sidecar_data.get("blocks", [])
                    if not isinstance(blocks, list) or not 0 <= ref_block < len(blocks):
                        continue
                    block_data = blocks[ref_block]
                    if not isinstance(block_data, Mapping):
                        continue
                    block_text = (block_text_variants(output_dir, ref_page, block_data) or [str(audit_task.get("evidence_excerpt") or "")])[0]
                    candidates_list.append({
                        "page": ref_page,
                        "source": ref_source,
                        "block": ref_block,
                        "order": block_data.get("order", ref_block),
                        "label": str(block_data.get("label") or "Text"),
                        "bbox": block_data.get("bbox"),
                        "key": str(audit_task.get("key") or ""),
                        "confidence": "review",
                        "disposition": "review_required",
                        "text": block_text[:240],
                        "result_audit_admitted": True,
                        "review_reason": "polish_semantic_issue_source_block",
                    })
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
    from ..ocr_correction import select_refinement_pages

    selected_page_dir, _selected_page_kind = select_refinement_pages(output_dir, config=config)
    model = resolve_subagent_model(config, "footnote-prepare")
    # Re-run the deterministic result audit on every prepare. Existing review
    # resolutions are reusable only for the exact task and source block hash.
    try:
        records = unit_records(output_dir)
    except (OSError, ValueError):
        # Older library-only tests and narrow preparation callers may not
        # provide a refined unit map. The normal page decision workflow remains
        # available; source-wide result auditing starts once scope exists.
        records = []
    from .footnote_source import project_footnote_source_text

    source_texts = {
        str(record["name"]): project_footnote_source_text(
            (output_dir / "ocr_markdown" / str(record["name"])).read_bytes()
        )
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
        "schema_version": AUDIT_VERSION,
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
    _carry_prior_audit_findings(
        prior_result_audit, audit,
        output_dir=output_dir,
        current_target_inventory=current_target_inventory,
        records=records,
        report=report,
        config=config,
    )
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
    _attach_polish_semantic_tasks(
        audit, report,
        output_dir=output_dir,
        records=records,
        source_texts=source_texts,
        exact_sidecars=exact_sidecars,
    )
    _restore_admitted_candidates(
        audit, report,
        output_dir=output_dir,
        prior_result_audit=prior_result_audit,
        exact_sidecars=exact_sidecars,
        prior_review_data=prior_review_data,
    )
    # Truncated source/target definitions need exact owning-page evidence and
    # the following physical page when available. Reassociate by the complete
    # source definition signature, never by a chapter-wide page range.
    _resolve_truncated_definitions(
        audit,
        output_dir=output_dir,
        records=records,
        source_texts=source_texts,
        exact_sidecars=exact_sidecars,
    )
    known_task_ids = {str(item.get("task_id")) for item in audit.get("findings", [])}
    _admit_leading_superscripts(
        audit, report,
        output_dir=output_dir,
        exact_sidecars=exact_sidecars,
        unit_by_page=unit_by_page,
        source_texts=source_texts,
        known_task_ids=known_task_ids,
    )
    _add_supplemental_note_tasks(audit, source_texts=source_texts)
    _bind_supplemental_candidates(
        audit, report,
        output_dir=output_dir,
        records=records,
        source_texts=source_texts,
        exact_sidecars=exact_sidecars,
    )
    _bind_heading_tasks(
        audit,
        output_dir=output_dir,
        exact_sidecars=exact_sidecars,
        selected_page_dir=selected_page_dir,
    )
    # A body superscript's exact sidecar address identifies the marker
    # occurrence. A separate, same-key numbered-note task can identify the
    # definition it refers to; keep that linked candidate available for an
    # occurrence-level confirmed_footnote review without moving the body block.
    _link_marker_candidates(audit)
    _annotate_review_attempts(audit, prior_audit_reviews)
    _populate_visual_context(
        audit, report,
        output_dir=output_dir,
        records=records,
        exact_sidecars=exact_sidecars,
        config=config,
    )
    prior_reviews = audit.pop("_prior_applied_reviews", None)
    prior_review_tasks = audit.pop("_prior_applied_review_tasks", None)
    if prior_reviews is not None or prior_review_tasks is not None:
        from .footnote_result_review import validate_applied_result_audit_reviews

        valid_reviews, valid_tasks, _history_errors = validate_applied_result_audit_reviews(
            output_dir,
            prior_reviews,
            prior_review_tasks,
            current_tasks=[
                *(audit.get("findings", []) if isinstance(audit.get("findings"), list) else []),
                *(audit.get("reviewed_candidate_tasks", []) if isinstance(audit.get("reviewed_candidate_tasks"), list) else []),
            ],
        )
        if valid_reviews:
            audit["applied_result_audit_reviews"] = valid_reviews
            audit["applied_result_audit_review_tasks"] = valid_tasks
    report["result_audit"] = audit
    packet_path, packet_sha256, preparation_evidence_sha256 = write_review_packet(output_dir, report)
    from .footnote_unit_contexts import write_footnote_unit_contexts

    unit_context_files = write_footnote_unit_contexts(
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
    polish_semantic_blockers = audit.get("polish_semantic_blockers", [])
    recovery_required = bool(polish_semantic_blockers)
    manifest = _build_prepare_manifest(_ManifestInputs(
        output_dir=output_dir,
        report=report,
        audit=audit,
        model=model,
        book_title=book_title,
        packet_path=packet_path,
        packet_sha256=packet_sha256,
        preparation_evidence_sha256=preparation_evidence_sha256,
        unit_context_files=unit_context_files,
        ambiguity_count=ambiguity_count,
        page_review_required=page_review_required,
        polish_semantic_blockers=polish_semantic_blockers,
        recovery_required=recovery_required,
    ))
    manifest_path = output_dir / "footnote_subagent_manifest.json"
    atomic_write_json(manifest_path, manifest)

    prompt = _render_review_prompt(
        book_title,
        report,
        ambiguity_count=ambiguity_count,
        page_review_required=page_review_required,
        audit=audit,
        recovery_required=recovery_required,
        polish_semantic_blockers=polish_semantic_blockers,
        model=model,
    )
    prompt_path = output_dir / "footnote_subagent_prompt.md"
    atomic_write_text(prompt_path, prompt)
    return {
        "report": output_dir / "footnote_candidates.json",
        "manifest": manifest_path,
        "prompt": prompt_path,
        "review_packet": output_dir / packet_path,
    }


__all__ = ["prepare_footnote_subagent"]
