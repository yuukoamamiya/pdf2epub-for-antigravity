"""Compact, immutable visual-review packet projected from the audit report."""
from __future__ import annotations

import hashlib
import json
from typing import Any, Mapping

from ..workflow_contracts import atomic_write_json, sha256_file
from .footnote_contracts import FOOTNOTE_PREPARE_SCHEMA_VERSION
from .footnote_page_evidence import resolve_page_sidecar_path

PACKET_FILENAME = "footnote_review_packet.json"


def build_review_packet(report: Mapping[str, Any]) -> dict[str, Any]:
    """Project review evidence once, keyed by exact page/source/block address."""
    pages = [row for row in report.get("pages", []) if isinstance(row, Mapping)]
    address_map: dict[str, dict[str, Any]] = {}
    for page in pages:
        page_number = int(page.get("page", 0))
        for field, default_source in (("candidates", "primary"), ("audit_candidates", "primary"), ("secondary_only_candidates", "secondary")):
            for row in page.get(field, []) or []:
                if not isinstance(row, Mapping):
                    continue
                source = str(row.get("source") or default_source)
                block = int(row.get("block", -1))
                address = f"{page_number}:{source}:{block}"
                facts = {
                    key: row[key] for key in (
                        "page", "source", "block", "key", "label", "bbox",
                        "confidence", "local_role",
                    ) if key in row
                }
                if row.get("confidence") == "review" or not row.get("key") or row.get("local_role") == "footnote_continuation":
                    facts.update({key: row[key] for key in (
                        "marker_label", "review_reason", "text", "text_ending",
                        "continuation_evidence", "body_flow_warning",
                    ) if key in row})
                address_map[address] = facts
    evidence_pages: set[int] = set(int(page) for page in report.get("review_pages", []) or [])
    windows = []
    for window in report.get("cross_page_windows", []) or []:
        if not isinstance(window, Mapping):
            continue
        window_pages = [int(page) for page in window.get("pages", [])]
        evidence_pages.update(window_pages)
        refs = []
        for side in ("left_candidates", "right_candidates"):
            for candidate in window.get(side, []) or []:
                if isinstance(candidate, Mapping):
                    refs.append(f"{int(candidate.get('page', 0))}:{candidate.get('source', 'primary')}:{int(candidate.get('block', -1))}")
        windows.append({"pages": window_pages, "candidate_refs": sorted(set(refs)), "reason": window.get("reason")})
    gaps = [dict(item) for item in report.get("continuation_reviews", []) or [] if isinstance(item, Mapping)]
    for task in gaps:
        evidence_pages.update(int(page) for page in task.get("pages", []) or [])
        source_note = task.get("source_note")
        if isinstance(source_note, Mapping):
            page_number = int(source_note.get("page", 0))
            source = str(source_note.get("source") or "primary")
            block = int(source_note.get("block", -1))
            address = f"{page_number}:{source}:{block}"
            address_map.setdefault(address, {
                key: source_note[key] for key in (
                    "page", "source", "block", "key", "text_ending",
                ) if key in source_note
            } | {"confidence": "review", "local_role": "footnote_continuation_gap"})
            task["source_note_ref"] = address
    page_index = {int(page.get("page", 0)): page for page in pages}
    visual_inventory = report.get("visual_evidence", {}).get("files", {}) if isinstance(report.get("visual_evidence"), Mapping) else {}
    gap_images: dict[int, str] = {}
    for task in gaps:
        for page, image in (task.get("visual_files", {}) or {}).items():
            if isinstance(image, Mapping) and isinstance(image.get("path"), str):
                gap_images[int(page)] = image["path"]
    visual_pages = []
    for number in sorted(evidence_pages):
        page = page_index.get(number, {})
        item = {key: page[key] for key in (
            "page", "sidecar", "secondary_sidecar", "visual_file", "consensus_visual_review", "consensus_status"
        ) if key in page}
        item.setdefault("page", number)
        item.setdefault("sidecar", f"pages/page_{number:03d}.ocr.json")
        if not item.get("visual_file") and number in gap_images:
            item["visual_file"] = gap_images[number]
        if isinstance(visual_inventory, Mapping):
            visual = visual_inventory.get(str(number), visual_inventory.get(number))
            if isinstance(visual, str) and not item.get("visual_file"):
                item["visual_file"] = visual
        if report.get("ocr_evidence_mode") == "two_ocr":
            secondary_hashes = report.get("secondary_sidecar_sha256", {})
            secondary_path = resolve_page_sidecar_path(secondary_hashes, number, "ocr_secondary")
            if secondary_path and not item.get("secondary_sidecar"):
                item["secondary_sidecar"] = secondary_path
        visual_pages.append(item)
    regions = []
    for page in pages:
        for region in page.get("footnote_regions", []) or []:
            if not isinstance(region, Mapping) or region.get("disposition") != "visual_audit_required":
                continue
            block_refs = []
            for block in region.get("blocks", []) or []:
                if isinstance(block, Mapping):
                    block_refs.append(
                        f"{int(block.get('page', page.get('page', 0)))}:{block.get('source', 'primary')}:{int(block.get('block', -1))}"
                    )
            regions.append({key: region[key] for key in (
                "region_id", "page", "bbox", "block_indices", "keys", "reasons", "region_visual_file"
            ) if key in region} | {"candidate_refs": sorted(set(block_refs))})
    return {
        "schema_version": FOOTNOTE_PREPARE_SCHEMA_VERSION,
        "task": "footnote-review-packet",
        "source_kind": report.get("source_kind"),
        "ocr_evidence_mode": report.get("ocr_evidence_mode"),
        "sidecar_sha256": report.get("sidecar_sha256", {}),
        "secondary_sidecar_sha256": report.get("secondary_sidecar_sha256", {}),
        "review_pages": sorted(evidence_pages),
        "visual_pages": visual_pages,
        "regions": regions,
        "candidates": address_map,
        "cross_page_windows": windows,
        "continuation_reviews": gaps,
        "bottom_detection": report.get("bottom_detection", {}),
    }


def write_review_packet(output_dir, report: Mapping[str, Any]) -> tuple[str, str, str]:
    path = output_dir / PACKET_FILENAME
    packet = build_review_packet(report)
    evidence_hash = hashlib.sha256(json.dumps(packet, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")).hexdigest()
    packet["preparation_evidence_sha256"] = evidence_hash
    atomic_write_json(path, packet)
    return PACKET_FILENAME, sha256_file(path), evidence_hash


def validate_review_packet(output_dir, manifest: Mapping[str, Any]) -> dict[str, Any]:
    """Fail closed on stale packet bytes or dangling exact candidate refs."""
    relative = manifest.get("review_packet")
    expected = manifest.get("review_packet_sha256")
    if not isinstance(relative, str) or not isinstance(expected, str):
        raise ValueError("footnote handoff is missing its review packet contract; rerun footnote-prepare")
    path = output_dir / relative
    try:
        path.resolve().relative_to(output_dir.resolve())
    except ValueError as exc:
        raise ValueError("footnote review packet path escapes output directory") from exc
    if not path.is_file() or sha256_file(path) != expected:
        raise ValueError("footnote review packet changed or is missing; rerun footnote-prepare")
    try:
        packet = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"footnote review packet is unreadable; rerun footnote-prepare: {exc}") from exc
    if not isinstance(packet, Mapping) or packet.get("schema_version") != FOOTNOTE_PREPARE_SCHEMA_VERSION:
        raise ValueError("footnote review packet schema is stale; rerun footnote-prepare")
    recorded_evidence_hash = manifest.get("preparation_evidence_sha256")
    if not isinstance(recorded_evidence_hash, str) or packet.get("preparation_evidence_sha256") != recorded_evidence_hash:
        raise ValueError("footnote preparation evidence hash is missing or inconsistent; rerun footnote-prepare")
    report_path = output_dir / "footnote_candidates.json"
    try:
        report = json.loads(report_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"footnote candidate report is unreadable; rerun footnote-prepare: {exc}") from exc
    expected_evidence_hash = hashlib.sha256(
        json.dumps(build_review_packet(report), sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    if expected_evidence_hash != recorded_evidence_hash:
        raise ValueError("footnote preparation evidence changed; rerun footnote-prepare")
    candidates = packet.get("candidates")
    if not isinstance(candidates, Mapping):
        raise ValueError("footnote review packet has no candidate address map")
    for window in packet.get("cross_page_windows", []) or []:
        for ref in window.get("candidate_refs", []) if isinstance(window, Mapping) else []:
            if ref not in candidates:
                raise ValueError(f"footnote review packet has dangling candidate reference {ref}")
    for region in packet.get("regions", []) or []:
        for ref in region.get("candidate_refs", []) if isinstance(region, Mapping) else []:
            if ref not in candidates:
                raise ValueError(f"footnote review packet has dangling region block reference {ref}")
    return dict(packet)
