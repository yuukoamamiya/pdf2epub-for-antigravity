"""Exact, hash-bound page sidecar inventory for footnote handoffs."""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Mapping

from ..workflow_contracts import sha256_file


_PAGE = re.compile(r"page_(\d+)\.ocr\.json")


def index_sidecar_paths(inventory: Mapping[str, Any], source_root: str) -> dict[int, str]:
    """Parse one source inventory into exact numeric page identities, without I/O."""
    if source_root not in {"pages", "ocr_secondary"}:
        raise ValueError(f"invalid OCR sidecar source root: {source_root}")
    result: dict[int, str] = {}
    prefix = f"{source_root}/"
    for raw_path in inventory:
        rel = str(raw_path).replace("\\", "/")
        match = _PAGE.fullmatch(rel.removeprefix(prefix))
        if not rel.startswith(prefix) or not match:
            raise ValueError(f"sidecar has an invalid {source_root} source mapping: {rel}")
        page = int(match.group(1))
        if page < 1 or page in result:
            raise ValueError(f"duplicate or invalid {source_root} sidecar page: {page}")
        result[page] = rel
    return result


def resolve_page_sidecar_path(inventory: Mapping[str, Any], page: int, source_root: str) -> str | None:
    """Resolve a single exact page/source path; no substring matching or I/O."""
    return index_sidecar_paths(inventory, source_root).get(int(page))


def page_sidecar_index(output_dir: Path, report: Mapping[str, Any]) -> dict[int, dict[str, Any]]:
    """Index primary and secondary sidecars by exact page identity and verify bytes."""
    root = Path(output_dir).resolve()
    mode = report.get("ocr_evidence_mode")
    if mode not in {"single_ocr", "two_ocr"}:
        raise ValueError(f"invalid footnote OCR evidence mode: {mode}")
    inventories = [("primary", report.get("sidecar_sha256", {}))]
    if mode == "two_ocr":
        inventories.append(("secondary", report.get("secondary_sidecar_sha256", {})))
    index: dict[int, dict[str, Any]] = {}
    for source, inventory in inventories:
        if not isinstance(inventory, Mapping):
            raise ValueError(f"footnote {source} sidecar hash inventory is missing")
        source_root = "pages" if source == "primary" else "ocr_secondary"
        source_paths = index_sidecar_paths(inventory, source_root)
        normalized_hashes = {str(name).replace("\\", "/"): value for name, value in inventory.items()}
        for page, rel in source_paths.items():
            expected_hash = normalized_hashes[rel]
            path = (root / rel).resolve()
            try:
                path.relative_to(root)
            except ValueError as exc:
                raise ValueError(f"footnote sidecar escapes output directory: {rel}") from exc
            if not path.is_file() or not isinstance(expected_hash, str) or sha256_file(path) != expected_hash:
                raise ValueError(f"footnote {source} sidecar missing or changed for page {page}: {rel}")
            record = index.setdefault(page, {})
            record[source] = {"path": rel, "sha256": expected_hash}
    primary_pages = {page for page, item in index.items() if "primary" in item}
    secondary_pages = {page for page, item in index.items() if "secondary" in item}
    if not primary_pages:
        raise ValueError("footnote primary sidecar inventory is empty")
    if mode == "two_ocr" and primary_pages != secondary_pages:
        missing = sorted(primary_pages - secondary_pages)
        orphan = sorted(secondary_pages - primary_pages)
        raise ValueError(f"two-OCR sidecar page inventories differ; missing secondary={missing[:10]}, orphan secondary={orphan[:10]}")
    reported_pages = {int(row.get("page", 0)) for row in report.get("pages", []) if isinstance(row, Mapping)}
    for page in reported_pages:
        if page < 1 or page not in primary_pages:
            raise ValueError(f"footnote primary sidecar missing for reported page {page}")
        if mode == "two_ocr" and page not in secondary_pages:
            raise ValueError(f"footnote secondary sidecar missing for reported page {page}")
    return index


__all__ = ["index_sidecar_paths", "page_sidecar_index", "resolve_page_sidecar_path"]
