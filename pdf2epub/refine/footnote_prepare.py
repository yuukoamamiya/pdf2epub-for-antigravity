"""Prepare a compact, layout-aware footnote review hand-off.

The OCR backends already persist page layout sidecars next to the Markdown
view.  This module uses those sidecars to do the cheap part locally: identify
bottom-of-page footnote candidates, preserve their *within-page* order, and
create small review windows for only the ambiguous cases.  It deliberately
does not decide that a block is a continuation of a previous footnote; that
is the visual/semantic judgment reserved for the workspace Subagent.
"""

from __future__ import annotations

import hashlib
import html
import json
import re
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional

from ..ocr_consensus import (
    consensus_is_current,
    ocr_evidence_mode,
    secondary_ocr_enabled,
)
from ..workflow_contracts import atomic_write_text


FOOTNOTE_PREPARE_SCHEMA_VERSION = 1
DEFAULT_BOTTOM_RATIO = 0.64
DEFAULT_CONTEXT_BLOCKS = 2
_DECISION_ROLES = frozenset(
    {
        "body",
        "citation",
        "bibliography",
        "footnote_start",
        "footnote_continuation",
        "footnote_definition",
        "review_required",
    }
)
_FOOTNOTE_LABEL_RE = re.compile(r"(?:foot\s*note|footnote|注脚|脚注|页下注)", re.IGNORECASE)
_CITATION_LABEL_RE = re.compile(
    r"(?:bibliograph(?:y|ies)|reference(?:s)?|citation|参考文献|引用|文献)",
    re.IGNORECASE,
)
_FOOTNOTE_KEY_RE = re.compile(
    r"^\s*(?:<sup>\s*)?(?P<key>\d{1,4})(?:\s*</sup>)?\s*(?=\D|$)",
    re.IGNORECASE,
)
_NATIVE_NUMERIC_ONLY_RE = re.compile(r"^\s*\d{1,4}\s*$")
_NATIVE_KEY_START_RE = re.compile(r"^\s*(?P<key>\d{1,3})(?=\s|[.)、，:：])")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _text_from_block(block: Mapping[str, Any]) -> str:
    """Return a short plain-text view without changing the stored OCR."""
    value = block.get("text")
    if value is None:
        value = block.get("html", "")
    value = html.unescape(str(value or ""))
    value = re.sub(r"<[^>]+>", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def _vertices_to_bbox(value: Any) -> Optional[list[float]]:
    if not isinstance(value, (list, tuple)):
        return None
    if len(value) == 4 and all(isinstance(item, (int, float)) for item in value):
        x0, y0, x1, y1 = (float(item) for item in value)
        return [x0, y0, x1, y1] if x1 > x0 and y1 > y0 else None
    points = []
    for item in value:
        if isinstance(item, Mapping):
            x, y = item.get("x"), item.get("y")
        elif isinstance(item, (list, tuple)) and len(item) >= 2:
            x, y = item[0], item[1]
        else:
            continue
        if isinstance(x, (int, float)) and isinstance(y, (int, float)):
            points.append((float(x), float(y)))
    if len(points) < 2:
        return None
    xs = [point[0] for point in points]
    ys = [point[1] for point in points]
    bbox = [min(xs), min(ys), max(xs), max(ys)]
    return bbox if bbox[2] > bbox[0] and bbox[3] > bbox[1] else None


def _normalise_bbox(block: Mapping[str, Any], sidecar: Mapping[str, Any]) -> Optional[list[float]]:
    """Normalize Chandra ``bbox`` and Paddle ``box`` values to page ratios."""
    raw = block.get("bbox")
    if raw is None:
        raw = block.get("box")
    if raw is None:
        raw = block.get("bbox_px")
    bbox = _vertices_to_bbox(raw)
    if bbox is None:
        return None

    page_box = _vertices_to_bbox(sidecar.get("page_box"))
    if page_box is not None:
        page_width = page_box[2] - page_box[0]
        page_height = page_box[3] - page_box[1]
    else:
        page_width = page_height = 0

    coordinate_system = str(sidecar.get("coordinate_system") or "").strip().lower()
    # Native PDF extraction stores coordinates in the PDF page coordinate
    # system.  This check must happen before the historical ``<= 1000``
    # heuristic because a US Letter page is only 612x792 points.
    if coordinate_system in {"page", "page_points", "pdf_points", "points"}:
        if page_width <= 0 or page_height <= 0:
            return None
        return [
            max(0.0, min(1.0, (bbox[0] - page_box[0]) / page_width)),
            max(0.0, min(1.0, (bbox[1] - page_box[1]) / page_height)),
            max(0.0, min(1.0, (bbox[2] - page_box[0]) / page_width)),
            max(0.0, min(1.0, (bbox[3] - page_box[1]) / page_height)),
        ]

    # Chandra stores normalized 0..1000 coordinates.  Paddle commonly stores
    # pixel coordinates.  A sidecar with no page dimensions cannot be safely
    # normalized, so it is sent for review rather than guessed.
    if coordinate_system in {"normalized", "normalised", "ratio"} or max(bbox) <= 1000:
        return [max(0.0, min(1.0, value / 1000.0)) for value in bbox]
    if page_width > 0 and page_height > 0:
        return [
            max(0.0, min(1.0, (bbox[0] - page_box[0]) / page_width)),
            max(0.0, min(1.0, (bbox[1] - page_box[1]) / page_height)),
            max(0.0, min(1.0, (bbox[2] - page_box[0]) / page_width)),
            max(0.0, min(1.0, (bbox[3] - page_box[1]) / page_height)),
        ]
    return None


def _load_sidecar(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"Invalid OCR sidecar {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"OCR sidecar must be an object: {path}")
    blocks = value.get("blocks")
    if not isinstance(blocks, list):
        raise ValueError(f"OCR sidecar has no blocks array: {path}")
    return value


def _candidate_for_block(
    page_number: int,
    block_index: int,
    block: Mapping[str, Any],
    sidecar: Mapping[str, Any],
    *,
    bottom_ratio: float,
    force_review: bool = False,
) -> Optional[dict[str, Any]]:
    text = _text_from_block(block)
    if not text:
        return None
    label = str(block.get("label") or "").strip()
    label_is_footnote = bool(_FOOTNOTE_LABEL_RE.search(label))
    label_is_citation = bool(_CITATION_LABEL_RE.search(label))
    bbox = _normalise_bbox(block, sidecar)
    is_bottom = bool(bbox and bbox[1] >= bottom_ratio)
    key_match = _FOOTNOTE_KEY_RE.match(text)
    key = key_match.group("key") if key_match else None

    # A candidate must have either an explicit OCR footnote label or a strong
    # bottom-of-page + numbered-start signal.  Ordinary low page text is not
    # sent to the Subagent merely because it contains a number.
    # OCR can label bibliography/reference material as a bottom block too.
    # It is never a page footnote candidate: citations and bibliography are
    # semantic book content and must remain where the chapter puts them.
    if label_is_citation or (not label_is_footnote and not (is_bottom and key)):
        return None

    if label_is_footnote and is_bottom and key and not force_review:
        confidence = "high"
        disposition = "local_candidate"
    else:
        confidence = "review"
        disposition = "review_required"

    candidate = {
        "page": page_number,
        "block": block_index,
        "order": block.get("order", block_index),
        "label": label,
        "bbox": bbox,
        "key": key,
        "confidence": confidence,
        "disposition": disposition,
        "text": text[:240],
    }
    if force_review:
        candidate["review_reason"] = "ocr_consensus_visual_review"
    return candidate


def _window_for_page(
    page_number: int,
    sidecar: Mapping[str, Any],
    candidate_blocks: list[dict[str, Any]],
    *,
    context_blocks: int,
) -> dict[str, Any]:
    blocks = sidecar.get("blocks", [])
    candidate_indexes = {int(item["block"]) for item in candidate_blocks}
    indexes = set(candidate_indexes)
    for index in candidate_indexes:
        for offset in range(1, context_blocks + 1):
            if index - offset >= 0:
                indexes.add(index - offset)
            if index + offset < len(blocks):
                indexes.add(index + offset)

    compact_blocks = []
    for index in sorted(indexes):
        block = blocks[index]
        compact_blocks.append(
            {
                "block": index,
                "order": block.get("order", index),
                "label": str(block.get("label") or ""),
                "bbox": _normalise_bbox(block, sidecar),
                "text": _text_from_block(block)[:240],
                "candidate": index in candidate_indexes,
                "font_size": block.get("font_size"),
                "font_names": block.get("font_names"),
            }
        )
    return {"page": page_number, "blocks": compact_blocks}


def _build_cross_page_windows(
    page_reports: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Pair adjacent pages without assuming where the continuation appears.

    The next page's candidate blocks remain in their original visual order.
    This is important for the common ``body -> previous-note continuation ->
    new-note`` layout; the continuation is not incorrectly attached to the
    top of the next page.
    """
    windows = []
    by_page = {int(item["page"]): item for item in page_reports}
    for report in page_reports:
        page_number = int(report["page"])
        if not report["candidates"]:
            continue
        following = by_page.get(page_number + 1)
        if not following or not following["candidates"]:
            continue
        windows.append(
            {
                "pages": [page_number, page_number + 1],
                "left_candidates": report["candidates"],
                "right_candidates": following["candidates"],
                "reason": "adjacent_pages_contain_ordered_footnote_candidates",
            }
        )
    return windows


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


def _load_refined_units(output_dir: Path) -> list[dict[str, Any]]:
    progress_path = Path(output_dir) / "ocr_markdown" / "tree_progress.json"
    try:
        value = json.loads(progress_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return []
    units = value.get("units", []) if isinstance(value, dict) else []
    return [item for item in units if isinstance(item, dict)]


def _load_consensus_review_pages(output_dir: Path) -> set[int]:
    """Return pages whose two OCR results require visual review."""
    try:
        value = json.loads(
            (Path(output_dir) / "ocr_consensus.json").read_text(encoding="utf-8")
        )
    except (OSError, UnicodeError, json.JSONDecodeError):
        return set()
    records = value.get("records", {}) if isinstance(value, Mapping) else {}
    if not isinstance(records, Mapping):
        return set()
    pages: set[int] = set()
    for name, record in records.items():
        if not isinstance(record, Mapping) or record.get("action") != "visual_review":
            continue
        match = re.match(r"^page_(\d+)\.md$", str(name))
        if match:
            pages.add(int(match.group(1)))
    return pages


def _candidate_signature(candidates: Iterable[Mapping[str, Any]]) -> list[tuple[str, str]]:
    """Return layout-only evidence that is stable across OCR text errors."""
    result = []
    for candidate in candidates:
        label = str(candidate.get("label") or "").strip().casefold()
        key = str(candidate.get("key") or "")
        result.append((key, label))
    return sorted(result)


def _secondary_sidecar_path(output_dir: Path, sidecar_path: Path) -> Path:
    return Path(output_dir) / "ocr_secondary" / sidecar_path.name


def _validate_two_ocr_checkpoint(output_dir: Path, config: Mapping[str, Any]) -> None:
    """Require the secondary OCR checkpoint whenever the switch is enabled."""
    if not secondary_ocr_enabled(config):
        return
    if not consensus_is_current(output_dir, config):
        raise ValueError(
            "two-OCR footnote review requires a current ocr_consensus.json; "
            "rerun ocr-pages and ocr-correct-validate first"
        )


def _is_native_text_source(output_dir: Path) -> bool:
    """Return whether the current page set came from native PDF text."""
    try:
        progress = json.loads(
            (Path(output_dir) / "pages" / "ocr_progress.json").read_text(encoding="utf-8")
        )
    except (OSError, UnicodeError, json.JSONDecodeError):
        return False
    return isinstance(progress, Mapping) and progress.get("mode") == "native_text"


def footnote_evidence_mode(
    output_dir: Path,
    config: Optional[Mapping[str, Any]] = None,
) -> str:
    """Resolve the evidence mode for the current footnote layout source.

    Native text has its own trustworthy page layout evidence and deliberately
    bypasses visual OCR, even when a stale configuration enables a secondary
    OCR backend.  The report still uses the historical ``single_ocr`` value
    for compatibility with downstream checkpoint contracts.
    """
    if _is_native_text_source(output_dir):
        return "single_ocr"
    return ocr_evidence_mode(config)


def _candidate_for_native_block(
    page_number: int,
    block_index: int,
    block: Mapping[str, Any],
    sidecar: Mapping[str, Any],
    *,
    bottom_ratio: float,
) -> Optional[dict[str, Any]]:
    """Find conservative native-text candidates for workspace review.

    Native extraction has no semantic OCR labels.  A bottom numbered block is
    therefore always a review candidate rather than a locally accepted note.
    A page-number-only block is excluded because it is common page furniture.
    """
    text = _text_from_block(block)
    key_match = _NATIVE_KEY_START_RE.match(text)
    if not text or _NATIVE_NUMERIC_ONLY_RE.fullmatch(text) or key_match is None:
        return None
    try:
        font_size = float(block.get("font_size"))
        body_font_size = float(sidecar.get("body_font_size"))
    except (TypeError, ValueError):
        font_size = body_font_size = 0.0
    candidate = _candidate_for_block(
        page_number,
        block_index,
        block,
        sidecar,
        bottom_ratio=bottom_ratio,
        force_review=True,
    )
    if candidate is None:
        return None
    candidate["key"] = key_match.group("key")
    candidate["source_kind"] = "native_text"
    candidate["review_reason"] = "native_layout_candidate"
    if block.get("font_size") is not None:
        candidate["font_size"] = block.get("font_size")
    if font_size > 0 and body_font_size > 0:
        candidate["font_size_ratio"] = round(font_size / body_font_size, 3)
    if block.get("font_names"):
        candidate["font_names"] = block.get("font_names")
    return candidate


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
            atomic_write_text(context_path, json.dumps(context, ensure_ascii=False, indent=2))
            unit_context_files[name] = context_path.relative_to(output_dir).as_posix()
    return unit_context_files


def prepare_footnote_candidates(
    output_dir: Path,
    *,
    config: Optional[Mapping[str, Any]] = None,
    bottom_ratio: float = DEFAULT_BOTTOM_RATIO,
    context_blocks: int = DEFAULT_CONTEXT_BLOCKS,
) -> dict[str, Any]:
    """Create a compact, review-only footnote candidate report.

    No Markdown or translated file is changed.  High-confidence candidates
    are recorded for deterministic later materialization; only review windows
    are intended for a Subagent.  The function reads compact page-layout
    sidecars, not the full book text, which keeps the hand-off small. Visual
    OCR and native-text PDFs use source-specific candidate detectors but share
    the same decision contract.
    """
    output_dir = Path(output_dir)
    pages_dir = output_dir / "pages"
    sidecars = sorted(pages_dir.glob("page_*.ocr.json"))
    if not sidecars:
        raise ValueError(f"No page layout sidecars found in {pages_dir}")
    if not 0.5 <= float(bottom_ratio) < 1.0:
        raise ValueError("bottom_ratio must be between 0.5 and 1.0")
    try:
        context_blocks = max(0, int(context_blocks))
    except (TypeError, ValueError):
        context_blocks = DEFAULT_CONTEXT_BLOCKS

    native_text_source = _is_native_text_source(output_dir)
    source_kind = "native_text" if native_text_source else "ocr"
    evidence_mode = footnote_evidence_mode(output_dir, config)
    # The CLI always supplies config.  The config-less path is retained for
    # old library callers/tests; only that compatibility path may consult a
    # legacy consensus file.  A real single-OCR run must ignore stale
    # two-OCR artifacts completely.
    legacy_consensus = config is None
    if evidence_mode == "two_ocr":
        _validate_two_ocr_checkpoint(output_dir, config)

    page_reports: list[dict[str, Any]] = []
    sidecar_hashes: dict[str, str] = {}
    secondary_sidecar_hashes: dict[str, str] = {}
    consensus_review_pages = (
        _load_consensus_review_pages(output_dir)
        if not native_text_source and (legacy_consensus or evidence_mode == "two_ocr")
        else set()
    )
    review_pages: set[int] = set()
    high_confidence_count = 0
    review_count = 0

    for sidecar_path in sidecars:
        sidecar = _load_sidecar(sidecar_path)
        try:
            page_number = int(sidecar.get("page_number"))
        except (TypeError, ValueError) as exc:
            raise ValueError(f"OCR sidecar has invalid page_number: {sidecar_path}") from exc
        sidecar_hashes[sidecar_path.relative_to(output_dir).as_posix()] = _sha256(sidecar_path)
        candidates = []
        for block_index, block in enumerate(sidecar["blocks"]):
            if not isinstance(block, Mapping):
                continue
            if native_text_source:
                candidate = _candidate_for_native_block(
                    page_number,
                    block_index,
                    block,
                    sidecar,
                    bottom_ratio=float(bottom_ratio),
                )
            else:
                candidate = _candidate_for_block(
                    page_number,
                    block_index,
                    block,
                    sidecar,
                    bottom_ratio=float(bottom_ratio),
                    force_review=(
                        page_number in consensus_review_pages
                        if legacy_consensus
                        else False
                    ),
                )
            if candidate is None:
                continue
            candidates.append(candidate)
            if candidate["confidence"] == "high":
                high_confidence_count += 1
            else:
                review_count += 1
                review_pages.add(page_number)

        candidates.sort(key=lambda item: (item["order"], item["block"]))
        secondary_sidecar = _secondary_sidecar_path(output_dir, sidecar_path)
        secondary_candidates: list[dict[str, Any]] = []
        candidate_disagreement = False
        if evidence_mode == "two_ocr":
            secondary = _load_sidecar(secondary_sidecar)
            secondary_sidecar_hashes[
                secondary_sidecar.relative_to(output_dir).as_posix()
            ] = _sha256(secondary_sidecar)
            for block_index, block in enumerate(secondary["blocks"]):
                if not isinstance(block, Mapping):
                    continue
                candidate = _candidate_for_block(
                    page_number,
                    block_index,
                    block,
                    secondary,
                    bottom_ratio=float(bottom_ratio),
                )
                if candidate is not None:
                    secondary_candidates.append(candidate)
            secondary_candidates.sort(key=lambda item: (item["order"], item["block"]))
            candidate_disagreement = (
                bool(candidates) != bool(secondary_candidates)
                or _candidate_signature(candidates)
                != _candidate_signature(secondary_candidates)
                or page_number in consensus_review_pages
            )
            if candidate_disagreement:
                review_pages.add(page_number)
                for candidate in candidates:
                    if candidate.get("confidence") == "high":
                        high_confidence_count -= 1
                        review_count += 1
                    candidate["confidence"] = "review"
                    candidate["disposition"] = "review_required"
                    candidate["review_reason"] = (
                        "ocr_candidate_presence_differs"
                        if bool(candidates) != bool(secondary_candidates)
                        else "ocr_footnote_candidate_differs"
                    )

        should_emit_page = bool(candidates) or (
            evidence_mode == "two_ocr"
            and (bool(secondary_candidates) or page_number in consensus_review_pages)
        )
        if should_emit_page:
            legacy_secondary = legacy_consensus and secondary_sidecar.is_file()
            if candidate_disagreement and not candidates:
                # The secondary OCR found a possible note that the primary
                # OCR did not expose.  Keep the page in the review hand-off,
                # but do not manufacture a primary block address that the
                # deterministic apply stage could not safely remove.
                review_pages.add(page_number)
            page_reports.append(
                {
                    "page": page_number,
                    "sidecar": sidecar_path.relative_to(output_dir).as_posix(),
                    "secondary_sidecar": (
                        secondary_sidecar.relative_to(output_dir).as_posix()
                        if evidence_mode == "two_ocr" or legacy_secondary
                        else None
                    ),
                    "consensus_visual_review": page_number in consensus_review_pages,
                    "consensus_status": (
                        "disagree" if candidate_disagreement else "agree"
                    ) if evidence_mode == "two_ocr" else None,
                    "candidates": candidates,
                    "secondary_candidates": secondary_candidates,
                    "window": _window_for_page(
                        page_number,
                        sidecar,
                        candidates,
                        context_blocks=context_blocks,
                    ),
                    "secondary_window": _window_for_page(
                        page_number,
                        secondary,
                        secondary_candidates,
                        context_blocks=context_blocks,
                    ) if evidence_mode == "two_ocr" else None,
                }
            )

    page_reports.sort(key=lambda item: item["page"])
    cross_page_windows = _build_cross_page_windows(page_reports)
    for window in cross_page_windows:
        review_pages.update(int(page) for page in window["pages"])

    report = {
        "schema_version": FOOTNOTE_PREPARE_SCHEMA_VERSION,
        "source_dir": "pages",
        "source_kind": source_kind,
        "ocr_evidence_mode": evidence_mode,
        "bottom_ratio": float(bottom_ratio),
        "context_blocks": context_blocks,
        "consensus_visual_review_pages": sorted(consensus_review_pages),
        "sidecar_sha256": sidecar_hashes,
        "secondary_sidecar_sha256": secondary_sidecar_hashes,
        "pages": page_reports,
        "cross_page_windows": cross_page_windows,
        "review_pages": sorted(review_pages),
        "high_confidence_candidate_count": high_confidence_count,
        "review_candidate_count": review_count,
        "review_required": bool(review_pages),
    }
    report_path = output_dir / "footnote_candidates.json"
    atomic_write_text(report_path, json.dumps(report, ensure_ascii=False, indent=2))
    return report


def prepare_footnote_subagent(
    output_dir: Path,
    *,
    book_title: str,
    config: Optional[Mapping[str, Any]] = None,
    bottom_ratio: float = DEFAULT_BOTTOM_RATIO,
    context_blocks: int = DEFAULT_CONTEXT_BLOCKS,
) -> dict[str, Path]:
    """Write the compact Subagent hand-off for ambiguous layout windows."""
    output_dir = Path(output_dir)
    report = prepare_footnote_candidates(
        output_dir,
        config=config,
        bottom_ratio=bottom_ratio,
        context_blocks=context_blocks,
    )
    unit_context_files = _write_unit_contexts(output_dir, report)
    manifest = {
        "schema_version": FOOTNOTE_PREPARE_SCHEMA_VERSION,
        "task": "footnote-prepare",
        "book_title": book_title,
        "source_kind": report["source_kind"],
        "ocr_evidence_mode": report["ocr_evidence_mode"],
        "candidate_report": "footnote_candidates.json",
        "review_pages": report["review_pages"],
        "high_confidence_candidate_count": report["high_confidence_candidate_count"],
        "review_candidate_count": report["review_candidate_count"],
        "cross_page_window_count": len(report["cross_page_windows"]),
        "consensus_visual_review_pages": report["consensus_visual_review_pages"],
        "unit_context_files": unit_context_files,
        "decision_file": "footnote_decisions.json",
        "status": "pending_review" if report["review_required"] else "no_subagent_review_required",
    }
    manifest_path = output_dir / "footnote_subagent_manifest.json"
    atomic_write_text(manifest_path, json.dumps(manifest, ensure_ascii=False, indent=2))

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
        page_lines.append(line)
    page_text = "\n".join(page_lines) or "- 无需 Subagent 复核；仅保留本地高置信度候选。"
    source_guidance = (
        "For native-text pages, use the recorded coordinates and font metadata as layout evidence. "
        "A bottom numbered block is only a candidate, not an automatic footnote; distinguish it from "
        "citations, page furniture, and ordinary numbered prose."
        if report["source_kind"] == "native_text"
        else "If a page is marked as an OCR-consensus visual review, treat the local label as untrusted even when it says `Footnote`; compare the primary and secondary sidecars and the page image before deciding."
    )
    prompt = f"""# Footnote layout review\n\nBook: {book_title}\n\nRead `footnote_subagent_manifest.json` and `footnote_candidates.json`. The local script has selected compact bottom-of-page candidate windows. Review only the listed windows; do not reread or rewrite the whole book. {source_guidance}\n\nCandidate page layout sidecars:\n{page_text}\n\nUse these meanings strictly:\n- `footnote_start`: the beginning of a real page footnote; it will be moved to the end of its logical chapter.\n- `footnote_continuation`: text continuing a real footnote from an earlier page; it will be joined to that footnote.\n- `footnote_definition`: a complete footnote definition already presented as a note block.\n- `citation`: an in-text citation, quoted source, parenthetical/numeric reference, or other scholarly reference; it must stay in the body and must never be moved.\n- `bibliography`: a reference-list/bibliography entry; it must stay in place and must never be moved.\n- `body`: ordinary prose or an uncertain block that is not a footnote.\n\nFor each candidate block, assign exactly one role. Keep the block's page and block number. For `footnote_start` and `footnote_definition`, copy the visible numeric key when present. For `footnote_continuation`, provide the key of the footnote it continues. A continuation may appear after ordinary body blocks on the next page; preserve visual order and attach it only when the page image/layout supports that decision. Do not assume that a next-page footnote continuation is at the top of the page. A numeric marker alone is not enough to call something a footnote: if it is a citation or reference, use `citation` or `bibliography`.\n\nWrite only valid JSON to `footnote_decisions.json` with this shape:\n\n```json\n{{\n  "schema_version": {FOOTNOTE_PREPARE_SCHEMA_VERSION},\n  "decisions": [\n    {{\n      "page": 125,\n      "block": 8,\n      "role": "footnote_continuation",\n      "key": "36",\n      "confidence": "high"\n    }}\n  ]\n}}\n```\n\nIf a candidate is genuinely ambiguous, use `role: "review_required"` and explain it in `reason`; do not guess.\n"""
    prompt += "\n\nDo not reread or rewrite the whole book.\n"
    prompt_path = output_dir / "footnote_subagent_prompt.md"
    atomic_write_text(prompt_path, prompt)
    return {
        "report": output_dir / "footnote_candidates.json",
        "manifest": manifest_path,
        "prompt": prompt_path,
    }


def _candidate_index(report: Mapping[str, Any]) -> dict[tuple[int, int], dict[str, Any]]:
    index = {}
    for page in report.get("pages", []) or []:
        if not isinstance(page, Mapping):
            continue
        for candidate in page.get("candidates", []) or []:
            if not isinstance(candidate, Mapping):
                continue
            try:
                address = (int(candidate["page"]), int(candidate["block"]))
            except (KeyError, TypeError, ValueError):
                continue
            index[address] = dict(candidate)
    return index


def validate_footnote_decisions(
    output_dir: Path,
    *,
    config: Optional[Mapping[str, Any]] = None,
) -> dict[str, Any]:
    """Validate the small JSON decision file produced by the Subagent.

    This validator checks references to candidate block addresses and basic
    continuation provenance.  It never infers a missing decision and never
    turns an unresolved visual case into an automatic acceptance.
    """
    output_dir = Path(output_dir)
    report_path = output_dir / "footnote_candidates.json"
    decision_path = output_dir / "footnote_decisions.json"
    try:
        report = json.loads(report_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        result = {"valid": False, "status": "retry_required", "errors": [f"invalid candidate report: {exc}"]}
        atomic_write_text(
            output_dir / "footnote_decision_validation.json",
            json.dumps(result, ensure_ascii=False, indent=2),
        )
        return result
    if not isinstance(report, dict):
        result = {"valid": False, "status": "retry_required", "errors": ["candidate report must be an object"]}
        atomic_write_text(
            output_dir / "footnote_decision_validation.json",
            json.dumps(result, ensure_ascii=False, indent=2),
        )
        return result

    source_kind = str(report.get("source_kind") or "ocr")
    evidence_mode = str(report.get("ocr_evidence_mode") or "single_ocr")
    errors: list[str] = []
    if source_kind not in {"ocr", "native_text"}:
        errors.append("candidate report has an unsupported page source kind")
    if evidence_mode not in {"single_ocr", "two_ocr"}:
        errors.append("candidate report has an unsupported OCR evidence mode")
    if config is not None:
        configured_mode = footnote_evidence_mode(output_dir, config)
        if evidence_mode != configured_mode:
            errors.append(
                f"candidate report was prepared in {evidence_mode}, but configuration requires {configured_mode}"
            )
        if configured_mode == "two_ocr":
            try:
                _validate_two_ocr_checkpoint(output_dir, config)
            except ValueError as exc:
                errors.append(str(exc))

    def _hashes_current(field: str) -> bool:
        expected_hashes = report.get(field, {})
        if not isinstance(expected_hashes, Mapping):
            errors.append(f"candidate report field {field} must be an object")
            return False
        current_hashes: dict[str, str] = {}
        for relative, expected in expected_hashes.items():
            path = output_dir / str(relative)
            if path.is_file():
                current_hashes[str(relative)] = _sha256(path)
        if current_hashes != dict(expected_hashes):
            errors.append(f"one or more {field} files changed or disappeared")
            return False
        return True

    _hashes_current("sidecar_sha256")
    secondary_hashes = report.get("secondary_sidecar_sha256", {})
    if evidence_mode == "two_ocr":
        _hashes_current("secondary_sidecar_sha256")
    elif secondary_hashes:
        errors.append("single-OCR candidate report unexpectedly contains secondary OCR files")

    if errors:
        result = {
            "schema_version": FOOTNOTE_PREPARE_SCHEMA_VERSION,
            "source_kind": source_kind,
            "ocr_evidence_mode": evidence_mode,
            "valid": False,
            "status": "retry_required",
            "errors": errors,
            "decisions": [],
        }
        atomic_write_text(
            output_dir / "footnote_decision_validation.json",
            json.dumps(result, ensure_ascii=False, indent=2),
        )
        return result

    required = {
        (int(candidate["page"]), int(candidate["block"]))
        for page in report.get("pages", []) or []
        for candidate in page.get("candidates", []) or []
        if candidate.get("confidence") == "review"
    }
    if not required:
        result = {
            "schema_version": FOOTNOTE_PREPARE_SCHEMA_VERSION,
            "source_kind": source_kind,
            "ocr_evidence_mode": evidence_mode,
            "valid": True,
            "status": "no_subagent_review_required",
            "errors": [],
            "decisions": [],
        }
        atomic_write_text(
            output_dir / "footnote_decision_validation.json",
            json.dumps(result, ensure_ascii=False, indent=2),
        )
        report["decisions"] = []
        report["decisions_status"] = "validated"
        atomic_write_text(report_path, json.dumps(report, ensure_ascii=False, indent=2))
        _write_unit_contexts(output_dir, report)
        return result

    try:
        decisions_data = json.loads(decision_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        result = {"valid": False, "status": "retry_required", "errors": [f"invalid decision file: {exc}"]}
        atomic_write_text(
            output_dir / "footnote_decision_validation.json",
            json.dumps(result, ensure_ascii=False, indent=2),
        )
        return result
    decisions = decisions_data.get("decisions") if isinstance(decisions_data, dict) else None
    human_review_required: list[str] = []
    if not isinstance(decisions_data, dict) or decisions_data.get("schema_version") != FOOTNOTE_PREPARE_SCHEMA_VERSION:
        errors.append("decision file has an unsupported schema_version")
    if not isinstance(decisions, list):
        errors.append("decision file must contain a decisions array")
        decisions = []

    candidates = _candidate_index(report)
    seen: set[tuple[int, int]] = set()
    normalized: list[dict[str, Any]] = []
    for item in decisions:
        if not isinstance(item, Mapping):
            errors.append("each decision must be an object")
            continue
        try:
            address = (int(item["page"]), int(item["block"]))
        except (KeyError, TypeError, ValueError):
            errors.append("decision is missing an integer page/block address")
            continue
        if address in seen:
            errors.append(f"duplicate decision for page {address[0]} block {address[1]}")
            continue
        seen.add(address)
        candidate = candidates.get(address)
        if candidate is None:
            errors.append(f"decision points to a non-candidate block: {address[0]}:{address[1]}")
            continue
        role = str(item.get("role") or "").strip()
        if role not in _DECISION_ROLES:
            errors.append(f"unsupported decision role for {address[0]}:{address[1]}: {role}")
            continue
        key = item.get("key")
        if key is not None:
            key = str(key).strip()
            if not re.fullmatch(r"[A-Za-z0-9_-]+", key):
                errors.append(f"invalid footnote key for {address[0]}:{address[1]}")
                continue
        if role in {"footnote_start", "footnote_continuation", "footnote_definition"} and not key:
            errors.append(f"footnote decision is missing a key for {address[0]}:{address[1]}")
            continue
        if candidate.get("key") and role in {"footnote_start", "footnote_definition"} and key != candidate["key"]:
            errors.append(
                f"decision key does not match OCR key for {address[0]}:{address[1]}"
            )
            continue
        decision = dict(item)
        decision["page"], decision["block"], decision["role"] = address[0], address[1], role
        if key is not None:
            decision["key"] = key
        if role == "review_required":
            human_review_required.append(f"{address[0]}:{address[1]}")
        normalized.append(decision)

    missing = sorted(required - seen)
    errors.extend(f"missing decision for review candidate {page}:{block}" for page, block in missing)

    key_sources = {
        str(candidate.get("key"))
        for candidate in candidates.values()
        if candidate.get("key")
    }
    key_sources.update(
        str(item.get("key"))
        for item in normalized
        if item.get("role") in {"footnote_start", "footnote_definition"} and item.get("key")
    )
    for item in normalized:
        if item.get("role") != "footnote_continuation" or not item.get("key"):
            continue
        if str(item["key"]) not in key_sources:
            errors.append(
                f"continuation {item['page']}:{item['block']} has no traceable footnote key"
            )

    if human_review_required:
        status = "human_review_required"
    elif errors:
        status = "retry_required"
    else:
        status = "validated"
    result = {
        "schema_version": FOOTNOTE_PREPARE_SCHEMA_VERSION,
        "source_kind": source_kind,
        "ocr_evidence_mode": evidence_mode,
        "valid": not errors and not human_review_required,
        "status": status,
        "errors": errors,
        "human_review_required": human_review_required,
        "decisions": normalized,
    }
    atomic_write_text(
        output_dir / "footnote_decision_validation.json",
        json.dumps(result, ensure_ascii=False, indent=2),
    )
    if result["valid"]:
        report["decisions"] = normalized
        report["decisions_status"] = "validated"
        atomic_write_text(report_path, json.dumps(report, ensure_ascii=False, indent=2))
        _write_unit_contexts(output_dir, report)
    return result


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
    "DEFAULT_BOTTOM_RATIO",
    "FOOTNOTE_PREPARE_SCHEMA_VERSION",
    "footnote_evidence_mode",
    "load_footnote_unit_contexts",
    "prepare_footnote_candidates",
    "prepare_footnote_subagent",
    "validate_footnote_decisions",
]
