"""Run PP-DocLayout-L as independent page-layout evidence.

The output is intentionally separate from OCR page sidecars and from the
secondary OCR consensus.  It records only model boxes, scores, page geometry,
and provenance; later structure stages decide how those boxes affect content.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Mapping, Optional

import pymupdf as fitz
from loguru import logger

from .ocr.backends.paddle_layout import close_client, init_client
from .workflow_contracts import atomic_write_text, sha256_file


LAYOUT_DETECTION_SCHEMA_VERSION = 1
DEFAULT_LAYOUT_DPI = 192


def layout_config(config: Mapping[str, Any] | None) -> dict[str, Any]:
    ocr = config.get("ocr", {}) if isinstance(config, Mapping) else {}
    settings = ocr.get("layout", {}) if isinstance(ocr, Mapping) else {}
    return dict(settings) if isinstance(settings, Mapping) else {}


def layout_enabled(config: Mapping[str, Any] | None) -> bool:
    settings = layout_config(config)
    return bool(settings.get("enabled", False))


def _config_hash(config: Mapping[str, Any] | None) -> str:
    settings = layout_config(config)
    payload = json.dumps(settings, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def layout_config_sha256(config: Mapping[str, Any] | None) -> str:
    """Stable hash of the layout-detector configuration."""
    return _config_hash(config)


def _dpi(config: Mapping[str, Any] | None) -> int:
    value = layout_config(config).get("dpi", DEFAULT_LAYOUT_DPI)
    try:
        return max(72, int(value))
    except (TypeError, ValueError):
        return DEFAULT_LAYOUT_DPI


def _render_page(pdf: fitz.Document, page_number: int, dpi: int) -> bytes:
    page = pdf[page_number - 1]
    zoom = float(dpi) / 72.0
    pixmap = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), alpha=False)
    return pixmap.tobytes("png")


def _atomic_json(path: Path, value: Mapping[str, Any]) -> None:
    atomic_write_text(path, json.dumps(value, ensure_ascii=False, indent=2))


def _manifest_path(output_dir: Path) -> Path:
    return Path(output_dir) / "layout_detection_manifest.json"


def _prediction_path(output_dir: Path, page_number: int) -> Path:
    return Path(output_dir) / "layout_detection" / f"page_{page_number:03d}.json"


def _load_manifest(output_dir: Path) -> dict[str, Any]:
    try:
        value = json.loads(_manifest_path(output_dir).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _page_prediction_is_current(
    output_dir: Path,
    page_number: int,
    *,
    source_sha256: str,
    config_sha256: str,
) -> bool:
    manifest = _load_manifest(output_dir)
    if (
        manifest.get("schema_version") != LAYOUT_DETECTION_SCHEMA_VERSION
        or manifest.get("source_pdf_sha256") != source_sha256
        or manifest.get("layout_config_sha256") != config_sha256
        or manifest.get("status") not in {"running", "complete"}
    ):
        return False
    path = _prediction_path(output_dir, page_number)
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return False
    return (
        isinstance(value, dict)
        and value.get("page_number") == page_number
        and value.get("source_pdf_sha256") == source_sha256
        and value.get("layout_config_sha256") == config_sha256
        and isinstance(value.get("boxes"), list)
    )


def run_layout_detection(
    pdf_path: Path,
    output_dir: Path,
    *,
    config: Mapping[str, Any],
    start_page: int = 1,
    end_page: Optional[int] = None,
    resume: bool = False,
) -> dict[str, Any]:
    """Generate current PP-DocLayout predictions for a page range."""
    output_dir = Path(output_dir)
    pdf_path = Path(pdf_path)
    settings = layout_config(config)
    if not bool(settings.get("enabled", False)):
        report = {
            "schema_version": LAYOUT_DETECTION_SCHEMA_VERSION,
            "status": "disabled",
            "backend": "pp_doclayout",
        }
        _atomic_json(_manifest_path(output_dir), report)
        return report
    backend = str(settings.get("backend") or "pp_doclayout").strip().lower()
    if backend != "pp_doclayout":
        raise ValueError(
            "ocr.layout.backend must be 'pp_doclayout' for the local PP-DocLayout worker"
        )

    output_dir.mkdir(parents=True, exist_ok=True)
    source_sha256 = sha256_file(pdf_path)
    config_sha256 = _config_hash(config)
    dpi = _dpi(config)
    with fitz.open(pdf_path) as pdf:
        total_pages = len(pdf)
        if total_pages <= 0:
            raise ValueError(f"PDF has no pages: {pdf_path}")
        first = max(1, int(start_page))
        last = total_pages if end_page is None else min(total_pages, int(end_page))
        if first > last:
            raise ValueError(f"Invalid layout page range: {first}-{last}")

        prediction_dir = output_dir / "layout_detection"
        prediction_dir.mkdir(parents=True, exist_ok=True)
        requested_pages = list(range(first, last + 1))
        pages_to_process = [
            page
            for page in requested_pages
            if not (
                resume
                and _page_prediction_is_current(
                    output_dir,
                    page,
                    source_sha256=source_sha256,
                    config_sha256=config_sha256,
                )
            )
        ]

        manifest = {
            "schema_version": LAYOUT_DETECTION_SCHEMA_VERSION,
            "status": "running",
            "backend": "pp_doclayout",
            "model_name": settings.get("model_name") or "PP-DocLayout-L",
            "source_pdf": pdf_path.name,
            "source_pdf_sha256": source_sha256,
            "layout_config_sha256": config_sha256,
            "device": settings.get("device") or "gpu:0",
            "dpi": dpi,
            "total_pages": total_pages,
            "requested_pages": requested_pages,
            "scope": "full_book"
            if first == 1 and last == total_pages
            else "page_range",
            "scope_complete": False,
            "processed_pages": [],
            "failed_pages": [],
        }
        _atomic_json(_manifest_path(output_dir), manifest)

        if not pages_to_process:
            manifest["status"] = "complete"
            manifest["processed_pages"] = requested_pages
            manifest["scope_complete"] = True
            manifest["complete"] = manifest["scope"] == "full_book"
            _atomic_json(_manifest_path(output_dir), manifest)
            return manifest

        client = None
        failed_pages: list[int] = []
        try:
            client = init_client(config)
            manifest["diagnostics"] = dict(getattr(client, "diagnostics", {}))
            _atomic_json(_manifest_path(output_dir), manifest)
            for page_number in pages_to_process:
                try:
                    image_bytes = _render_page(pdf, page_number, dpi)
                    result = client.predict(image_bytes)
                    page_box = result.get("page_box")
                    boxes = result.get("boxes")
                    if not isinstance(page_box, list) or not isinstance(boxes, list):
                        raise RuntimeError("PP-DocLayout returned an invalid page result")
                    prediction = {
                        "schema_version": LAYOUT_DETECTION_SCHEMA_VERSION,
                        "page_number": page_number,
                        "backend": "pp_doclayout",
                        "model_name": manifest["model_name"],
                        "source_pdf_sha256": source_sha256,
                        "layout_config_sha256": config_sha256,
                        "coordinate_system": "pixels",
                        "page_box": page_box,
                        "boxes": boxes,
                    }
                    _atomic_json(_prediction_path(output_dir, page_number), prediction)
                    manifest["processed_pages"] = sorted(
                        set(manifest["processed_pages"]) | {page_number}
                    )
                    _atomic_json(_manifest_path(output_dir), manifest)
                    logger.info(
                        "PP-DocLayout page {}/{}: {} region(s)",
                        page_number,
                        total_pages,
                        len(boxes),
                    )
                except Exception as exc:
                    failed_pages.append(page_number)
                    logger.error("PP-DocLayout failed on page {}: {}", page_number, exc)
        finally:
            if client is not None:
                close_client(client)

        manifest["processed_pages"] = sorted(
            page for page in requested_pages
            if _page_prediction_is_current(
                output_dir,
                page,
                source_sha256=source_sha256,
                config_sha256=config_sha256,
            )
        )
        manifest["failed_pages"] = sorted(failed_pages)
        manifest["status"] = "complete" if not failed_pages else "failed"
        manifest["scope_complete"] = not failed_pages and all(
            _page_prediction_is_current(
                output_dir,
                page,
                source_sha256=source_sha256,
                config_sha256=config_sha256,
            )
            for page in requested_pages
        )
        manifest["complete"] = (
            manifest["scope"] == "full_book" and manifest["scope_complete"]
        )
        if not manifest["scope_complete"] and not failed_pages:
            manifest["status"] = "incomplete"
        _atomic_json(_manifest_path(output_dir), manifest)
        return manifest


def load_layout_prediction(output_dir: Path, page_number: int) -> dict[str, Any] | None:
    """Load one page prediction without treating stale output as evidence."""
    try:
        value = json.loads(
            _prediction_path(Path(output_dir), int(page_number)).read_text(encoding="utf-8")
        )
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError):
        return None
    return value if isinstance(value, dict) else None


__all__ = [
    "LAYOUT_DETECTION_SCHEMA_VERSION",
    "layout_config",
    "layout_config_sha256",
    "layout_enabled",
    "load_layout_prediction",
    "run_layout_detection",
]
