"""Canonical sparse footnote context projection and freshness contract."""

from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from typing import Any, Mapping, Optional

from ..workflow_contracts import atomic_write_json, sha256_file
from .footnote_contracts import FOOTNOTE_PREPARE_SCHEMA_VERSION, FOOTNOTE_UNIT_CONTEXT_PROJECTION_VERSION
from .footnote_structure import load_refined_units
from .footnote_page_evidence import page_sidecar_index
from .footnote_review_packet import validate_review_packet

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
        continuation_chains = [
            _compact_window_context(item) for item in report.get("continuation_chains", [])
            if any(start_page <= int(page) <= end_page for page in item.get("pages", []))
        ]
        evidence_pages = set(range(start_page, end_page + 1))
        for window in [*windows, *continuation_reviews, *continuation_chains]:
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
            refs.update(str(ref) for ref in window.get("candidate_refs", []))
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
            "continuation_chains": continuation_chains,
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


def write_footnote_unit_contexts(
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
        report, packet_data, load_refined_units(output_dir), page_index, review_packet,
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
        "key", "candidate_refs",
    ) if key in window}
    for field in ("left_candidates", "right_candidates"):
        rows = window.get(field)
        if isinstance(rows, list):
            result[field.removesuffix("_candidates") + "_candidate_refs"] = [
                f"{int(row.get('page', 0))}:{row.get('source', 'primary')}:{int(row.get('block', -1))}"
                for row in rows if isinstance(row, Mapping)
            ]
    return result



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
    validated_paths: dict[str, tuple[str, Path]] = {}
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
        validated_paths[name] = (relative, path)
    packet = validate_review_packet(output_dir, manifest)
    # The report is the independent page inventory. A worker context cannot
    # authenticate its own evidence paths by changing both a page and a fact.
    try:
        report = json.loads((output_dir / "footnote_candidates.json").read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"footnote candidate report is unreadable; rerun footnote-prepare: {exc}") from exc
    expected_contexts = _project_unit_contexts(
        report, packet, load_refined_units(output_dir),
        page_sidecar_index(output_dir, report), manifest["review_packet"],
    )
    if set(values) != set(expected_contexts) or set(expected_context_hashes) != set(expected_contexts):
        raise ValueError("footnote unit context inventory differs from current refined mapping; rerun footnote-prepare and footnote-validate (existing decisions are retained)")
    result: dict[str, Path] = {}
    for name, (relative, path) in validated_paths.items():
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



__all__ = ["load_footnote_unit_contexts", "write_footnote_unit_contexts"]
