from dataclasses import dataclass
"""
Page-wise OCR processing for PDF files.

This module handles OCR at the page level, producing individual markdown files
for each page. The pages can later be aggregated into chapters.

Supports multiple backends:
- vllm: VLLM-based OCR (legacy compatibility)
- chandra: the default multimodal page OCR backend
"""

import json
import pymupdf as fitz
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
from .workflow_contracts import atomic_write_json, atomic_write_text, sha256_file

# Configure logger
logger = configure_logging()

SECONDARY_PREFLIGHT_SCHEMA_VERSION = 1
DEFAULT_SECONDARY_MAX_ESTIMATED_SECONDS = 60 * 60
DEFAULT_SECONDARY_ESTIMATED_SECONDS_PER_PAGE = 2.0
DEFAULT_PRIMARY_OCR_BACKEND = "chandra"


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
    # This legacy chunk adapter is retained for the historical VLLM caller.
    backend: str = "chandra",
    api_key: str = None,
    base_url: str = None,
    config: Dict = None
) -> Tuple[str, List, int]:
    """OCR a PDF chunk using selected backend.

    Routes to the appropriate backend based on configuration.
    The VLLM adapter is the only remaining chunk-shaped compatibility path.
    Chandra uses :func:`ocr_pdf_page` because it returns richer page artifacts.

    Args:
        pdf_bytes: PDF content as bytes
        session: Retained for compatibility with older callers.
        project_id: Retained for compatibility with older callers.
        location: Retained for compatibility with older callers.
        chunk_info: Description of the chunk being processed
        images_dir: Directory to save extracted images
        page_number: Page number for image naming
        image_counter: Starting counter for image numbering
        max_retries: Maximum number of retry attempts for 429 errors
        initial_backoff: Initial backoff time in seconds
        backend: OCR backend to use ('vllm')
        api_key: Retained for compatibility with older callers.
        base_url: Retained for compatibility with older callers.
        config: Configuration dict (required by the selected backend)

    Returns:
        Tuple of (markdown_content, images_info, updated_image_counter)
    """
    backend = str(backend or "").strip().lower()
    spec = get_backend_spec(backend)
    if spec.chunk_processor is not None:
        if backend == "vllm" and config is None:
            from .utils.configuration import load_config
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
        kwargs["config"] = config
        return spec.chunk_processor(**kwargs)

    raise ValueError(
        f"Backend {backend!r} only supports native page OCR; use ocr_pdf_page instead"
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
    backend: str = DEFAULT_PRIMARY_OCR_BACKEND,
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
        atomic_write_text(raw_html_path, result.raw_html)
    if result.html is not None:
        atomic_write_text(html_path, result.html)

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
    atomic_write_json(sidecar_path, sidecar)
    atomic_write_text(markdown_path, result.markdown)
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


def _secondary_performance_config(
    config: Dict[str, Any], backend: str
) -> Dict[str, Any]:
    """Return the explicit, non-secret secondary OCR performance policy."""
    ocr_config = config.get("ocr", {}) if isinstance(config, dict) else {}
    secondary = ocr_config.get("secondary", {}) if isinstance(ocr_config, dict) else {}
    backend_settings = _secondary_ocr_config(config, backend)
    policy = secondary.get("performance", {}) if isinstance(secondary, dict) else {}
    backend_policy = (
        backend_settings.get("performance", {})
        if isinstance(backend_settings, dict)
        else {}
    )
    merged: Dict[str, Any] = {}
    if isinstance(policy, dict):
        merged.update(policy)
    if isinstance(backend_policy, dict):
        merged.update(backend_policy)
    return merged


def _positive_float(value: Any, default: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


def preflight_secondary_ocr(
    output_dir: Path,
    total_pages: int,
    config: Dict[str, Any],
    *,
    allow_slow: bool = False,
) -> Dict[str, Any]:
    """Check secondary OCR policy before any primary page worker starts.

    This check intentionally does not initialize a model. It records a
    conservative runtime estimate before the primary OCR spends time producing
    page output.
    """
    backend = secondary_backend_name(config)
    if secondary_ocr_enabled(config) and not backend:
        report = {
            "schema_version": SECONDARY_PREFLIGHT_SCHEMA_VERSION,
            "status": "blocked",
            "evidence_mode": "two_ocr",
            "total_pages": int(total_pages),
            "reason": "missing_secondary_backend",
            "error": "ocr.secondary.enabled is true but ocr.secondary.backend is missing",
        }
        atomic_write_json(output_dir / "ocr_secondary_preflight.json", report)
        raise ValueError(report["error"])
    if not backend:
        report = {
            "schema_version": SECONDARY_PREFLIGHT_SCHEMA_VERSION,
            "status": "disabled",
            "evidence_mode": "single_ocr",
            "total_pages": int(total_pages),
            "estimated_seconds": 0.0,
            "requires_confirmation": False,
        }
        atomic_write_json(output_dir / "ocr_secondary_preflight.json", report)
        return report

    backend_settings = _secondary_ocr_config(config, backend)
    policy = _secondary_performance_config(config, backend)
    report: Dict[str, Any] = {
        "schema_version": SECONDARY_PREFLIGHT_SCHEMA_VERSION,
        "status": "ready",
        "evidence_mode": "two_ocr",
        "backend": backend,
        "total_pages": int(total_pages),
        "policy": "abort_until_explicit_confirmation",
        "requires_confirmation": False,
    }

    seconds_per_page = _positive_float(
        policy.get("estimated_seconds_per_page"),
        DEFAULT_SECONDARY_ESTIMATED_SECONDS_PER_PAGE,
    )
    max_estimated_seconds = _positive_float(
        policy.get("max_estimated_seconds", DEFAULT_SECONDARY_MAX_ESTIMATED_SECONDS),
        DEFAULT_SECONDARY_MAX_ESTIMATED_SECONDS,
    )
    try:
        workers = max(1, int(backend_settings.get("max_workers", 1)))
    except (TypeError, ValueError):
        workers = 1
    estimated_seconds = float(total_pages) * seconds_per_page / workers
    report.update(
        {
            "worker_count": workers,
            "estimated_seconds_per_page": seconds_per_page,
            "max_estimated_seconds": max_estimated_seconds,
            "estimated_seconds": round(estimated_seconds, 2),
            "estimated_hours": round(estimated_seconds / 3600.0, 3),
        }
    )
    if estimated_seconds > max_estimated_seconds and not allow_slow:
        report.update(
            {
                "status": "confirmation_required",
                "requires_confirmation": True,
                "reason": "estimated_runtime_exceeds_limit",
                "error": (
                    f"Estimated secondary OCR time is {estimated_seconds / 3600.0:.2f}h, "
                    f"above the configured limit of {max_estimated_seconds / 3600.0:.2f}h. "
                    "Rerun with --allow-slow-secondary only after accepting the cost, "
                    "or disable ocr.secondary and rerun as single OCR."
                ),
            }
        )
        atomic_write_json(output_dir / "ocr_secondary_preflight.json", report)
        raise RuntimeError(
            f"Secondary OCR preflight requires confirmation: {report['error']} "
            "See ocr_secondary_preflight.json."
        )
    if estimated_seconds > max_estimated_seconds:
        report.update(
            {
                "status": "allowed_with_confirmation",
                "requires_confirmation": True,
                "reason": "estimated_runtime_exceeds_limit_but_was_explicitly_allowed",
            }
        )
    atomic_write_json(output_dir / "ocr_secondary_preflight.json", report)
    return report


@dataclass(frozen=True)
class _SecondaryConsensusState:
    """Static configuration for one secondary-OCR consensus run."""

    secondary_backend: str
    source_sha256: str
    source_names: list[str]
    secondary_dir: Path
    record_dir: Path
    secondary_workers: int
    secondary_max_retries: int
    secondary_initial_backoff: float
    resume: bool = False


def _load_secondary_consensus_state(
    *,
    ocr_pdf: Path,
    output_dir: Path,
    total_pages: int,
    primary_backend: str,
    config: Dict[str, Any],
    resume: bool,
) -> tuple[Optional[Dict[str, Any]], Optional[_SecondaryConsensusState]]:
    """Validate secondary OCR configuration and load resumable state."""
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
        }, None
    if secondary_backend == str(primary_backend or "").strip().lower():
        raise ValueError("ocr secondary backend must differ from the primary backend")

    source_sha256 = sha256_file(ocr_pdf)
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
        }, None

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
    try:
        secondary_max_retries = max(1, int(backend_settings.get("max_retries", 5)))
    except (TypeError, ValueError):
        secondary_max_retries = 5
    try:
        secondary_initial_backoff = max(
            0.1, float(backend_settings.get("initial_backoff", 4.0))
        )
    except (TypeError, ValueError):
        secondary_initial_backoff = 4.0
    return None, _SecondaryConsensusState(
        secondary_backend=secondary_backend,
        source_sha256=source_sha256,
        source_names=source_names,
        secondary_dir=secondary_dir,
        record_dir=record_dir,
        secondary_workers=secondary_workers,
        secondary_max_retries=secondary_max_retries,
        secondary_initial_backoff=secondary_initial_backoff,
        resume=resume,
    )


def _reusable_secondary_page(
    page_number: int,
    *,
    state: _SecondaryConsensusState,
) -> Optional[tuple[str, Dict[str, Any]]]:
    """Return existing secondary page text and sidecar during resume when valid."""
    if not state.resume:
        return None
    name = f"page_{page_number:03d}.md"
    secondary_md = state.secondary_dir / name
    secondary_sidecar = state.secondary_dir / f"page_{page_number:03d}.ocr.json"
    if not secondary_md.is_file() or not secondary_sidecar.is_file():
        return None
    secondary_layout = _load_page_sidecar(secondary_sidecar)
    if not isinstance(secondary_layout, dict):
        return None
    if str(secondary_layout.get("backend") or "").strip().lower() != state.secondary_backend:
        return None
    if not isinstance(secondary_layout.get("blocks"), list):
        return None
    try:
        secondary_text = secondary_md.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return None
    if secondary_text.strip():
        return secondary_text, secondary_layout
    record_path = state.record_dir / f"{Path(name).stem}.json"
    if not record_path.is_file():
        return None
    try:
        existing_record = json.loads(record_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    if not isinstance(existing_record, dict):
        return None
    if (
        existing_record.get("secondary_sha256") == sha256_file(secondary_md)
        and existing_record.get("secondary_layout_sha256") == sha256_file(secondary_sidecar)
    ):
        return secondary_text, secondary_layout
    return None


def _process_secondary_page(
    page_number: int,
    *,
    state: _SecondaryConsensusState,
    ocr_pdf: Path,
    output_dir: Path,
    config: Dict[str, Any],
    primary_backend: str,
) -> Dict[str, Any]:
    """OCR one page with the secondary backend and persist its record."""
    name = f"page_{page_number:03d}.md"
    primary_path = output_dir / "pages" / name
    try:
        primary_text = primary_path.read_text(encoding="utf-8")
        reused = _reusable_secondary_page(page_number, state=state)
        if reused is not None:
            secondary_text, secondary_layout = reused
        else:
            pdf_bytes = extract_pdf_pages(ocr_pdf, page_number, page_number)
            secondary_result = ocr_pdf_page(
                pdf_bytes,
                chunk_info=f"Secondary OCR page {page_number}",
                page_number=page_number,
                backend=state.secondary_backend,
                max_retries=state.secondary_max_retries,
                initial_backoff=state.secondary_initial_backoff,
                config=config,
            )
            # Keep a Chandra-shaped secondary sidecar.  The consensus record
            # still compares Markdown, but footnote review can now compare
            # page zones, labels, and numeric note keys as well.
            save_page_artifacts(secondary_result, state.secondary_dir, page_number)
            secondary_text = secondary_result.markdown
            secondary_layout = _load_page_sidecar(
                state.secondary_dir / f"page_{page_number:03d}.ocr.json"
            )
        record = write_page_consensus(
            output_dir,
            source_name=name,
            primary_text=primary_text,
            secondary_text=secondary_text,
            primary_backend=primary_backend,
            secondary_backend=state.secondary_backend,
            config=config,
            primary_layout=_load_page_sidecar(
                output_dir / "pages" / f"page_{page_number:03d}.ocr.json"
            ),
            secondary_layout=secondary_layout,
        )
        record["primary_source_sha256"] = sha256_file(primary_path)
        primary_sidecar = output_dir / "pages" / f"page_{page_number:03d}.ocr.json"
        secondary_sidecar = state.secondary_dir / f"page_{page_number:03d}.ocr.json"
        if primary_sidecar.is_file():
            record["primary_layout_sha256"] = sha256_file(primary_sidecar)
        record["secondary_layout_sha256"] = sha256_file(secondary_sidecar)
        record_path = state.record_dir / f"{Path(name).stem}.json"
        atomic_write_json(record_path, record)
        return {"page": page_number, "record": record, "error": None}
    except Exception as exc:
        return {"page": page_number, "record": None, "error": str(exc)}


def _run_secondary_consensus_pages(
    state: _SecondaryConsensusState,
    *,
    ocr_pdf: Path,
    output_dir: Path,
    config: Dict[str, Any],
    total_pages: int,
    primary_backend: str,
) -> tuple[Dict[str, Any], List[int]]:
    """Run secondary OCR workers and collect successful page records."""
    from concurrent.futures import ThreadPoolExecutor, as_completed

    records: Dict[str, Any] = {}
    failed_pages: List[int] = []
    with ThreadPoolExecutor(max_workers=state.secondary_workers) as executor:
        futures = {
            executor.submit(
                _process_secondary_page,
                page_number,
                state=state,
                ocr_pdf=ocr_pdf,
                output_dir=output_dir,
                config=config,
                primary_backend=primary_backend,
            ): page_number
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
    return records, failed_pages


def _finalize_secondary_consensus(
    state: _SecondaryConsensusState,
    *,
    output_dir: Path,
    config: Dict[str, Any],
    primary_backend: str,
    records: Dict[str, Any],
    failed_pages: List[int],
) -> Dict[str, Any]:
    """Write the consensus manifest and annotate common-risk pages."""
    source_names = state.source_names
    manifest = {
        "schema_version": OCR_CONSENSUS_SCHEMA_VERSION,
        "source_sha256": state.source_sha256,
        "primary_backend": primary_backend,
        "secondary_backend": state.secondary_backend,
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
                "secondary": (state.secondary_dir / name).read_text(encoding="utf-8"),
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
            atomic_write_json(state.record_dir / f"{Path(name).stem}.json", record)
    atomic_write_json(output_dir / "ocr_consensus.json", manifest)
    review_pages = sorted(
        name
        for name, record in records.items()
        if record.get("action") == "visual_review"
    )
    return {
        "enabled": True,
        "secondary_backend": state.secondary_backend,
        "review_pages": review_pages,
        "failed_pages": sorted(failed_pages),
        "manifest": manifest,
    }


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
    early_result, state = _load_secondary_consensus_state(
        ocr_pdf=ocr_pdf,
        output_dir=output_dir,
        total_pages=total_pages,
        primary_backend=primary_backend,
        config=config,
        resume=resume,
    )
    if early_result is not None or state is None:
        return early_result or {}
    records, failed_pages = _run_secondary_consensus_pages(
        state,
        ocr_pdf=ocr_pdf,
        output_dir=output_dir,
        config=config,
        total_pages=total_pages,
        primary_backend=primary_backend,
    )
    return _finalize_secondary_consensus(
        state,
        output_dir=output_dir,
        config=config,
        primary_backend=primary_backend,
        records=records,
        failed_pages=failed_pages,
    )


@dataclass(frozen=True)
class _PageRunInputs:
    """State for one page-OCR worker batch."""

    pages_to_process: list[int]
    ocr_pdf: Path
    session: Any
    project_id: Any
    location: Any
    images_dir: Optional[Path]
    pages_dir: Path
    output_dir: Path
    backend: str
    api_key: Optional[str]
    base_url: Optional[str]
    config: Dict[str, Any]
    max_workers: int
    max_retries: int
    initial_backoff: float
    allow_empty_pages: bool
    progress: Dict[str, Any]
    page_stats: Dict[str, Any]
    progress_file: Path
    page_stats_file: Path
    total_pages: int


def _ocr_pending_page(inputs: _PageRunInputs, page_num: int) -> Dict[str, Any]:
    """OCR one pending page and return its result envelope."""
    try:
        pdf_bytes = extract_pdf_pages(inputs.ocr_pdf, page_num, page_num)
        chunk_info = f"Page {page_num}"

        # Use page_num as image counter base to avoid conflicts
        page_result = ocr_pdf_page(
            pdf_bytes,
            inputs.session,
            inputs.project_id,
            inputs.location,
            chunk_info,
            inputs.images_dir,
            page_num,
            page_num * 100,  # Use page-based counter to avoid conflicts
            max_retries=inputs.max_retries,
            initial_backoff=inputs.initial_backoff,
            backend=inputs.backend,
            api_key=inputs.api_key,
            base_url=inputs.base_url,
            config=inputs.config
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


def _apply_page_result(inputs: _PageRunInputs, result: Dict[str, Any]) -> None:
    """Record one worker result in the page stats and progress maps."""
    page_num = result['page_num']
    page_file = inputs.pages_dir / f"page_{page_num:03d}.md"
    progress = inputs.progress
    page_stats = inputs.page_stats

    if result['success']:
        page_result = result['page_result']
        save_page_artifacts(page_result, inputs.pages_dir, page_num)

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
            'file': (page_file.relative_to(inputs.output_dir)).as_posix(),
            'char_count': len(page_result.markdown),
            'html_file': (
                (inputs.pages_dir / f"page_{page_num:03d}.html").relative_to(inputs.output_dir).as_posix()
                if page_result.html is not None
                else None
            ),
            'artifact_file': (
                (inputs.pages_dir / f"page_{page_num:03d}.ocr.json").relative_to(inputs.output_dir).as_posix()
            ),
        }

        # Empty OCR output is not silently accepted.  It can be a real blank
        # page, but that requires explicit acknowledgement.
        if empty_result and not inputs.allow_empty_pages:
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


def _save_page_progress(inputs: _PageRunInputs) -> None:
    """Persist sorted progress and per-page stats after each completion."""
    progress = inputs.progress
    progress["pages_processed"] = sorted(set(progress["pages_processed"]))
    progress["failed_pages"] = sorted(set(progress["failed_pages"]))
    progress["empty_pages"] = sorted(set(progress["empty_pages"]))
    progress["allowed_empty_pages"] = sorted(
        set(progress["allowed_empty_pages"])
    )
    progress["missing_pages"] = sorted(
        set(range(1, inputs.total_pages + 1))
        - set(progress["pages_processed"])
    )
    atomic_write_json(inputs.progress_file, progress)
    atomic_write_json(inputs.page_stats_file, inputs.page_stats)


def _run_page_workers(inputs: _PageRunInputs) -> None:
    """OCR the pending pages in parallel and persist progress per page."""
    from concurrent.futures import ThreadPoolExecutor, as_completed

    if not inputs.pages_to_process:
        logger.info("All pages already processed")
        return
    logger.info(f"Processing {len(inputs.pages_to_process)} pages...")

    with ThreadPoolExecutor(max_workers=inputs.max_workers) as executor:
        futures = {
            executor.submit(_ocr_pending_page, inputs, page_num): page_num
            for page_num in inputs.pages_to_process
        }

        for future in as_completed(futures):
            _apply_page_result(inputs, future.result())
            _save_page_progress(inputs)


def _resume_page_progress(
    pages_dir: Path,
    *,
    ocr_pdf: Path,
    backend: str,
    resume: bool,
    start_page: int,
    end_page: Optional[int],
    total_pages: int,
    retry_pages: Optional[list[int]],
) -> tuple[Dict[str, Any], Dict[str, Any], int]:
    """Load the page checkpoint, apply retry pages, and clamp the range."""
    source_sha256 = sha256_file(ocr_pdf)
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

    return progress, page_stats, end_page


def _collect_pages_to_process(
    pages_dir: Path,
    output_dir: Path,
    *,
    start_page: int,
    end_page: int,
    resume: bool,
    progress: Dict[str, Any],
    page_stats: Dict[str, Any],
) -> list[int]:
    """Collect pending pages and backfill missing token statistics."""
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

    return pages_to_process


@dataclass(frozen=True)
class _PagewiseSummaryInputs:
    """State for the final page-wise OCR summary."""

    progress: Dict[str, Any]
    page_stats: Dict[str, Any]
    total_pages: int
    progress_file: Path
    page_stats_file: Path
    ocr_pdf: Path
    output_dir: Path
    pages_dir: Path
    backend: str
    config: Dict[str, Any]
    max_workers: int
    resume: bool
    secondary_preflight: Optional[Dict[str, Any]]


def _finish_pagewise(inputs: _PagewiseSummaryInputs) -> Dict[str, Any]:
    """Write the final checkpoint and report the page-wise result."""
    progress = inputs.progress
    page_stats = inputs.page_stats
    total_pages = inputs.total_pages
    progress_file = inputs.progress_file
    page_stats_file = inputs.page_stats_file
    ocr_pdf = inputs.ocr_pdf
    output_dir = inputs.output_dir
    pages_dir = inputs.pages_dir
    backend = inputs.backend
    config = inputs.config
    max_workers = inputs.max_workers
    resume = inputs.resume
    secondary_preflight = inputs.secondary_preflight
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
    atomic_write_json(progress_file, progress)
    atomic_write_json(page_stats_file, page_stats)

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
        "secondary_preflight": secondary_preflight,
        "progress_file": progress_file,
    }


def ocr_full_book_pagewise(
    pdf_path: Path,
    output_dir: Path,
    session: Any = None,
    project_id: str = None,
    location: str = None,
    start_page: int = 1,
    end_page: int = None,
    backend: str = DEFAULT_PRIMARY_OCR_BACKEND,
    api_key: str = None,
    base_url: str = None,
    resume: bool = False,
    config: Dict = None,
    max_workers: int = 5,
    allow_empty_pages: bool = False,
    retry_pages: Optional[List[int]] = None,
    allow_slow_secondary: bool = False,
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
        api_key: Retained for compatibility with legacy callers.
        base_url: Retained for compatibility with legacy callers.
        resume: Resume from previous progress
        config: Configuration dict (for retry settings)
        max_workers: Number of parallel OCR requests (default: 5)
        allow_empty_pages: Explicitly acknowledge pages with no OCR payload
            after manual inspection.
        retry_pages: Specific physical pages to re-OCR even when their
            checkpoint currently says they are complete.
        allow_slow_secondary: Explicitly accept a secondary OCR estimate above
            the configured preflight limit.
    """
    backend = str(backend or "").strip().lower()
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

    secondary_preflight = preflight_secondary_ocr(
        output_dir,
        total_pages,
        config,
        allow_slow=allow_slow_secondary,
    )

    progress_file = pages_dir / "ocr_progress.json"
    page_stats_file = pages_dir / "page_stats.json"
    progress, page_stats, end_page = _resume_page_progress(
        pages_dir,
        ocr_pdf=ocr_pdf,
        backend=backend,
        resume=resume,
        start_page=start_page,
        end_page=end_page,
        total_pages=total_pages,
        retry_pages=retry_pages,
    )
    logger.info(f"Processing pages {start_page}-{end_page} (total: {end_page - start_page + 1} pages, {max_workers} workers)")

    pages_to_process = _collect_pages_to_process(
        pages_dir, output_dir,
        start_page=start_page,
        end_page=end_page,
        resume=resume,
        progress=progress,
        page_stats=page_stats,
    )
    _run_page_workers(_PageRunInputs(
        pages_to_process=pages_to_process,
        ocr_pdf=ocr_pdf,
        session=session,
        project_id=project_id,
        location=location,
        images_dir=images_dir,
        pages_dir=pages_dir,
        output_dir=output_dir,
        backend=backend,
        api_key=api_key,
        base_url=base_url,
        config=config,
        max_workers=max_workers,
        max_retries=max_retries,
        initial_backoff=initial_backoff,
        allow_empty_pages=allow_empty_pages,
        progress=progress,
        page_stats=page_stats,
        progress_file=progress_file,
        page_stats_file=page_stats_file,
        total_pages=total_pages,
    ))
    return _finish_pagewise(_PagewiseSummaryInputs(
        progress=progress,
        page_stats=page_stats,
        total_pages=total_pages,
        progress_file=progress_file,
        page_stats_file=page_stats_file,
        ocr_pdf=ocr_pdf,
        output_dir=output_dir,
        pages_dir=pages_dir,
        backend=backend,
        config=config,
        max_workers=max_workers,
        resume=resume,
        secondary_preflight=secondary_preflight,
    ))
