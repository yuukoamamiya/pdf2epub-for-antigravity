"""
Page-wise OCR processing for PDF files.

This module handles OCR at the page level, producing individual markdown files
for each page. The pages can later be aggregated into chapters.

Supports multiple backends:
- mistral: Mistral OCR API
- vertex: Vertex AI Mistral OCR
- vllm: VLLM-based OCR
- azure: Azure Document Intelligence (for Japanese vertical text)
- vision: Google Cloud Vision API (for Japanese vertical text)
"""

import json
import hashlib
import pymupdf as fitz
import os
import tempfile
from pathlib import Path
from typing import Any, Dict, Optional, Tuple, List
from loguru import logger
from .utils.logging_config import configure_logging
from .ocr.artifacts import OCRPageResult
from .ocr.backends import get_backend_spec
from .ocr_progress import (
    OCR_PROGRESS_SCHEMA_VERSION,
    load_progress,
    new_progress,
    normalize_progress,
)
from .ocr_consensus import (
    OCR_CONSENSUS_SCHEMA_VERSION,
    common_ocr_risk_reasons,
    consensus_is_current,
    secondary_ocr_enabled,
    secondary_backend_name,
    secondary_config_hash,
    write_page_consensus,
)
from .workflow_contracts import atomic_write_text, sha256_file

# Configure logger
logger = configure_logging()

# Cache for Azure/Vision clients (to avoid re-initialization)
_backend_clients = {}


def pdf_to_image(pdf_bytes: bytes, zoom_factor: float = 1.0) -> bytes:
    """Convert single-page PDF to PNG image bytes for vision backends.

    Args:
        pdf_bytes: PDF content as bytes (should be single page)
        zoom_factor: Image quality factor (1.0-3.0, higher = better quality)

    Returns:
        PNG image bytes
    """
    with fitz.open(stream=pdf_bytes, filetype="pdf") as pdf:
        page = pdf[0]
        mat = fitz.Matrix(zoom_factor, zoom_factor)
        pix = page.get_pixmap(matrix=mat)
        return pix.tobytes("png")


def ocr_pdf_chunk(
    pdf_bytes: bytes,
    session=None,
    project_id: str = None,
    location: str = None,
    chunk_info: str = "",
    images_dir: Path = None,
    page_number: int = 1,
    image_counter: int = 0,
    max_retries: int = 5,
    initial_backoff: float = 4.0,
    backend: str = "vertex",
    api_key: str = None,
    base_url: str = None,
    config: Dict = None
) -> Tuple[str, List, int]:
    """OCR a PDF chunk using selected backend.

    Routes to the appropriate backend based on configuration.
    Tuple-based adapters are used for vertex, mistral, vllm, azure, and vision.
    Chandra uses :func:`ocr_pdf_page` because it returns richer page artifacts.

    Args:
        pdf_bytes: PDF content as bytes
        session: Authorized session for Vertex AI (required for vertex backend)
        project_id: GCP project ID (required for vertex backend)
        location: GCP location (required for vertex backend)
        chunk_info: Description of the chunk being processed
        images_dir: Directory to save extracted images
        page_number: Page number for image naming
        image_counter: Starting counter for image numbering
        max_retries: Maximum number of retry attempts for 429 errors
        initial_backoff: Initial backoff time in seconds
        backend: OCR backend to use ('vertex', 'mistral', 'vllm', 'azure', 'vision')
        api_key: API key (for mistral backend)
        base_url: API base URL (for mistral backend)
        config: Configuration dict (required for azure/vision backends)

    Returns:
        Tuple of (markdown_content, images_info, updated_image_counter)
    """
    backend = str(backend or "").strip().lower()
    spec = get_backend_spec(backend)
    if spec.chunk_processor is not None:
        if backend == "mistral" and not api_key:
            raise ValueError("Mistral API key is required for mistral backend")
        if backend == "vertex" and (
            not session or not project_id or not location
        ):
            raise ValueError("session, project_id, and location are required for vertex backend")
        if backend == "vllm" and config is None:
            from .utils.common import load_config
            config = load_config()

        kwargs = {
            "pdf_bytes": pdf_bytes,
            "chunk_info": chunk_info,
            "images_dir": images_dir,
            "page_number": page_number,
            "image_counter": image_counter,
            "max_retries": max_retries,
            "initial_backoff": initial_backoff,
        }
        if backend == "mistral":
            kwargs.update(api_key=api_key)
            if base_url:
                kwargs["base_url"] = base_url
        elif backend == "vertex":
            kwargs.update(session=session, project_id=project_id, location=location)
        else:
            kwargs["config"] = config
        return spec.chunk_processor(**kwargs)

    if spec.image_page_processor is not None:
        if config is None:
            raise ValueError(f"config is required for {backend} backend")
        result = _process_image_page_backend(
            spec, pdf_bytes, config, images_dir, page_number, image_counter
        )
        return result

    raise ValueError(
        f"Backend {backend!r} only supports native page OCR; use ocr_pdf_page instead"
    )


def _process_image_page_backend(
    spec,
    pdf_bytes: bytes,
    config: Dict,
    images_dir: Optional[Path],
    page_number: int,
    image_counter: int,
) -> Tuple[str, List, int]:
    """Run an image-oriented backend through the legacy tuple adapter."""
    result = _process_image_page_result(
        spec, pdf_bytes, config, images_dir, page_number, image_counter
    )
    return result.markdown, result.images, result.image_counter


def _process_image_page_result(
    spec,
    pdf_bytes: bytes,
    config: Dict,
    images_dir: Optional[Path],
    page_number: int,
    image_counter: int,
) -> OCRPageResult:
    """Run an image backend while retaining its optional layout artifacts."""
    global _backend_clients
    zoom_factor = config.get("vision_ocr_settings", {}).get("zoom_factor", 1.0)
    if spec.name == "paddle":
        ocr_config = config.get("ocr", {}) if isinstance(config, dict) else {}
        backends = ocr_config.get("backends", {}) if isinstance(ocr_config, dict) else {}
        paddle_config = backends.get("paddle", {}) if isinstance(backends, dict) else {}
        if isinstance(paddle_config, dict) and paddle_config.get("dpi"):
            try:
                zoom_factor = max(1.0, float(paddle_config["dpi"]) / 72.0)
            except (TypeError, ValueError):
                pass
    img_bytes = pdf_to_image(pdf_bytes, zoom_factor)
    if spec.name not in _backend_clients:
        _backend_clients[spec.name] = spec.init_client(config)
        logger.info(f"Initialized {spec.name} OCR client")
    result = spec.image_page_processor(
        client=_backend_clients[spec.name],
        img_bytes=img_bytes,
        page_num=page_number,
        config=config,
        base_output_dir=images_dir.parent if images_dir else None,
    )
    markdown = result.get("text", "")
    illustrations = result.get("illustrations", [])
    if illustrations:
        from .ocr import inject_illustrations_into_text
        markdown = inject_illustrations_into_text(markdown, illustrations)
    return OCRPageResult(
        markdown=markdown,
        images=illustrations,
        image_counter=image_counter + len(illustrations),
        html=result.get("html"),
        raw_html=result.get("raw_html"),
        blocks=result.get("blocks", []),
        page_box=result.get("page_box"),
        model_input_size=result.get("model_input_size"),
        token_count=result.get("token_count"),
        backend=spec.name,
        model=result.get("model"),
        model_revision=result.get("model_revision"),
        assets=result.get("assets", []),
    )


def ocr_pdf_page(
    pdf_bytes: bytes,
    session=None,
    project_id: str = None,
    location: str = None,
    chunk_info: str = "",
    images_dir: Path = None,
    page_number: int = 1,
    image_counter: int = 0,
    max_retries: int = 5,
    initial_backoff: float = 4.0,
    backend: str = "vertex",
    api_key: str = None,
    base_url: str = None,
    config: Dict = None,
) -> OCRPageResult:
    """OCR one page while retaining every representation a backend exposes."""
    backend = str(backend or "").strip().lower()
    spec = get_backend_spec(backend)
    if spec.native_page_processor is not None:
        if config is None:
            raise ValueError(f"config is required for {backend} backend")
        return spec.native_page_processor(
            pdf_bytes,
            config,
            page_number=page_number,
            images_dir=images_dir,
            image_counter=image_counter,
        )

    if spec.image_page_processor is not None:
        if config is None:
            raise ValueError(f"config is required for {backend} backend")
        return _process_image_page_result(
            spec, pdf_bytes, config, images_dir, page_number, image_counter
        )

    tuple_result = ocr_pdf_chunk(
        pdf_bytes=pdf_bytes,
        session=session,
        project_id=project_id,
        location=location,
        chunk_info=chunk_info,
        images_dir=images_dir,
        page_number=page_number,
        image_counter=image_counter,
        max_retries=max_retries,
        initial_backoff=initial_backoff,
        backend=backend,
        api_key=api_key,
        base_url=base_url,
        config=config,
    )
    return OCRPageResult.from_tuple(tuple_result, backend=backend)


def _atomic_write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, path)
    finally:
        if os.path.exists(temporary_name):
            os.unlink(temporary_name)


def _atomic_write_json(path: Path, value: Dict[str, Any]) -> None:
    """Write a JSON checkpoint atomically."""
    _atomic_write_text(
        path,
        json.dumps(value, ensure_ascii=False, indent=2),
    )


def _file_sha256(path: Path) -> str:
    """Hash a source PDF for checkpoint provenance."""
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _load_page_sidecar(path: Path) -> Optional[Dict[str, Any]]:
    """Load a layout sidecar when the primary backend provides one."""
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _page_artifacts_are_complete(pages_dir: Path, page_number: int) -> bool:
    """Return whether a visual-OCR page has its completion artifacts."""
    markdown_path = pages_dir / f"page_{page_number:03d}.md"
    sidecar_path = pages_dir / f"page_{page_number:03d}.ocr.json"
    if not markdown_path.is_file() or not sidecar_path.is_file():
        return False
    try:
        sidecar = json.loads(sidecar_path.read_text(encoding="utf-8"))
        markdown_path.read_text(encoding="utf-8")
    except (OSError, UnicodeError, json.JSONDecodeError):
        return False
    return (
        isinstance(sidecar, dict)
        and sidecar.get("page_number") == page_number
        and sidecar.get("formats", {}).get("markdown") == markdown_path.name
    )


def _is_empty_page_result(page_result: OCRPageResult) -> bool:
    """Detect a result with no textual or visual payload.

    This is a review/failure signal rather than an automatic claim that the
    physical page is invalid: genuinely blank pages are possible, so callers
    may explicitly acknowledge them after inspection.
    """
    return not str(page_result.markdown or "").strip() and not (
        page_result.assets or page_result.images or page_result.blocks
    )


def save_page_artifacts(result: OCRPageResult, pages_dir: Path, page_number: int) -> Path:
    """Persist rich artifacts, writing Markdown last as the completion marker."""
    stem = f"page_{page_number:03d}"
    markdown_path = pages_dir / f"{stem}.md"
    html_path = pages_dir / f"{stem}.html"
    raw_html_path = pages_dir / f"{stem}.raw.html"
    sidecar_path = pages_dir / f"{stem}.ocr.json"

    if result.raw_html is not None:
        _atomic_write_text(raw_html_path, result.raw_html)
    if result.html is not None:
        _atomic_write_text(html_path, result.html)

    sidecar = {
        "schema_version": 1,
        "page_number": page_number,
        "backend": result.backend,
        "model": result.model,
        "model_revision": result.model_revision,
        "page_box": result.page_box,
        "model_input_size": result.model_input_size,
        "token_count": result.token_count,
        "formats": {
            "markdown": markdown_path.name,
            "html": html_path.name if result.html is not None else None,
            "raw_html": raw_html_path.name if result.raw_html is not None else None,
        },
        "raw_html": result.raw_html,
        "blocks": result.blocks,
        "assets": result.assets,
    }
    _atomic_write_text(sidecar_path, json.dumps(sidecar, ensure_ascii=False, indent=2))
    _atomic_write_text(markdown_path, result.markdown)
    return markdown_path


def count_tokens(text: str) -> int:
    """Estimate token count for text.

    Uses a simple approximation: ~4 characters per token for English,
    ~2 characters per token for CJK languages.
    """
    # Simple heuristic: check if text contains CJK characters
    import re
    cjk_pattern = re.compile(r'[\u4e00-\u9fff\u3040-\u309f\u30a0-\u30ff]')
    has_cjk = bool(cjk_pattern.search(text))

    if has_cjk:
        # CJK languages: ~2 chars per token
        return len(text) // 2
    else:
        # English: ~4 chars per token
        return len(text) // 4


def extract_pdf_pages(pdf_path: Path, start_page: int, end_page: int) -> bytes:
    """Extract specific pages from PDF and return as bytes."""
    with fitz.open(pdf_path) as full_pdf:
        # Create a new PDF with just the specified pages
        extracted_pdf = fitz.open()
        for page_num in range(start_page - 1, end_page):  # Convert to 0-based indexing
            if page_num < len(full_pdf):
                extracted_pdf.insert_pdf(full_pdf, from_page=page_num, to_page=page_num)

        # Save to bytes
        pdf_bytes = extracted_pdf.tobytes()
        extracted_pdf.close()

    return pdf_bytes


def _secondary_ocr_config(config: Dict[str, Any], backend: str) -> Dict[str, Any]:
    ocr_config = config.get("ocr", {}) if isinstance(config, dict) else {}
    backends = ocr_config.get("backends", {}) if isinstance(ocr_config, dict) else {}
    settings = backends.get(backend, {}) if isinstance(backends, dict) else {}
    return settings if isinstance(settings, dict) else {}


def _preflight_secondary_backend(
    output_dir: Path,
    backend: str,
    config: Dict[str, Any],
) -> None:
    """Fail before page workers when an optional local backend cannot start."""
    if backend != "paddle":
        return
    from .ocr.backends.paddle import preflight

    diagnostics, client = preflight(config)
    _atomic_write_json(output_dir / "ocr_secondary_preflight.json", diagnostics)
    if diagnostics.get("status") != "ready" or client is None:
        detail = diagnostics.get("error") or "backend initialization failed"
        raise RuntimeError(
            "PaddleOCR secondary backend preflight failed: "
            f"{detail}. See ocr_secondary_preflight.json for runtime versions; "
            "use an isolated OCR environment or disable the secondary backend."
        )
    # Reuse the initialized client so preflight does not download models or
    # trigger the same platform-specific initialization twice.
    _backend_clients[backend] = client


def run_secondary_ocr_consensus(
    *,
    ocr_pdf: Path,
    output_dir: Path,
    total_pages: int,
    primary_backend: str,
    config: Dict[str, Any],
    max_workers: int,
    resume: bool,
) -> Dict[str, Any]:
    """Run an optional local OCR backend and classify pages for review."""
    if secondary_ocr_enabled(config) and not secondary_backend_name(config):
        raise ValueError(
            "ocr.secondary.enabled is true but ocr.secondary.backend is missing"
        )
    secondary_backend = secondary_backend_name(config)
    if not secondary_backend:
        return {
            "enabled": False,
            "review_pages": [],
            "failed_pages": [],
        }
    if secondary_backend == str(primary_backend or "").strip().lower():
        raise ValueError("ocr secondary backend must differ from the primary backend")

    source_sha256 = _file_sha256(ocr_pdf)
    source_names = [f"page_{number:03d}.md" for number in range(1, total_pages + 1)]
    if resume and consensus_is_current(
        output_dir,
        config,
        source_sha256=source_sha256,
        primary_backend=primary_backend,
    ):
        from .ocr_consensus import load_consensus_manifest, review_required_files

        manifest = load_consensus_manifest(output_dir)
        return {
            "enabled": True,
            "secondary_backend": secondary_backend,
            "review_pages": review_required_files(output_dir),
            "failed_pages": [],
            "manifest": manifest,
        }

    from concurrent.futures import ThreadPoolExecutor, as_completed
    from .ocr_consensus import consensus_dir, secondary_page_dir

    secondary_dir = secondary_page_dir(output_dir)
    record_dir = consensus_dir(output_dir)
    secondary_dir.mkdir(parents=True, exist_ok=True)
    record_dir.mkdir(parents=True, exist_ok=True)
    backend_settings = _secondary_ocr_config(config, secondary_backend)
    secondary_workers = backend_settings.get("max_workers", 1)
    try:
        secondary_workers = max(1, int(secondary_workers))
    except (TypeError, ValueError):
        secondary_workers = 1

    _preflight_secondary_backend(output_dir, secondary_backend, config)

    def process_page(page_number: int) -> Dict[str, Any]:
        name = f"page_{page_number:03d}.md"
        primary_path = output_dir / "pages" / name
        try:
            primary_text = primary_path.read_text(encoding="utf-8")
            pdf_bytes = extract_pdf_pages(ocr_pdf, page_number, page_number)
            secondary_result = ocr_pdf_page(
                pdf_bytes,
                chunk_info=f"Secondary OCR page {page_number}",
                page_number=page_number,
                backend=secondary_backend,
                config=config,
            )
            # Keep a Chandra-shaped secondary sidecar.  The consensus record
            # still compares Markdown, but footnote review can now compare
            # page zones, labels, and numeric note keys as well.
            save_page_artifacts(secondary_result, secondary_dir, page_number)
            record = write_page_consensus(
                output_dir,
                source_name=name,
                primary_text=primary_text,
                secondary_text=secondary_result.markdown,
                primary_backend=primary_backend,
                secondary_backend=secondary_backend,
                config=config,
                primary_layout=_load_page_sidecar(
                    output_dir / "pages" / f"page_{page_number:03d}.ocr.json"
                ),
                secondary_layout=_load_page_sidecar(
                    secondary_dir / f"page_{page_number:03d}.ocr.json"
                ),
            )
            record["primary_source_sha256"] = sha256_file(primary_path)
            primary_sidecar = output_dir / "pages" / f"page_{page_number:03d}.ocr.json"
            secondary_sidecar = secondary_dir / f"page_{page_number:03d}.ocr.json"
            if primary_sidecar.is_file():
                record["primary_layout_sha256"] = sha256_file(primary_sidecar)
            record["secondary_layout_sha256"] = sha256_file(secondary_sidecar)
            record_path = record_dir / f"{Path(name).stem}.json"
            atomic_write_text(
                record_path,
                json.dumps(record, ensure_ascii=False, indent=2),
            )
            return {"page": page_number, "record": record, "error": None}
        except Exception as exc:
            return {"page": page_number, "record": None, "error": str(exc)}

    records: Dict[str, Any] = {}
    failed_pages: List[int] = []
    with ThreadPoolExecutor(max_workers=secondary_workers) as executor:
        futures = {
            executor.submit(process_page, page_number): page_number
            for page_number in range(1, total_pages + 1)
        }
        for future in as_completed(futures):
            result = future.result()
            page_number = result["page"]
            name = f"page_{page_number:03d}.md"
            if result["error"]:
                failed_pages.append(page_number)
                logger.error(
                    f"Secondary OCR failed for page {page_number}: {result['error']}"
                )
            else:
                records[name] = result["record"]

    manifest = {
        "schema_version": OCR_CONSENSUS_SCHEMA_VERSION,
        "source_sha256": source_sha256,
        "primary_backend": primary_backend,
        "secondary_backend": secondary_backend,
        "config_sha256": secondary_config_hash(config),
        "files": source_names,
        "records": records,
        "failed_pages": sorted(failed_pages),
        "complete": not failed_pages and len(records) == len(source_names),
    }
    if not failed_pages and len(records) == len(source_names):
        page_texts = {
            name: {
                "primary": (output_dir / "pages" / name).read_text(encoding="utf-8"),
                "secondary": (secondary_dir / name).read_text(encoding="utf-8"),
            }
            for name in source_names
        }
        common_risks = common_ocr_risk_reasons(page_texts, config)
        for name, reasons in common_risks.items():
            record = records.get(name)
            if not isinstance(record, dict):
                continue
            record["common_miss_risks"] = reasons
            record["action"] = "visual_review"
            record["comparison"]["status"] = "review_required"
            record["comparison"].setdefault("reasons", []).extend(reasons)
            atomic_write_text(
                record_dir / f"{Path(name).stem}.json",
                json.dumps(record, ensure_ascii=False, indent=2),
            )
    atomic_write_text(
        output_dir / "ocr_consensus.json",
        json.dumps(manifest, ensure_ascii=False, indent=2),
    )
    review_pages = sorted(
        name
        for name, record in records.items()
        if record.get("action") == "visual_review"
    )
    return {
        "enabled": True,
        "secondary_backend": secondary_backend,
        "review_pages": review_pages,
        "failed_pages": sorted(failed_pages),
        "manifest": manifest,
    }


def ocr_full_book_pagewise(
    pdf_path: Path,
    output_dir: Path,
    session: Any = None,
    project_id: str = None,
    location: str = None,
    start_page: int = 1,
    end_page: int = None,
    backend: str = "vertex",
    api_key: str = None,
    base_url: str = None,
    resume: bool = False,
    config: Dict = None,
    max_workers: int = 5,
    allow_empty_pages: bool = False,
    retry_pages: Optional[List[int]] = None,
) -> Dict[str, Any]:
    """OCR全书，并行处理多页。

    Args:
        pdf_path: Path to PDF file
        output_dir: Base output directory (will create pages/ subdirectory)
        session: Authorized session for API calls
        project_id: GCP project ID
        location: GCP location
        start_page: First page to process (default: 1)
        end_page: Last page to process (default: all pages)
        backend: OCR backend to use
        api_key: API key for Mistral backend
        base_url: Base URL for Mistral backend
        resume: Resume from previous progress
        config: Configuration dict (for retry settings)
        max_workers: Number of parallel OCR requests (default: 5)
        allow_empty_pages: Explicitly acknowledge pages with no OCR payload
            after manual inspection.
        retry_pages: Specific physical pages to re-OCR even when their
            checkpoint currently says they are complete.
    """
    backend = str(backend or "").strip().lower()
    from concurrent.futures import ThreadPoolExecutor, as_completed
    # Get retry settings from config
    if config is None:
        config = {}

    ocr_config = config.get('ocr', {})
    max_retries = ocr_config.get('max_retries', 5)
    initial_backoff = ocr_config.get('initial_backoff', 4.0)

    # Optional: backend-specific override
    backend_config = ocr_config.get('backends', {}).get(backend, {})
    if backend_config:
        max_retries = backend_config.get('max_retries', max_retries)
        initial_backoff = backend_config.get('initial_backoff', initial_backoff)
    # Create pages directory
    pages_dir = output_dir / "pages"
    pages_dir.mkdir(parents=True, exist_ok=True)

    # Create images directory
    images_dir = output_dir / "images"
    images_dir.mkdir(parents=True, exist_ok=True)

    # Prefer original PDF for OCR (preprocessed version is binarized, ruins images)
    original_pdf = output_dir / "input_original.pdf"
    ocr_pdf = original_pdf if original_pdf.exists() else pdf_path
    if ocr_pdf != pdf_path:
        logger.info(f"Using original PDF for OCR: {ocr_pdf}")

    # Determine page range
    with fitz.open(ocr_pdf) as pdf:
        total_pages = len(pdf)

    source_sha256 = _file_sha256(ocr_pdf)
    progress_file = pages_dir / "ocr_progress.json"
    page_stats_file = pages_dir / "page_stats.json"

    progress = None
    page_stats: Dict[str, Any] = {}
    if resume and progress_file.exists():
        progress = load_progress(progress_file)
        if progress is None:
            logger.warning(
                "OCR progress is invalid; rebuilding the checkpoint and rechecking all pages"
            )
        else:
            try:
                recorded_total_pages = int(progress.get("total_pages", 0) or 0)
            except (TypeError, ValueError):
                recorded_total_pages = 0
            if (
                progress.get("schema_version") != OCR_PROGRESS_SCHEMA_VERSION
                or progress.get("source_sha256") != source_sha256
                or recorded_total_pages != total_pages
                or str(progress.get("backend", "")).strip().lower() != backend
            ):
                logger.warning(
                    "OCR checkpoint provenance changed; existing page files will be reprocessed"
                )
                progress = None
            else:
                progress = normalize_progress(progress, total_pages)
                logger.info(
                    f"Resuming from progress: {len(progress['pages_processed'])} pages already processed"
                )

    if progress is None:
        progress = new_progress(
            source_sha256=source_sha256,
            total_pages=total_pages,
            backend=backend,
        )

    if page_stats_file.exists() and progress.get("pages_processed"):
        try:
            loaded_stats = json.loads(page_stats_file.read_text(encoding="utf-8"))
            if isinstance(loaded_stats, dict):
                page_stats = loaded_stats
        except (OSError, UnicodeError, json.JSONDecodeError):
            logger.warning("page_stats.json is invalid; rebuilding page statistics")

    if end_page is None:
        end_page = total_pages

    end_page = min(end_page, total_pages)

    forced_pages = {
        int(page)
        for page in (retry_pages or [])
        if isinstance(page, int) and not isinstance(page, bool)
    }
    invalid_forced_pages = sorted(
        page for page in forced_pages if page < 1 or page > total_pages
    )
    if invalid_forced_pages:
        raise ValueError(
            f"retry_pages contains page(s) outside the PDF range: {invalid_forced_pages}"
        )
    outside_requested_range = sorted(
        page for page in forced_pages if page < start_page or page > end_page
    )
    if outside_requested_range:
        raise ValueError(
            "retry_pages must fall within the requested OCR range: "
            f"{outside_requested_range}"
        )
    if forced_pages:
        progress["pages_processed"] = [
            page for page in progress["pages_processed"] if page not in forced_pages
        ]
        progress["failed_pages"] = [
            page for page in progress["failed_pages"] if page not in forced_pages
        ]
        progress["empty_pages"] = [
            page for page in progress["empty_pages"] if page not in forced_pages
        ]
        progress["allowed_empty_pages"] = [
            page
            for page in progress["allowed_empty_pages"]
            if page not in forced_pages
        ]
        for page in forced_pages:
            page_stats.pop(str(page), None)

    progress["requested_start_page"] = start_page
    progress["requested_end_page"] = end_page
    progress["source_sha256"] = source_sha256
    progress["total_pages"] = total_pages
    progress["backend"] = backend
    progress["schema_version"] = OCR_PROGRESS_SCHEMA_VERSION
    progress["retry_pages"] = sorted(forced_pages)

    logger.info(f"Processing pages {start_page}-{end_page} (total: {end_page - start_page + 1} pages, {max_workers} workers)")

    # Determine pages to process
    pages_to_process = []
    for page_num in range(start_page, end_page + 1):
        page_file = pages_dir / f"page_{page_num:03d}.md"

        # Check if already processed
        if resume and page_num in progress['pages_processed']:
            if _page_artifacts_are_complete(pages_dir, page_num):
                # Update stats if not already recorded
                if str(page_num) not in page_stats:
                    with open(page_file, 'r', encoding='utf-8') as f:
                        existing_content = f.read()
                    token_count = count_tokens(existing_content)
                    page_stats[str(page_num)] = {
                        'tokens': token_count,
                        'file': (page_file.relative_to(output_dir)).as_posix(),
                        'char_count': len(existing_content)
                    }
                logger.debug(f"Skipping page {page_num} (already processed)")
                continue
            else:
                logger.warning(
                    f"Page {page_num} is marked processed but its artifacts are incomplete; reprocessing"
                )
                progress['pages_processed'].remove(page_num)

        pages_to_process.append(page_num)

    if not pages_to_process:
        logger.info("All pages already processed")
    else:
        logger.info(f"Processing {len(pages_to_process)} pages...")

        # Define worker function
        def process_single_page(page_num):
            """Process a single page and return result."""
            try:
                pdf_bytes = extract_pdf_pages(ocr_pdf, page_num, page_num)
                chunk_info = f"Page {page_num}"

                # Use page_num as image counter base to avoid conflicts
                page_result = ocr_pdf_page(
                    pdf_bytes,
                    session,
                    project_id,
                    location,
                    chunk_info,
                    images_dir,
                    page_num,
                    page_num * 100,  # Use page-based counter to avoid conflicts
                    max_retries=max_retries,
                    initial_backoff=initial_backoff,
                    backend=backend,
                    api_key=api_key,
                    base_url=base_url,
                    config=config
                )

                return {
                    'page_num': page_num,
                    'page_result': page_result,
                    'success': True,
                    'error': None
                }
            except Exception as e:
                import traceback
                return {
                    'page_num': page_num,
                    'page_result': None,
                    'success': False,
                    'error': str(e),
                    'traceback': traceback.format_exc()
                }

        # Process pages in parallel
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {executor.submit(process_single_page, page_num): page_num
                      for page_num in pages_to_process}

            for future in as_completed(futures):
                result = future.result()
                page_num = result['page_num']
                page_file = pages_dir / f"page_{page_num:03d}.md"

                if result['success']:
                    page_result = result['page_result']
                    save_page_artifacts(page_result, pages_dir, page_num)

                    empty_result = _is_empty_page_result(page_result)

                    # Count tokens
                    token_count = (
                        page_result.token_count
                        if page_result.token_count is not None
                        else count_tokens(page_result.markdown)
                    )

                    # Update stats
                    page_stats[str(page_num)] = {
                        'tokens': token_count,
                        'file': (page_file.relative_to(output_dir)).as_posix(),
                        'char_count': len(page_result.markdown),
                        'html_file': (
                            (pages_dir / f"page_{page_num:03d}.html").relative_to(output_dir).as_posix()
                            if page_result.html is not None
                            else None
                        ),
                        'artifact_file': (
                            (pages_dir / f"page_{page_num:03d}.ocr.json").relative_to(output_dir).as_posix()
                        ),
                    }

                    # Empty OCR output is not silently accepted.  It can be a
                    # real blank page, but that requires explicit acknowledgement.
                    if empty_result and not allow_empty_pages:
                        if page_num not in progress["empty_pages"]:
                            progress["empty_pages"].append(page_num)
                        if page_num in progress["pages_processed"]:
                            progress["pages_processed"].remove(page_num)
                        logger.error(
                            f"Page {page_num} produced empty OCR output; review or retry it"
                        )
                    else:
                        if page_num not in progress['pages_processed']:
                            progress['pages_processed'].append(page_num)
                        if empty_result and page_num not in progress["allowed_empty_pages"]:
                            progress["allowed_empty_pages"].append(page_num)
                        if page_num in progress["empty_pages"]:
                            progress["empty_pages"].remove(page_num)

                    # Remove from failed list if it was there
                    if page_num in progress.get('failed_pages', []):
                        progress['failed_pages'].remove(page_num)

                    if empty_result:
                        logger.warning(
                            f"Saved page {page_num} with no OCR payload ({token_count} tokens)"
                        )
                    else:
                        logger.success(f"Saved page {page_num} ({token_count} tokens)")
                else:
                    logger.error(f"Failed to process page {page_num}: {result['error']}")
                    logger.debug(result.get('traceback', ''))

                    # Record failed page
                    if 'failed_pages' not in progress:
                        progress['failed_pages'] = []
                    if page_num in progress['pages_processed']:
                        progress['pages_processed'].remove(page_num)
                    if page_num not in progress['failed_pages']:
                        progress['failed_pages'].append(page_num)

                # Save progress after each page
                progress["pages_processed"] = sorted(set(progress["pages_processed"]))
                progress["failed_pages"] = sorted(set(progress["failed_pages"]))
                progress["empty_pages"] = sorted(set(progress["empty_pages"]))
                progress["allowed_empty_pages"] = sorted(
                    set(progress["allowed_empty_pages"])
                )
                progress["missing_pages"] = sorted(
                    set(range(1, total_pages + 1))
                    - set(progress["pages_processed"])
                )
                _atomic_write_json(progress_file, progress)
                _atomic_write_json(page_stats_file, page_stats)

    # Summary
    total_tokens = sum(stats['tokens'] for stats in page_stats.values())
    avg_tokens = total_tokens / len(page_stats) if page_stats else 0

    progress["pages_processed"] = sorted(set(progress["pages_processed"]))
    progress["failed_pages"] = sorted(set(progress["failed_pages"]))
    progress["empty_pages"] = sorted(set(progress["empty_pages"]))
    progress["allowed_empty_pages"] = sorted(set(progress["allowed_empty_pages"]))
    progress["missing_pages"] = sorted(
        set(range(1, total_pages + 1)) - set(progress["pages_processed"])
    )
    _atomic_write_json(progress_file, progress)
    _atomic_write_json(page_stats_file, page_stats)

    unacknowledged_empty = sorted(
        set(progress["empty_pages"]) - set(progress["allowed_empty_pages"])
    )
    missing_pages = progress["missing_pages"]
    failed_pages = progress.get("failed_pages", [])

    secondary_summary: Dict[str, Any] = {
        "enabled": False,
        "review_pages": [],
        "failed_pages": [],
    }
    if not failed_pages and not missing_pages and not unacknowledged_empty:
        secondary_summary = run_secondary_ocr_consensus(
            ocr_pdf=ocr_pdf,
            output_dir=output_dir,
            total_pages=total_pages,
            primary_backend=backend,
            config=config,
            max_workers=max_workers,
            resume=resume,
        )

    logger.info(f"\n=== Page-wise OCR Summary ===")
    logger.info(
        f"Total pages processed: {len(progress['pages_processed'])}/{total_pages}"
    )
    logger.info(f"Total tokens: {total_tokens}")
    logger.info(f"Average tokens per page: {avg_tokens:.0f}")

    # Report failed pages
    if failed_pages or missing_pages or unacknowledged_empty:
        if failed_pages:
            logger.error(f"Failed pages ({len(failed_pages)}): {sorted(failed_pages)}")
        if missing_pages:
            logger.error(f"Missing/unprocessed pages ({len(missing_pages)}): {missing_pages}")
        if unacknowledged_empty:
            logger.error(
                f"Empty OCR pages requiring review ({len(unacknowledged_empty)}): "
                f"{unacknowledged_empty}"
            )
        logger.error("OCR is incomplete; downstream PDF workflow stages must stop")
    else:
        logger.success(f"All pages processed successfully!")

    if secondary_summary.get("enabled"):
        secondary_failed = secondary_summary.get("failed_pages", [])
        if secondary_failed:
            logger.error(
                "Secondary OCR consensus is incomplete; visual review cannot be safely scoped"
            )
        else:
            logger.info(
                "Secondary OCR consensus complete: "
                f"{len(secondary_summary.get('review_pages', []))} page(s) need visual review"
            )

    logger.info(f"Output directory: {pages_dir}")
    return {
        "total_pages": total_pages,
        "processed_pages": progress["pages_processed"],
        "failed_pages": failed_pages,
        "missing_pages": missing_pages,
        "empty_pages": unacknowledged_empty,
        "secondary_ocr_enabled": bool(secondary_summary.get("enabled")),
        "secondary_ocr_backend": secondary_summary.get("secondary_backend"),
        "secondary_review_pages": secondary_summary.get("review_pages", []),
        "secondary_failed_pages": secondary_summary.get("failed_pages", []),
        "progress_file": progress_file,
    }
