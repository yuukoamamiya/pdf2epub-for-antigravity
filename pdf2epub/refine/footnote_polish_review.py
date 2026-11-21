"""Map polish footnote semantic reports to exact OCR review tasks."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Mapping

from ..workflow_contracts import sha256_file
from ..polish_footnote_semantics import polish_issue_task_id, validate_prepared_file_review
from .footnote_matching import block_text_variants

def polish_semantic_issue_tasks(
    output_dir: Path,
    records: list[Mapping[str, Any]],
    source_texts: Mapping[str, str],
    sidecar_index: Mapping[int, Mapping[str, Mapping[str, Any]]],
    page_report: Mapping[str, Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Route current polish semantic issues back to exact OCR evidence.

    Polish sidecars identify a source excerpt but do not decide ownership. An
    issue is admitted only when that exact excerpt maps to one current OCR
    block; ambiguous mappings fail closed rather than widening to a unit/page
    range.
    """
    manifest_path = output_dir / "polish_subagent_manifest.json"
    if not manifest_path.is_file():
        return [], []
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        return [], [{
            "kind": "stale_contract",
            "file": "polish_subagent_manifest.json",
            "message": f"unreadable: {exc}",
            "recovery": "repair/refresh the polish manifest, rerun polish --resume, then footnote-prepare",
        }]
    source_dir = str(manifest.get("source_dir") or "") if isinstance(manifest, Mapping) else ""
    target_dir = str(manifest.get("target_dir") or "") if isinstance(manifest, Mapping) else ""
    normalized_dir = output_dir / "footnote_normalized"
    if normalized_dir.is_dir() and any(normalized_dir.glob("*.md")):
        polish_source_root = normalized_dir.resolve()
    else:
        polish_source_root = (output_dir / source_dir).resolve()
    target_root = (output_dir / target_dir).resolve()
    try:
        polish_source_root.relative_to(output_dir.resolve())
        target_root.relative_to(output_dir.resolve())
    except ValueError:
        return [], [{
            "kind": "stale_contract",
            "file": "polish_subagent_manifest.json",
            "message": "source or target directory escapes output workspace",
            "recovery": "repair/refresh the polish manifest, rerun polish --resume, then footnote-prepare",
        }]
    tasks: list[dict[str, Any]] = []
    diagnostics: list[dict[str, Any]] = []

    def diagnose(
        kind: str,
        filename: str,
        message: str,
        *,
        issue_id: str | None = None,
        stage: str | None = None,
        match_count: int | None = None,
    ) -> None:
        row: dict[str, Any] = {
            "kind": kind,
            "file": filename,
            "message": message,
            "recovery": (
                "rerun polish --resume, then footnote-prepare"
                if kind == "stale_contract"
                else (
                    "have the original Polish Reviewer repair this current semantic sidecar; "
                    "for mapping failures, revise source_excerpt to a unique continuous excerpt "
                    "in one primary OCR block; if it spans blocks, report separately per block; "
                    "then rerun footnote-prepare"
                )
            ),
        }
        if issue_id:
            row["issue_id"] = issue_id
        if stage:
            row["mapping_stage"] = stage
        if match_count is not None:
            row["match_count"] = match_count
        diagnostics.append(row)
    semantic = manifest.get("footnote_semantics") if isinstance(manifest, Mapping) else None
    semantic_files = semantic.get("files") if isinstance(semantic, Mapping) else None
    for record in records:
        filename = str(record.get("name") or "")
        if not isinstance(semantic_files, Mapping) or filename not in semantic_files:
            continue
        raw_source = source_texts.get(filename)
        polish_source_path = polish_source_root / filename
        target_path = target_root / filename
        if raw_source is None or not polish_source_path.is_file() or not target_path.is_file():
            diagnose("stale_contract", filename, "current source, polished source, or target file is missing")
            continue
        polish_source = polish_source_path.read_text(encoding="utf-8")
        target = target_path.read_text(encoding="utf-8")
        review = validate_prepared_file_review(
            output_dir, filename, source_text=polish_source, target_text=target, required=False,
        )
        if review is None:
            continue
        if review.get("contract_valid") is not True:
            stale_fields = review.get("stale_fields")
            if isinstance(stale_fields, list) and stale_fields:
                diagnose(
                    "stale_contract", filename,
                    "semantic review hashes or prepared contract snapshot are no longer current: "
                    + ", ".join(str(field) for field in stale_fields),
                )
                continue
            raw_issues = review.get("issues")
            if not isinstance(raw_issues, list):
                diagnose("invalid_review", filename, "current semantic sidecar is malformed and has no usable issues list")
                continue
            if not raw_issues:
                if review.get("status") == "clear":
                    continue
                if review.get("errors"):
                    diagnose("invalid_review", filename, "current semantic sidecar failed validation and provides no usable issues to map")
                    continue
            invalid_issue_rows = [
                item for item in raw_issues
                if not isinstance(item, Mapping) or not item.get("issue_id") or not item.get("source_excerpt")
            ]
            if invalid_issue_rows:
                diagnose(
                    "invalid_review", filename,
                    "current semantic sidecar contains malformed issue entries",
                )
                continue
            if review.get("errors"):
                diagnose(
                    "invalid_review", filename,
                    "current semantic sidecar failed shape or coverage validation: "
                    + "; ".join(str(error) for error in review.get("errors", [])[:5]),
                )
            # Preserve parseable unresolved issues so mapping failures become explicit blockers.
        for issue in review.get("issues", []) or []:
            excerpt = str(issue.get("source_excerpt") or "")
            from .footnote_matching import find_exact_signature_spans
            polish_first = polish_source.find(excerpt) if excerpt else -1
            if not excerpt:
                polish_match_count = 0
            elif polish_first >= 0 and polish_source.find(excerpt, polish_first + 1) < 0:
                polish_match_count = 1
            else:
                polish_match_count = len(find_exact_signature_spans(polish_source, excerpt))
            if polish_match_count != 1:
                diagnose(
                    "current_issue_mapping_failure", filename,
                    "source_excerpt is absent or not unique in the current Polish source",
                    issue_id=str(issue.get("issue_id") or ""),
                    stage="polish_source",
                    match_count=polish_match_count,
                )
                continue
            raw_record = record
            raw_text = raw_source
            raw_spans = find_exact_signature_spans(raw_text, excerpt)
            if len(raw_spans) != 1 and record.get("chapter_id"):
                chapter_id = str(record.get("chapter_id"))
                chapter_matches: list[tuple[Mapping[str, Any], str, list[tuple[int, int]]]] = []
                total_chapter_spans = 0
                for sibling in records:
                    sib_name = str(sibling.get("name") or "")
                    if not sib_name or sib_name == filename or str(sibling.get("chapter_id") or "") != chapter_id:
                        continue
                    sib_text = source_texts.get(sib_name)
                    if sib_text is None:
                        continue
                    sib_spans = find_exact_signature_spans(sib_text, excerpt)
                    if sib_spans:
                        total_chapter_spans += len(sib_spans)
                        chapter_matches.append((sibling, sib_text, sib_spans))
                if len(chapter_matches) == 1 and total_chapter_spans == 1:
                    raw_record, raw_text, raw_spans = chapter_matches[0]
            if len(raw_spans) != 1:
                diagnose(
                    "current_issue_mapping_failure", filename,
                    "source_excerpt does not map uniquely to current raw refinement Markdown",
                    issue_id=str(issue.get("issue_id") or ""),
                    stage="raw_refined_source",
                    match_count=len(raw_spans),
                )
                continue
            raw_start, raw_end = raw_spans[0]
            raw_excerpt = raw_text[raw_start:raw_end]
            primary_matches: list[tuple[int, str, int, Mapping[str, Any], Mapping[str, Any]]] = []
            sig_matches: list[tuple[int, str, int, Mapping[str, Any], Mapping[str, Any]]] = []
            adjacent_pair_matches: list[tuple[int, str, int, int, Mapping[str, Any], Mapping[str, Any]]] = []
            for page in range(int(raw_record.get("start_page") or 0), int(raw_record.get("end_page") or 0) + 1):
                for source_kind, entry in sidecar_index.get(page, {}).items():
                    if str(source_kind) != "primary":
                        continue
                    if not isinstance(entry, Mapping):
                        continue
                    try:
                        data = json.loads((output_dir / str(entry["path"])).read_text(encoding="utf-8"))
                    except (OSError, UnicodeError, json.JSONDecodeError, KeyError, TypeError):
                        continue
                    blocks = data.get("blocks", [])
                    block_variants_cache: list[tuple[int, Mapping[str, Any], list[str]]] = []
                    for block_index, block in enumerate(blocks):
                        if not isinstance(block, Mapping):
                            continue
                        variants = block_text_variants(output_dir, page, block)
                        block_variants_cache.append((block_index, block, variants))
                        if any(variant.count(excerpt) == 1 for variant in variants):
                            primary_matches.append((page, str(source_kind), block_index, entry, block))
                        elif any(len(find_exact_signature_spans(variant, excerpt)) == 1 for variant in variants):
                            sig_matches.append((page, str(source_kind), block_index, entry, block))
                    for pos in range(len(block_variants_cache) - 1):
                        idx_a, blk_a, vars_a = block_variants_cache[pos]
                        idx_b, _blk_b, vars_b = block_variants_cache[pos + 1]
                        if idx_b != idx_a + 1 or not vars_a or not vars_b:
                            continue
                        joined = f"{vars_a[0]}\n\n{vars_b[0]}"
                        if len(find_exact_signature_spans(joined, excerpt)) == 1:
                            adjacent_pair_matches.append((page, str(source_kind), idx_a, idx_b, entry, blk_a))
            extra_linked_refs: set[tuple[int, str, int]] = set()
            unique_matches = {(page, kind, block) for page, kind, block, _, _ in primary_matches}
            if not unique_matches and sig_matches:
                primary_matches = sig_matches
                unique_matches = {(page, kind, block) for page, kind, block, _, _ in primary_matches}
            if not unique_matches and len(adjacent_pair_matches) == 1:
                adj_page, adj_kind, idx_a, idx_b, adj_entry, adj_blk = adjacent_pair_matches[0]
                primary_matches = [(adj_page, adj_kind, idx_a, adj_entry, adj_blk)]
                unique_matches = {(adj_page, adj_kind, idx_a)}
                extra_linked_refs.add((adj_page, adj_kind, idx_b))
            if len(unique_matches) != 1:
                diagnose(
                    "current_issue_mapping_failure", filename,
                    f"source_excerpt maps to {len(unique_matches)} current primary OCR blocks",
                    issue_id=str(issue.get("issue_id") or ""),
                    stage="primary_ocr_block",
                    match_count=len(unique_matches),
                )
                continue
            page, source_kind, block_index = next(iter(unique_matches))
            _, _, _, entry, block_data = next(row for row in primary_matches if row[:3] == (page, source_kind, block_index))
            evidence_hash = hashlib.sha256(json.dumps(
                {"sidecar_sha256": entry["sha256"], "block": block_data},
                ensure_ascii=False, sort_keys=True, separators=(",", ":"),
            ).encode("utf-8")).hexdigest()
            line = raw_text.count("\n", 0, raw_start) + 1
            task_id = polish_issue_task_id(filename, issue)
            linked_pages = {page}
            linked_refs = {(page, source_kind, block_index), *extra_linked_refs}
            for collection in ("continuation_chains", "cross_page_windows"):
                for window in page_report.get(collection, []) or []:
                    if not isinstance(window, Mapping):
                        continue
                    refs: list[Mapping[str, Any]] = []
                    if collection == "continuation_chains":
                        refs.extend(ref for ref in window.get("candidate_refs", []) or [] if isinstance(ref, Mapping))
                    else:
                        for side in ("left_candidates", "right_candidates"):
                            refs.extend(ref for ref in window.get(side, []) or [] if isinstance(ref, Mapping))
                    window_refs = set()
                    for ref in refs:
                        try:
                            window_refs.add((int(ref.get("page", 0)), str(ref.get("source") or "primary"), int(ref.get("block", -1))))
                        except (TypeError, ValueError):
                            continue
                    if (page, source_kind, block_index) in window_refs:
                        linked_refs.update(window_refs)
                        linked_pages.update(int(value) for value in window.get("pages", []) if str(value).isdigit())
            for gap in page_report.get("continuation_reviews", []) or []:
                if not isinstance(gap, Mapping):
                    continue
                source_note = gap.get("source_note")
                if isinstance(source_note, Mapping) and (
                    int(source_note.get("page", -1)) == page
                    and str(source_note.get("source") or "primary") == source_kind
                    and int(source_note.get("block", -1)) == block_index
                ):
                    linked_pages.update(int(value) for value in gap.get("pages", []) if str(value).isdigit())
            tasks.append({
                "task_id": task_id,
                "code": "polish_semantic_issue",
                "file": filename,
                "line": line,
                "page": page,
                "key": "",
                "evidence_origin": "source_markdown",
                "evidence_sha256": evidence_hash,
                "evidence_excerpt": raw_excerpt,
                "evidence_file_sha256": sha256_file(output_dir / "ocr_markdown" / filename),
                **({"polish_raw_source_file": str(raw_record.get("name"))} if str(raw_record.get("name") or "") != filename else {}),
                "evidence_target_sha256": review.get("target_sha256"),
                "polish_issue": {
                    key: issue.get(key) for key in (
                        "issue_id", "code", "source_excerpt", "target_excerpt", "reason",
                        "unit_id", "file", "source_sha256", "target_sha256", "context_sha256",
                    ) if issue.get(key) is not None
                } | {
                    "polish_source_sha256": review.get("source_sha256"),
                    "raw_source_excerpt_sha256": hashlib.sha256(raw_excerpt.encode("utf-8")).hexdigest(),
                },
                "detail": str(issue.get("reason") or "Polish reviewer flagged a footnote semantic boundary for image review."),
                "requires_visual_review": True,
                "source_address": {
                    "page": page, "source": source_kind, "block": block_index,
                    "sidecar": str(entry["path"]), "sidecar_sha256": str(entry["sha256"]),
                },
                "candidate_refs": [
                    {"page": ref_page, "source": ref_source, "block": ref_block}
                    for ref_page, ref_source, ref_block in sorted(linked_refs)
                ],
                "pages": sorted(linked_pages),
                "context_status": "exact_source_excerpt_mapped_to_current_ocr_block",
            })
    return tasks, diagnostics




__all__ = ["polish_semantic_issue_tasks"]
