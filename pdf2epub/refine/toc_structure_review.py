"""Evidence inventory and fail-closed review contract for PDF TOC structure.

Local code identifies locations worth examining; it never assigns semantic roles
or hierarchy. The reviewer owns those decisions and the tree remains canonical.
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any, Mapping

from pdf2epub.toc_model import (
    TOC_TREE_SCHEMA_VERSION,
    iter_toc_nodes,
    normalize_toc_source_label,
    validate_toc_node_identities,
)
from pdf2epub.workflow_contracts import atomic_write_json
from pdf2epub.numbering import standalone_number_label
from .heading_ranges import source_range_matches_title

SCHEMA_VERSION = 2
DISPOSITIONS = {"toc_node", "body_text", "page_number", "running_header", "ornament", "unresolved"}
MAX_LAYOUT_BLOCKS_PER_PAGE = 80
_CANDIDATE = re.compile(r"^\s*(?:#{1,6}\s+.+|\*\*[^*]{2,100}\*\*|(?:chapter|part|book|appendix|section)\s+[\wIVXLCDM.-]+.*|\d+(?:\.\d+){0,4}[.)]?\s+.{2,100})\s*$", re.I)
_STANDALONE_EMPHASIS = re.compile(
    r"^\s*(?:\*\*?[^*\n]{1,300}\*\*?|__?[^_\n]{1,300}__?|"
    r"<(?:em|i)\b[^>]*>[^<\n]{1,300}</(?:em|i)>)\s*$",
    re.IGNORECASE,
)
_EMPHASIS_OPEN = re.compile(r"^\s*(\*\*?|__?)\s*\S.*$")
_EMPHASIS_CLOSE = re.compile(r".*\S\s*(\*\*?|__?)\s*$")
_MARKDOWN_LIST = re.compile(r"^\s{0,3}(?:[-+*]\s+|\d+[.)]\s+|\[\^.+?\]:\s*)")
_FENCE = re.compile(r"^\s{0,3}(`{3,}|~{3,})")


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _file_sha(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _heading_like_text_regions(page: int, lines: list[str]) -> list[dict[str, Any]]:
    """Collect heading-like source locations without deciding their meaning."""
    rows: list[dict[str, Any]] = []
    line_no = 1
    fence: tuple[str, int] | None = None
    while line_no <= len(lines):
        line = lines[line_no - 1]
        stripped = line.strip()
        fence_match = _FENCE.match(line)
        if fence_match:
            marker = fence_match.group(1)
            if fence is None:
                fence = (marker[0], len(marker))
            elif (marker[0] == fence[0] and len(marker) >= fence[1]
                  and not line[fence_match.end():].strip()):
                fence = None
            line_no += 1
            continue
        if fence is not None or line.startswith(("    ", "\t")) or _MARKDOWN_LIST.match(line):
            line_no += 1
            continue
        number_text = standalone_number_label(stripped)
        if number_text:
            digest_source = f"{page}:{line_no}:{line_no}:{stripped}"
            rows.append({
                "candidate_id": "cand-" + _sha(digest_source.encode())[:20],
                "kind": "explicit_page_label" if re.match(r"(?i)^(?:pdf\s+)?page\b", stripped) else "standalone_number",
                "page": page, "line": line_no,
                "end_line": line_no, "excerpt": stripped[:300],
            })
            line_no += 1
            continue
        standalone_title = bool(
            stripped and len(stripped) <= 100 and stripped.isupper()
            and any(character.isalpha() for character in stripped)
        )
        emphasis_opener = _EMPHASIS_OPEN.match(stripped)
        if stripped and (
            _CANDIDATE.match(line) or _STANDALONE_EMPHASIS.match(line)
            or standalone_title or emphasis_opener
        ):
            start_line = line_no
            end_line = line_no
            excerpt = stripped
            # Pair a standalone emphasis opener with a later closer. Keep the
            # bounded region as evidence; never join ordinary prose lines.
            if emphasis_opener and not _STANDALONE_EMPHASIS.match(stripped):
                marker = emphasis_opener.group(1)
                close = _EMPHASIS_CLOSE
                for offset in range(1, min(5, len(lines) - line_no + 1)):
                    following = lines[line_no + offset - 1].strip()
                    if following and close.match(following) and following.endswith(marker):
                        end_line = line_no + offset
                        excerpt = " ".join(value.strip() for value in lines[line_no - 1:end_line])
                        break
            digest_source = f"{page}:{start_line}:{end_line}:{excerpt}"
            rows.append({
                "candidate_id": "cand-" + _sha(digest_source.encode())[:20],
                "kind": "text_line", "page": page, "line": start_line,
                "end_line": end_line, "excerpt": excerpt[:300],
            })
            line_no = end_line + 1
            continue
        line_no += 1
    return rows


def selected_page_source_kind(output_dir: Path, pages_dir: Path) -> str:
    """Describe the selected page artifact set without consulting old probes."""
    from .pdf_evidence import is_native_text_source

    if is_native_text_source(output_dir) and Path(pages_dir).resolve() == (Path(output_dir) / "pages").resolve():
        return "native_text"
    return "ocr_corrected" if Path(pages_dir).resolve() != (Path(output_dir) / "pages").resolve() else "ocr"


def create_structure_review_snapshot(output_dir: Path, pages_dir: Path, page_source_kind: str,
                                     outline_path: Path, pdf_path: Path,
                                     config: Any = None) -> tuple[dict[str, Any], dict[str, Any]]:
    """Build the expected source inventory without writing files."""
    page_files = sorted(pages_dir.glob("page_*.md"))
    pages = []
    candidates = []
    layout_evidence = []
    for page_file in page_files:
        m = re.fullmatch(r"page_(\d+)\.md", page_file.name)
        if not m:
            continue
        page = int(m.group(1))
        raw = page_file.read_bytes()
        lines = raw.decode("utf-8", errors="replace").splitlines()
        pages.append({
            "page": page,
            "path": page_file.relative_to(output_dir).as_posix(),
            "sha256": _sha(raw),
            "line_count": len(lines),
        })
        candidates.extend(_heading_like_text_regions(page, lines))
    outline_data = _json(outline_path) if outline_path.is_file() else None
    outline_entries = outline_data.get("chapters", []) if isinstance(outline_data, dict) else []
    flattened_outline: list[dict[str, Any]] = []
    def collect_outline(nodes: Any) -> None:
        if not isinstance(nodes, list): return
        for entry in nodes:
            if isinstance(entry, dict):
                flattened_outline.append(entry)
                collect_outline(entry.get("children", []))
    collect_outline(outline_entries)
    if flattened_outline:
        for i, item in enumerate(flattened_outline):
            if not isinstance(item, dict):
                continue
            title = item.get("title") or item.get("text") or ""
            page = item.get("start_page", 1)
            if type(page) is not int or page < 1:
                page = 1
            cid = "outline-" + _sha(f"{i}:{title}:{page}".encode())[:20]
            candidates.append({"candidate_id": cid, "kind": "pdf_outline", "page": page,
                               "line": None, "excerpt": str(title)[:180], "outline_index": i})
    outline = _file_sha(outline_path) if outline_path.is_file() else None
    pdf_sha = _file_sha(pdf_path) if pdf_path.is_file() else None
    evidence = []
    evidence_paths = [pages_dir / "ocr_progress.json"]
    evidence_paths.extend(sorted(pages_dir.glob("page_*.ocr.json")))
    evidence_paths.extend(sorted((output_dir / "pages").glob("page_*.html")))
    if page_source_kind == "ocr_corrected":
        evidence_paths.append(output_dir / "ocr_consensus.json")
        evidence_paths.extend(sorted((output_dir / "pages").glob("page_*.ocr.json")))
        evidence_paths.extend(sorted((output_dir / "ocr_secondary").glob("page_*.ocr.json")))
    for path in evidence_paths:
        if path.is_file():
            evidence.append({"path": path.relative_to(output_dir).as_posix(), "sha256": _file_sha(path)})
        if path.name.endswith(".ocr.json") and path.is_file():
            sidecar = _json(path)
            if not isinstance(sidecar, dict):
                continue
            from .layout_evidence import normalized_bbox, text_from_block
            blocks = sidecar.get("blocks", [])
            if not isinstance(blocks, list):
                continue
            sidecar_page = sidecar.get("page_number")
            page_match = re.fullmatch(r"page_(\d+)\.ocr\.json", path.name)
            source_page = int(page_match.group(1)) if page_match else None
            if source_page is None or type(sidecar_page) is not int or sidecar_page != source_page:
                continue
            for block_index, block in enumerate(blocks[:MAX_LAYOUT_BLOCKS_PER_PAGE]):
                if not isinstance(block, dict):
                    continue
                bbox = normalized_bbox(block, sidecar)
                text = text_from_block(block).strip()
                if bbox is not None and text:
                    label = str(block.get("label") or block.get("type") or "")
                    evidence_record = {
                        "page": source_page,
                        "block": block_index,
                        "text": text[:180],
                        "bbox": bbox,
                        "label": label,
                        "source": path.relative_to(output_dir).as_posix(),
                    }
                    layout_evidence.append({"page": source_page,
                                            "block": block_index,
                                            "text": text[:180],
                                            "bbox": bbox,
                                            "source": path.relative_to(output_dir).as_posix()})
                    # Sidecar-only heading labels are possible true titles
                    # omitted from Markdown. Short independent blocks also
                    # remain review candidates (including ordinary prose).
                    if label in {"Section-Header", "Page-Header", "Page-Footer"} or len(text) <= 120:
                        payload = json.dumps(evidence_record, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
                        candidates.append({
                            "candidate_id": "layout-" + _sha(payload.encode())[:20],
                            "kind": "layout_region",
                            **evidence_record,
                        })
            if len(blocks) > MAX_LAYOUT_BLOCKS_PER_PAGE:
                layout_evidence.append({
                    "page": source_page,
                    "source": path.relative_to(output_dir).as_posix(),
                    "omitted_blocks": len(blocks) - MAX_LAYOUT_BLOCKS_PER_PAGE,
                })
    from .pdf_evidence import pdf_evidence_mode
    evidence_mode = pdf_evidence_mode(output_dir, config)
    ocr_config = config.get("ocr", {}) if isinstance(config, Mapping) else {}
    secondary = ocr_config.get("secondary", {}) if isinstance(ocr_config, Mapping) else {}
    config_evidence = {
        "backend": ocr_config.get("backend") if isinstance(ocr_config, Mapping) else None,
        "secondary_enabled": secondary.get("enabled") if isinstance(secondary, Mapping) else None,
        "secondary_backend": secondary.get("backend") if isinstance(secondary, Mapping) else None,
    }
    fingerprint_payload = {"pages": pages, "outline_sha256": outline, "pdf_sha256": pdf_sha,
                           "page_source_kind": page_source_kind,
                           "evidence_mode": evidence_mode,
                           "ocr_evidence_config": config_evidence,
                           "pages_dir": pages_dir.relative_to(output_dir).as_posix(), "evidence": evidence}
    fingerprint = _sha(json.dumps(fingerprint_payload, sort_keys=True, separators=(",", ":")).encode())
    candidate_doc = {"schema_version": SCHEMA_VERSION, "source_fingerprint": fingerprint,
                     "pages": pages, "candidates": candidates,
                     "layout_evidence": layout_evidence}
    candidate_doc["inventory_sha256"] = _sha(json.dumps(candidate_doc, sort_keys=True, separators=(",", ":")).encode())
    visual_evidence = []
    if pdf_path.is_file():
        visual_evidence.append({"path": pdf_path.relative_to(output_dir).as_posix(),
                                "kind": "source_pdf", "allowed_pages": [p["page"] for p in pages]})
    for page in pages:
        image_path = pages_dir / "images" / f"page_{page['page']:03d}.png"
        if image_path.is_file():
            visual_evidence.append({"path": image_path.relative_to(output_dir).as_posix(),
                                    "kind": "page_image", "page": page["page"],
                                    "sha256": _file_sha(image_path)})
    manifest = {"schema_version": SCHEMA_VERSION, "workflow": "toc_structure_review",
                "source_fingerprint": fingerprint, "candidate_file": "toc_heading_candidates.json",
                "decisions_file": "toc_structure_decisions.json", "tree_file": "toc_tree.json",
                "page_source_kind": page_source_kind,
                "evidence_mode": evidence_mode,
                "visual_evidence": visual_evidence}
    return candidate_doc, manifest


def build_structure_review(output_dir: Path, pages_dir: Path, page_source_kind: str,
                           outline_path: Path, pdf_path: Path,
                           config: Any = None) -> dict[str, Any]:
    """Write candidate inventory and source-bound review manifest."""
    candidates, manifest = create_structure_review_snapshot(
        output_dir, pages_dir, page_source_kind, outline_path, pdf_path, config
    )
    atomic_write_json(output_dir / "toc_heading_candidates.json", candidates)
    atomic_write_json(output_dir / "toc_structure_review_manifest.json", manifest)
    return manifest


def _validate_page_coverage(coverage: Any, page_numbers: set[int], manifest: dict[str, Any]) -> list[str]:
    """Require explicit review coverage and evidence mode for each source page."""
    errors: list[str] = []
    if not isinstance(coverage, list):
        return ["page_coverage must be an array"]
    visual_evidence = manifest.get("visual_evidence", [])
    if not isinstance(visual_evidence, list):
        errors.append("review manifest visual_evidence must be an array")
        visual_evidence = []
    visual_pages: set[int] = set()
    for evidence in visual_evidence:
        if not isinstance(evidence, dict):
            continue
        if evidence.get("kind") == "source_pdf":
            allowed = evidence.get("allowed_pages", [])
            if isinstance(allowed, list):
                visual_pages.update(page for page in allowed if type(page) is int)
        elif evidence.get("kind") == "page_image" and type(evidence.get("page")) is int:
            visual_pages.add(evidence["page"])
    reviewed: list[int] = []
    for record in coverage:
        if not isinstance(record, dict) or type(record.get("page")) is not int or record.get("reviewed") is not True:
            errors.append("each page_coverage entry must explicitly mark an integer page reviewed")
            continue
        required_source = "visual" if record["page"] in visual_pages else "page_text"
        sources = record.get("evidence")
        if not isinstance(sources, list) or required_source not in sources:
            errors.append(f"page {record['page']} coverage must declare {required_source} evidence")
        reviewed.append(record["page"])
    if len(reviewed) != len(set(reviewed)):
        errors.append("duplicate page coverage entry")
    if set(reviewed) != page_numbers:
        errors.append("page coverage must include every available source page exactly once")
    return errors


def _index_node_decisions(records: list[Any]) -> dict[str, list[dict[str, Any]]]:
    """Index accepted node decisions once for deterministic node validation."""
    indexed: dict[str, list[dict[str, Any]]] = {}
    for record in records:
        if (isinstance(record, dict) and record.get("disposition") == "toc_node"
                and isinstance(record.get("node_id"), str)):
            indexed.setdefault(record["node_id"], []).append(record)
    return indexed


def _validate_review_artifact_headers(manifest: Any, candidates_doc: Any,
                                      decisions: Any) -> list[str]:
    """Check required artifact schemas and their top-level source binding."""
    errors: list[str] = []
    if not isinstance(manifest, dict) or type(manifest.get("schema_version")) is not int or manifest.get("schema_version") != SCHEMA_VERSION:
        return ["modern TOC structure review manifest is missing or unsupported"]
    if (not isinstance(candidates_doc, dict)
            or type(candidates_doc.get("schema_version")) is not int
            or candidates_doc.get("schema_version") != SCHEMA_VERSION
            or candidates_doc.get("source_fingerprint") != manifest.get("source_fingerprint")):
        errors.append("TOC candidate inventory is missing or does not match its manifest")
    if not isinstance(decisions, dict) or type(decisions.get("schema_version")) is not int or decisions.get("schema_version") != SCHEMA_VERSION:
        errors.append("toc_structure_decisions.json is missing or invalid")
    elif decisions.get("source_fingerprint") != manifest.get("source_fingerprint"):
        errors.append("TOC structure decisions are stale for current page/outline/evidence sources")
    return errors


def _validate_candidate_decisions_and_evidence(output_dir: Path, candidates: list[Any], decisions: dict[str, Any], allowed: dict[str, dict[str, Any]], valid_pages: dict[int, dict[str, Any]], manifest: dict[str, Any]) -> tuple[list[str], dict[str, str], list[Any], dict[str, list[dict[str, Any]]]]:
    errors: list[str] = []
    records = decisions.get("candidate_decisions")
    if not isinstance(records, list): records = []; errors.append("candidate_decisions must be an array")
    decisions_by_node = _index_node_decisions(records)
    seen: set[str] = set(); toc_links: dict[str, str] = {}
    for i, record in enumerate(records):
        if not isinstance(record, dict): errors.append(f"candidate_decisions[{i}] must be an object"); continue
        cid = record.get("candidate_id"); disp = record.get("disposition")
        if not isinstance(cid, str) or not cid: errors.append(f"candidate_decisions[{i}] has invalid candidate_id"); continue
        if cid in seen: errors.append(f"duplicate disposition for candidate {cid}")
        seen.add(cid)
        if cid not in allowed and not cid.startswith("reviewer-"): errors.append(f"unknown candidate_id: {cid}")
        if not isinstance(disp, str) or disp not in DISPOSITIONS:
            errors.append(f"invalid disposition for {cid}")
            continue
        if disp == "unresolved": errors.append(f"unresolved candidate: {cid}")
        if disp == "page_number" and (
            cid not in allowed or allowed[cid].get("kind") not in {"standalone_number", "explicit_page_label"}
        ):
            errors.append(f"page_number disposition {cid} must target one exact standalone numeral candidate")
        if disp == "toc_node":
            node_id = record.get("node_id")
            if not isinstance(node_id, str) or not node_id.strip(): errors.append(f"TOC candidate {cid} requires node_id")
            else:
                toc_links[cid] = node_id
            decision_binding = record.get("binding_mode", "visible")
            if not isinstance(decision_binding, str) or decision_binding not in {"visible", "container_only"}:
                errors.append(f"TOC candidate {cid} has invalid binding_mode")
            if decision_binding == "container_only" and (not isinstance(record.get("reason"), str) or not record["reason"].strip()):
                errors.append(f"container_only candidate {cid} requires an explicit reason")
        elif "node_id" in record: errors.append(f"non-TOC candidate {cid} must not specify node_id")
        if cid.startswith("reviewer-"):
            loc = record.get("evidence")
            excerpt = record.get("excerpt")
            if not isinstance(loc, dict) or type(loc.get("page")) is not int or type(loc.get("line")) is not int or not isinstance(excerpt, str) or not excerpt.strip():
                errors.append(f"reviewer candidate {cid} requires integer page/line and non-empty excerpt evidence")
            elif loc["page"] not in valid_pages or type(valid_pages[loc["page"]].get("line_count")) is not int or not 1 <= loc["line"] <= valid_pages[loc["page"]]["line_count"]:
                errors.append(f"reviewer candidate {cid} evidence location is outside the allowlist")
            else:
                page_record = valid_pages[loc["page"]]
                relative_source = page_record.get("path")
                if not isinstance(relative_source, str) or Path(relative_source).is_absolute() or ".." in Path(relative_source).parts:
                    errors.append(f"reviewer candidate {cid} page path is unsafe")
                    source_path = None
                else:
                    source_path = output_dir / relative_source
                try:
                    if source_path is None or not source_path.resolve().is_relative_to(output_dir.resolve()):
                        raise ValueError("page path escapes output directory")
                    source_line = source_path.read_text(encoding="utf-8").splitlines()[loc["line"] - 1].strip()
                except (OSError, IndexError, ValueError):
                    source_line = None
                expected = "reviewer-" + _sha(f"{loc['page']}:{loc['line']}:{excerpt.strip()}".encode())[:20]
                if cid != expected or source_line != excerpt.strip():
                    errors.append(f"reviewer candidate {cid} does not match its source location and excerpt")
        elif "evidence" in record and not (
            cid in allowed and allowed[cid].get("kind") == "layout_region" and disp == "toc_node"
        ):
            loc = record["evidence"]
            if not isinstance(loc, dict) or type(loc.get("page")) is not int or type(loc.get("line")) is not int:
                errors.append(f"candidate {cid} evidence must contain integer page and line")
            elif (loc["page"] not in valid_pages or type(valid_pages[loc["page"]].get("line_count")) is not int
                  or not 1 <= loc["line"] <= valid_pages[loc["page"]]["line_count"]):
                errors.append(f"candidate {cid} evidence is outside the page/line allowlist")
        if cid in allowed and allowed[cid].get("kind") == "layout_region" and disp == "toc_node":
            row = allowed[cid]
            loc = record.get("evidence")
            expected_evidence = {
                "kind": "layout_region", "page": row.get("page"),
                "block": row.get("block"), "source": row.get("source"),
                "bbox": row.get("bbox"),
            }
            if not isinstance(loc, dict) or any(loc.get(key) != value for key, value in expected_evidence.items()):
                errors.append(f"layout TOC candidate {cid} must copy its bounded layout evidence pointer")
            if record.get("node_id") == cid:
                insertion_line = loc.get("start_line") if isinstance(loc, dict) else None
                page = row.get("page")
                line_count = valid_pages.get(page, {}).get("line_count")
                if type(insertion_line) is not int or type(line_count) is not int or not 1 <= insertion_line <= line_count + 1:
                    errors.append(f"layout TOC candidate {cid} requires a valid evidence.start_line on its source page")
            elif "start_line" in loc:
                errors.append(f"supplemental layout evidence {cid} must not override the canonical node start line")
            if record.get("node_id") == cid and not isinstance(record.get("reason"), str):
                errors.append(f"layout-only TOC node {cid} requires an explicit reviewer reason")
            elif record.get("node_id") == cid and not record["reason"].strip():
                errors.append(f"layout-only TOC node {cid} requires an explicit reviewer reason")
            elif record.get("node_id") != cid and not isinstance(record.get("node_id"), str):
                errors.append(f"layout TOC node_id must equal its stable layout candidate_id: {cid}")
    missing = set(allowed) - seen
    if missing: errors.append(f"candidate disposition missing for {len(missing)} candidate(s)")
    expected_pages = set(valid_pages)
    errors.extend(_validate_page_coverage(decisions.get("page_coverage"), expected_pages, manifest))
    return errors, toc_links, records, decisions_by_node


def _validate_toc_node_source_correspondence(output_dir: Path, tree: Any, toc_links: dict[str, str], records: list[Any], decisions_by_node: dict[str, list[dict[str, Any]]], allowed: dict[str, dict[str, Any]], valid_pages: dict[int, dict[str, Any]]) -> list[str]:
    errors: list[str] = []
    if not isinstance(tree, dict) or type(tree.get("schema_version")) is not int or tree.get("schema_version") != TOC_TREE_SCHEMA_VERSION or not isinstance(tree.get("chapters"), list):
        errors.append("toc_tree.json is missing or invalid")
    else:
        errors.extend(validate_toc_node_identities(tree["chapters"]))
        node_ids = [n.get("node_id") for _, n in iter_toc_nodes(tree["chapters"])]
        if any(not isinstance(nid, str) or not nid.strip() for nid in node_ids):
            errors.append("every modern TOC node must have a non-empty stable node_id")
        valid_node_ids = [nid for nid in node_ids if isinstance(nid, str)]
        if set(valid_node_ids) != set(toc_links.values()):
            errors.append("tree node_id set must match accepted toc_node candidate decisions")
        starts: dict[tuple[int, int], str] = {}
        source_ranges: list[tuple[int, int, int, str, str]] = []
        for _, source_node in iter_toc_nodes(tree["chapters"]):
            source_id = source_node.get("node_id")
            source_page = source_node.get("start_page")
            source_boundary = source_node.get("boundary_info")
            source_line = source_boundary.get("start_line") if isinstance(source_boundary, dict) else None
            if type(source_page) is int and type(source_line) is int and isinstance(source_id, str):
                location = (source_page, source_line)
                prior = starts.get(location)
                if prior is not None and prior != source_id:
                    errors.append(
                        f"TOC nodes {prior} and {source_id} share source insertion position {source_page}/{source_line}"
                    )
                starts[location] = source_id
                action = source_boundary.get("heading_action") if isinstance(source_boundary, dict) else None
                end = source_boundary.get("heading_end_line") if isinstance(source_boundary, dict) else None
                if source_node.get("binding_mode", "visible") == "visible":
                    source_ranges.append((source_page, source_line, end if type(end) is int else source_line + 1,
                                          source_id, action or ""))
        for page in {record[0] for record in source_ranges}:
            page_ranges = sorted((record for record in source_ranges if record[0] == page), key=lambda item: (item[1], item[2]))
            for index, current in enumerate(page_ranges):
                for following in page_ranges[index + 1:]:
                    if current[4] == "insert":
                        overlap = current[1] == following[1] or (
                            following[4] == "bind" and following[1] <= current[1] < following[2]
                        )
                    elif following[4] == "insert":
                        overlap = current[1] <= following[1] < current[2]
                    else:
                        overlap = following[1] < current[2]
                    if overlap:
                        errors.append(f"reviewed heading source ranges overlap for TOC nodes {current[3]} and {following[3]}")
        candidate_by_id = allowed
        exact_text_ids = {
            cid for cid, row in candidate_by_id.items()
            if row.get("kind") in {"text_line", "standalone_number", "explicit_page_label"} and row.get("line") is not None
        }
        layout_ids = {
            cid for cid, row in candidate_by_id.items()
            if row.get("kind") == "layout_region"
        }
        for path, node in iter_toc_nodes(tree["chapters"]):
            node_id = node.get("node_id")
            if not isinstance(node_id, str): continue
            binding_mode = node.get("binding_mode", "visible")
            page = node.get("start_page")
            line = (node.get("boundary_info") or {}).get("start_line") if isinstance(node.get("boundary_info"), dict) else None
            matching_decisions = decisions_by_node.get(node_id, [])
            if node.get("navigation_visibility", "auto") != "auto" and not any(
                record.get("navigation_visibility") == node.get("navigation_visibility")
                for record in matching_decisions
            ):
                errors.append(f"TOC node {node_id} navigation_visibility requires a matching explicit structure decision")
            if node.get("numbering_label") is not None and not any(
                record.get("numbering_label") == node.get("numbering_label")
                and record.get("numbering_evidence") == node.get("numbering_evidence")
                for record in matching_decisions
            ):
                errors.append(f"TOC node {node_id} numbering_label and evidence must be copied into its structure decision")
            if not isinstance(binding_mode, str) or binding_mode not in {"visible", "container_only"}:
                errors.append(f"TOC node {node_id} has invalid binding_mode")
            node_boundary = node.get("boundary_info") if isinstance(node.get("boundary_info"), dict) else {}
            if binding_mode == "visible" and node_boundary.get("heading_action") not in {"bind", "insert"}:
                errors.append(f"visible TOC node {node_id} requires explicit heading_action")
            if binding_mode == "visible" and node_boundary.get("heading_action") == "bind":
                heading_start = node_boundary.get("start_line")
                heading_end = node_boundary.get("heading_end_line")
                page_record = valid_pages.get(page, {}) if type(page) is int else {}
                line_count = page_record.get("line_count")
                if (type(heading_start) is not int or type(heading_end) is not int
                        or type(line_count) is not int
                        or heading_start != line
                        or not 1 <= heading_start < heading_end <= line_count + 1):
                    errors.append(f"bound TOC node {node_id} requires a valid in-page exclusive heading range")
            if binding_mode == "visible" and node_boundary.get("heading_action") == "insert" and node_id not in layout_ids:
                errors.append(f"only reviewed layout evidence may authorize inserted heading TOC node {node_id}")
            has_heading_prefix = "heading_prefix" in node_boundary
            heading_prefix = node_boundary.get("heading_prefix")
            if binding_mode == "container_only" and has_heading_prefix:
                errors.append(f"container_only TOC node {node_id} cannot declare heading_prefix")
            if has_heading_prefix and node_id in layout_ids:
                errors.append(f"layout TOC node {node_id} cannot declare heading_prefix")
            if has_heading_prefix:
                if not isinstance(heading_prefix, str) or not heading_prefix.strip() or "\n" in heading_prefix or "\r" in heading_prefix:
                    errors.append(f"TOC node {node_id} heading_prefix must be a non-empty single-line prefix")
                elif type(page) is not int or type(line) is not int:
                    errors.append(f"TOC node {node_id} heading_prefix requires a source page and line")
                else:
                    page_record = valid_pages.get(page, {})
                    source_path = output_dir / str(page_record.get("path", ""))
                    try:
                        if not source_path.resolve().is_relative_to(output_dir.resolve()):
                            raise ValueError("page path escapes output directory")
                        source_line = source_path.read_text(encoding="utf-8").splitlines()[line - 1]
                    except (OSError, ValueError, IndexError):
                        source_line = None
                    normalized_source_line = re.sub(r"^\s*#{1,6}\s*", "", source_line) if source_line is not None else None
                    if normalized_source_line is None or not normalized_source_line.startswith(heading_prefix):
                        errors.append(f"TOC node {node_id} heading_prefix does not match its source line")
                    else:
                        from ..toc_model import compose_numbered_title
                        source_prefix = normalize_toc_source_label(heading_prefix, markdown_source=True)
                        bare_title = normalize_toc_source_label(node.get("title", ""), markdown_source=False)
                        numbering_label = node.get("numbering_label")
                        numbered_title = normalize_toc_source_label(
                            compose_numbered_title(str(node.get("title") or ""), numbering_label),
                            markdown_source=False,
                        ) if numbering_label else bare_title
                        if source_prefix not in {bare_title, numbered_title}:
                            errors.append(f"TOC node {node_id} heading_prefix visible text does not match its title")
            if node.get("numbering_label") is not None:
                evidence = node.get("numbering_evidence")
                if not isinstance(evidence, dict):
                    errors.append(f"TOC node {node_id} numbering_label requires numbering_evidence")
                else:
                    evidence_page, evidence_line = evidence.get("page"), evidence.get("line")
                    excerpt = evidence.get("excerpt")
                    page_record = valid_pages.get(evidence_page, {}) if type(evidence_page) is int else {}
                    source_path = output_dir / str(page_record.get("path", ""))
                    try:
                        if type(evidence_line) is not int or not source_path.resolve().is_relative_to(output_dir.resolve()):
                            raise ValueError("invalid evidence location")
                        evidence_text = source_path.read_text(encoding="utf-8").splitlines()[evidence_line - 1]
                    except (OSError, ValueError, IndexError):
                        evidence_text = None
                    if (evidence_text is None or not isinstance(excerpt, str) or excerpt not in evidence_text
                            or not 1 <= evidence_line <= int(page_record.get("line_count") or 0)
                            or not re.search(
                                rf"(?<!\w){re.escape(str(node.get('numbering_label')))}(?!\w)(?!\.\d)",
                                excerpt,
                                re.IGNORECASE,
                            )):
                        errors.append(f"TOC node {node_id} numbering_evidence does not support its label")
            if binding_mode == "container_only" and not any(
                isinstance(r.get("reason"), str) and r["reason"].strip()
                for r in matching_decisions
            ):
                errors.append(f"container_only TOC node {node_id} requires an explicit review reason")
            if any(r.get("binding_mode", "visible") != binding_mode for r in matching_decisions):
                errors.append(f"TOC node {node_id} binding_mode differs from its decision")
            exact = False
            canonical = candidate_by_id.get(node_id)
            if canonical and canonical.get("kind") != "pdf_outline" and type(page) is int and page == canonical.get("page") and type(line) is int and line == canonical.get("line"):
                exact = True
                if canonical.get("kind") in {"text_line", "standalone_number", "explicit_page_label"}:
                    page_record = valid_pages.get(page, {})
                    source_path = output_dir / str(page_record.get("path", ""))
                    try:
                        if not source_path.resolve().is_relative_to(output_dir.resolve()):
                            raise ValueError("page path escapes output directory")
                        source_lines = source_path.read_text(encoding="utf-8").splitlines()
                        # The reviewer may bind one visible TOC heading to a
                        # multi-line source range (for example, a title and
                        # subtitle on adjacent lines). The candidate inventory
                        # records the candidate's own extent; it does not cap
                        # the explicitly reviewed heading range.
                        boundary_info = node.get("boundary_info") if isinstance(node.get("boundary_info"), dict) else {}
                        reviewed_heading_end = boundary_info.get("heading_end_line")
                        region_end = (
                            reviewed_heading_end - 1
                            if boundary_info.get("heading_action") == "bind"
                            and type(reviewed_heading_end) is int
                            and reviewed_heading_end > line
                            else canonical.get("end_line", line)
                        )
                        candidate_end = canonical.get("end_line", line)
                        if type(candidate_end) is int and candidate_end > region_end:
                            errors.append(f"TOC node {node_id} reviewed heading range does not contain its accepted text candidate")
                        source_region = source_lines[line - 1:region_end]
                    except (OSError, ValueError, IndexError):
                        source_region = []
                    if source_region:
                        source_region = [re.sub(r"^\s*#{1,6}\s*", "", item) for item in source_region]
                    reviewed_prefix = (node.get("boundary_info") or {}).get("heading_prefix")
                    source_label = normalize_toc_source_label(
                        reviewed_prefix if isinstance(reviewed_prefix, str) else " ".join(source_region),
                        markdown_source=True,
                    )
                    tree_label = normalize_toc_source_label(node.get("title", ""), markdown_source=False)
                    numbering_label = node.get("numbering_label")
                    if numbering_label:
                        from ..toc_model import compose_numbered_title
                        numbered_label = normalize_toc_source_label(
                            compose_numbered_title(str(node.get("title") or ""), numbering_label),
                            markdown_source=False,
                        )
                    else:
                        numbered_label = tree_label
                    if not source_label or source_label not in {tree_label, numbered_label}:
                        errors.append(f"TOC node {node_id} title does not match its reviewed source region")
                    boundary_info = node.get("boundary_info") or {}
                    heading_end = boundary_info.get("heading_end_line") if isinstance(boundary_info, dict) else None
                    expected_heading_end = region_end + 1
                    if heading_end != expected_heading_end:
                        errors.append(f"TOC heading {node_id} has invalid heading_end_line")
            layout_decision = next((
                record for record in matching_decisions
                if record.get("candidate_id") == node_id
                and node_id in layout_ids
                and isinstance(record.get("evidence"), dict)
                and record["evidence"].get("kind") == "layout_region"
            ), None)
            if layout_decision is not None:
                layout_row = canonical if isinstance(canonical, dict) else {}
                node_boundary = node.get("boundary_info") if isinstance(node.get("boundary_info"), dict) else {}
                heading_action = node_boundary.get("heading_action")
                if heading_action not in {"bind", "insert"}:
                    errors.append(f"layout TOC node {node_id} requires explicit heading_action bind or insert")
                if heading_action == "insert" and node_boundary.get("heading_evidence_id") != node_id:
                    errors.append(f"inserted layout TOC node {node_id} must identify its reviewed layout evidence")
                if heading_action == "insert":
                    checked = node_boundary.get("heading_checked_range")
                    page_record = valid_pages.get(page, {})
                    line_count = page_record.get("line_count")
                    if (not isinstance(checked, dict) or type(checked.get("start_line")) is not int
                            or type(checked.get("end_line")) is not int or type(line_count) is not int
                            or not 1 <= checked["start_line"] < checked["end_line"] <= line_count + 1
                            or type(line) is not int or not checked["start_line"] <= line <= checked["end_line"]):
                        errors.append(f"inserted layout TOC node {node_id} requires an exact heading_checked_range")
                    else:
                        source_path = output_dir / str(page_record.get("path") or "")
                        try:
                            if not source_path.resolve().is_relative_to(output_dir.resolve()):
                                raise ValueError("page path escapes output directory")
                            page_lines = source_path.read_text(encoding="utf-8").splitlines()
                        except (OSError, ValueError):
                            page_lines = []
                        if source_range_matches_title(page_lines, checked["start_line"], checked["end_line"], node.get("title", ""), anchor_line=line):
                            errors.append(f"inserted layout TOC node {node_id} range already contains its title; bind that source range")
                if heading_action == "bind":
                    heading_end = node_boundary.get("heading_end_line")
                    if type(line) is not int or type(heading_end) is not int or heading_end <= line:
                        errors.append(f"bound layout TOC node {node_id} requires an explicit source heading range")
                    else:
                        source_path = output_dir / str(valid_pages.get(page, {}).get("path") or "")
                        try:
                            if not source_path.resolve().is_relative_to(output_dir.resolve()):
                                raise ValueError("page path escapes output directory")
                            raw_region = source_path.read_text(encoding="utf-8").splitlines()[line - 1:heading_end - 1]
                        except (OSError, ValueError):
                            raw_region = []
                        source_title = normalize_toc_source_label(
                            " ".join(re.sub(r"^\s*#{1,6}\s*", "", value) for value in raw_region),
                            markdown_source=True,
                        )
                        if source_title != normalize_toc_source_label(node.get("title", ""), markdown_source=False):
                            errors.append(f"bound layout TOC node {node_id} source range does not match its title")
                title_matches = (
                    normalize_toc_source_label(node.get("title", ""), markdown_source=False)
                    == normalize_toc_source_label(layout_row.get("text", ""), markdown_source=False)
                )
                exact = (
                    type(page) is int and page == layout_row.get("page")
                    and type(line) is int
                    and line == layout_decision["evidence"].get("start_line")
                    and title_matches
                )
                if not title_matches:
                    errors.append(f"layout TOC node {node_id} title does not match its reviewed layout region")
            if not exact:
                exact = any(
                    r.get("candidate_id") == node_id
                    and isinstance(r.get("evidence"), dict)
                    and r["evidence"].get("page") == page
                    and r["evidence"].get("line") == line
                    for r in records
                    if isinstance(r, dict)
                )
            reviewer_identity = any(
                r.get("candidate_id") == node_id
                and isinstance(r.get("evidence"), dict)
                and isinstance(r.get("excerpt"), str)
                for r in matching_decisions
            )
            if node_id not in exact_text_ids and node_id not in layout_ids and not reviewer_identity and not (binding_mode == "container_only" and any(r.get("node_id") == node_id and r.get("reason") for r in matching_decisions)):
                errors.append(f"TOC node {node_id} needs a stable text candidate ID or a reviewed container_only reason")
            if not exact:
                if binding_mode != "container_only":
                    errors.append(f"TOC node {node_id} start {page}/{line} does not match its accepted source candidate")
        page_number_locations = {
            (allowed[record["candidate_id"]].get("page"), allowed[record["candidate_id"]].get("line"))
            for record in records
            if isinstance(record, dict) and record.get("disposition") == "page_number"
            and record.get("candidate_id") in allowed
        }
        for _, node in iter_toc_nodes(tree["chapters"]):
            boundary = node.get("boundary_info") if isinstance(node.get("boundary_info"), dict) else {}
            if boundary.get("heading_action") != "bind":
                continue
            page = node.get("start_page")
            start = boundary.get("start_line")
            end = boundary.get("heading_end_line")
            if type(page) is int and type(start) is int and type(end) is int:
                if any(number_page == page and start <= number_line < end
                       for number_page, number_line in page_number_locations):
                    errors.append(f"page_number disposition conflicts with bound heading range for TOC node {node.get('node_id')}")
    return errors


def validate_structure_review(output_dir: Path, tree: Any = None,
                              *, expected_snapshot: tuple[dict[str, Any], dict[str, Any]]) -> list[str]:
    """Validate candidate coverage, decision bijection, freshness and tree IDs."""
    manifest = _json(output_dir / "toc_structure_review_manifest.json")
    candidates_doc = _json(output_dir / "toc_heading_candidates.json")
    decisions = _json(output_dir / "toc_structure_decisions.json")
    tree = tree if tree is not None else _json(output_dir / "toc_tree.json")
    errors = _validate_review_artifact_headers(manifest, candidates_doc, decisions)
    if not isinstance(manifest, dict):
        return errors
    if not isinstance(decisions, dict):
        return errors
    expected_candidates, expected_manifest = expected_snapshot
    if manifest != expected_manifest:
        errors.append("TOC review is stale: source pages, outline, OCR evidence, or mode changed; rerun refine-prepare")
    if candidates_doc != expected_candidates:
        errors.append("TOC candidate inventory differs from current source evidence; rerun refine-prepare and review")
    candidates = candidates_doc.get("candidates", []) if isinstance(candidates_doc, dict) else []
    if not isinstance(candidates, list):
        errors.append("TOC candidate inventory candidates must be an array")
        candidates = []
    page_rows = candidates_doc.get("pages", []) if isinstance(candidates_doc, dict) else []
    if not isinstance(page_rows, list):
        errors.append("TOC candidate inventory pages must be an array")
        page_rows = []
    if isinstance(candidates_doc, dict) and decisions.get("candidate_inventory_sha256") != candidates_doc.get("inventory_sha256"):
        errors.append("TOC decisions are not bound to the current candidate inventory")
    if isinstance(candidates_doc, dict):
        inventory = dict(candidates_doc); recorded = inventory.pop("inventory_sha256", None)
        actual = _sha(json.dumps(inventory, sort_keys=True, separators=(",", ":")).encode())
        if recorded != actual: errors.append("TOC candidate inventory content hash mismatch")
    allowed: dict[str, dict[str, Any]] = {}
    for candidate in candidates:
        if not isinstance(candidate, dict) or not isinstance(candidate.get("candidate_id"), str):
            errors.append("candidate inventory contains a malformed candidate ID")
            continue
        cid = candidate["candidate_id"]
        if cid in allowed:
            errors.append(f"candidate inventory repeats candidate ID {cid}")
        allowed[cid] = candidate
    valid_pages: dict[int, dict[str, Any]] = {}
    for page_record in page_rows:
        if not isinstance(page_record, dict) or type(page_record.get("page")) is not int:
            errors.append("candidate inventory contains malformed page metadata")
            continue
        valid_pages[page_record["page"]] = page_record
    for cid, candidate in allowed.items():
        page = candidate.get("page")
        line = candidate.get("line")
        if type(page) is not int or page not in valid_pages:
            errors.append(f"candidate {cid} references a page outside the source allowlist")
        elif type(valid_pages[page].get("line_count")) is not int:
            errors.append(f"page {page} has invalid line_count metadata")
        elif line is not None and (type(line) is not int or line < 1 or line > valid_pages[page]["line_count"]):
            errors.append(f"candidate {cid} references a line outside the source allowlist")
        end_line = candidate.get("end_line")
        if end_line is not None and (
            type(line) is not int or type(end_line) is not int
            or end_line < line or end_line > valid_pages.get(page, {}).get("line_count", 0)
        ):
            errors.append(f"candidate {cid} has an invalid heading-region end_line")
        if candidate.get("kind") == "layout_region":
            block = candidate.get("block")
            bbox = candidate.get("bbox")
            source = candidate.get("source")
            if type(block) is not int or block < 0:
                errors.append(f"layout candidate {cid} has an invalid block index")
            if not isinstance(source, str) or Path(source).is_absolute() or ".." in Path(source).parts:
                errors.append(f"layout candidate {cid} has an unsafe sidecar path")
            if (not isinstance(bbox, list) or len(bbox) != 4
                    or any(type(value) not in (int, float) or not 0 <= value <= 1 for value in bbox)):
                errors.append(f"layout candidate {cid} has an invalid normalized bounding box")
    decision_errors, toc_links, records, decisions_by_node = _validate_candidate_decisions_and_evidence(
        output_dir, candidates, decisions, allowed, valid_pages, manifest
    )
    errors.extend(decision_errors)
    errors.extend(_validate_toc_node_source_correspondence(
        output_dir, tree, toc_links, records, decisions_by_node, allowed, valid_pages
    ))
    return errors


def reviewed_page_number_lines(output_dir: Path) -> dict[int, set[int]]:
    """Project exact current standalone-number decisions into source coordinates.

    Call only after ``validate_structure_review`` succeeds. This function is
    deliberately strict so callers cannot silently skip malformed evidence.
    """
    candidates = _json(Path(output_dir) / "toc_heading_candidates.json")
    decisions = _json(Path(output_dir) / "toc_structure_decisions.json")
    if not isinstance(candidates, dict) or not isinstance(decisions, dict):
        raise ValueError("current TOC structure review artifacts are required for page-number projection")
    by_id = {row.get("candidate_id"): row for row in candidates.get("candidates", [])
             if isinstance(row, dict) and isinstance(row.get("candidate_id"), str)}
    projection: dict[int, set[int]] = {}
    seen: set[str] = set()
    for decision in decisions.get("candidate_decisions", []):
        if not isinstance(decision, dict):
            raise ValueError("malformed TOC structure decision in page-number projection")
        candidate_id = decision.get("candidate_id")
        if not isinstance(candidate_id, str) or candidate_id in seen:
            raise ValueError("duplicate or invalid candidate decision in page-number projection")
        seen.add(candidate_id)
        row = by_id.get(candidate_id)
        if row is None or decision.get("disposition") != "page_number":
            continue
        if (row.get("kind") not in {"standalone_number", "explicit_page_label"} or type(row.get("page")) is not int
                or type(row.get("line")) is not int):
            raise ValueError(f"page_number disposition is not bound to a standalone numbered source line: {candidate_id}")
        projection.setdefault(row["page"], set()).add(row["line"])
    return projection
