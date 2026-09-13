"""OCR backend registry and compatibility accessors.

The project historically had two routing layers: the legacy chunk adapters in
``pdf2epub.ocr_backends`` and the newer page-oriented backends in this
package.  Keep the old ``get_backend`` API for callers that need it, but make
``get_backend_spec`` the single discovery point for the page OCR pipeline.
"""

from dataclasses import dataclass
from typing import Callable, Optional, Tuple


@dataclass(frozen=True)
class OCRBackendSpec:
    """Describe one OCR backend without importing provider clients eagerly.

    ``chunk_processor`` is the compatibility interface used by the original
    Mistral/Vertex/VLLM adapters.  ``image_page_processor`` is the common
    interface used by Azure and Google Vision.  Chandra has a richer native
    page result and therefore uses ``native_page_processor``.
    """

    name: str
    chunk_processor: Optional[Callable] = None
    init_client: Optional[Callable] = None
    image_page_processor: Optional[Callable] = None
    native_page_processor: Optional[Callable] = None


_BACKEND_NAMES = ("mistral", "vertex", "vllm", "azure", "vision", "chandra")


def get_backend(backend_name: str) -> Tuple[Callable, Callable]:
    """
    Get the init_client and process_page functions for a backend.
    
    Args:
        backend_name: Name of the backend ('azure', 'vision', or 'vllm')
        
    Returns:
        Tuple of (init_client, process_page) functions
        
    Raises:
        ValueError: If backend_name is not recognized
    """
    if backend_name == 'azure':
        from .azure import init_client, process_page
    elif backend_name == 'vision':
        from .vision import init_client, process_page
    elif backend_name == 'vllm':
        from .vllm import init_client, process_page
    else:
        raise ValueError(f"Unknown backend: {backend_name}")
    
    return init_client, process_page


def get_backend_spec(backend_name: str) -> OCRBackendSpec:
    """Return the normalized backend description used by page OCR.

    Imports stay lazy so installing or using one provider does not eagerly
    initialize every optional SDK.  The legacy adapters remain the source of
    truth for the chunk-shaped Mistral, Vertex, and VLLM calls.
    """
    name = str(backend_name or "").strip().lower()
    if name not in _BACKEND_NAMES:
        supported = ", ".join(_BACKEND_NAMES)
        raise ValueError(f"Unknown backend: {backend_name}. Supported: {supported}")

    if name in {"mistral", "vertex", "vllm"}:
        from pdf2epub.ocr_backends import (
            ocr_pdf_chunk_mistral,
            ocr_pdf_chunk_vertex,
            ocr_pdf_chunk_vllm,
        )

        processors = {
            "mistral": ocr_pdf_chunk_mistral,
            "vertex": ocr_pdf_chunk_vertex,
            "vllm": ocr_pdf_chunk_vllm,
        }
        return OCRBackendSpec(name=name, chunk_processor=processors[name])

    if name == "chandra":
        from .chandra import process_pdf_page

        return OCRBackendSpec(name=name, native_page_processor=process_pdf_page)

    init_client, process_page = get_backend(name)
    return OCRBackendSpec(
        name=name,
        init_client=init_client,
        image_page_processor=process_page,
    )


def supported_backends() -> tuple[str, ...]:
    """Return the backend names accepted by the page OCR workflow."""
    return _BACKEND_NAMES


__all__ = ["OCRBackendSpec", "get_backend", "get_backend_spec", "supported_backends"]
