"""OCR backend registry and compatibility accessors.

The page-oriented backends are discovered lazily so optional provider SDKs do
not become import-time requirements for the default Chandra workflow.
"""

from dataclasses import dataclass
from importlib import import_module
from typing import Callable, Optional, Tuple


@dataclass(frozen=True)
class OCRBackendSpec:
    """Describe one OCR backend without importing provider clients eagerly.

    ``chunk_processor`` is the compatibility interface used by the legacy
    VLLM adapter.  ``image_page_processor`` is the common
    interface used by Azure and Google Vision.  Chandra has a richer native
    page result and therefore uses ``native_page_processor``.
    """

    name: str
    chunk_processor: Optional[Callable] = None
    init_client: Optional[Callable] = None
    image_page_processor: Optional[Callable] = None
    native_page_processor: Optional[Callable] = None


_BACKEND_NAMES = ("vllm", "azure", "vision", "chandra")


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
    initialize every optional SDK.  The legacy VLLM adapter remains available
    for callers that still use the chunk-shaped compatibility interface.
    """
    name = str(backend_name or "").strip().lower()
    if name not in _BACKEND_NAMES:
        supported = ", ".join(_BACKEND_NAMES)
        raise ValueError(f"Unknown backend: {backend_name}. Supported: {supported}")

    if name == "vllm":
        return OCRBackendSpec(
            name=name,
            chunk_processor=_lazy_backend_callable(
                "pdf2epub.ocr_backends", "ocr_pdf_chunk_vllm"
            ),
        )

    if name == "chandra":
        return OCRBackendSpec(
            name=name,
            native_page_processor=_lazy_backend_callable(
                "pdf2epub.ocr.backends.chandra", "process_pdf_page"
            ),
        )

    return OCRBackendSpec(
        name=name,
        init_client=_lazy_backend_callable(
            f"pdf2epub.ocr.backends.{name}", "init_client"
        ),
        image_page_processor=_lazy_backend_callable(
            f"pdf2epub.ocr.backends.{name}", "process_page"
        ),
    )


def _lazy_backend_callable(module_name: str, attribute: str) -> Callable:
    """Resolve an optional provider module only when the callable is used."""

    def call(*args, **kwargs):
        module = import_module(module_name)
        return getattr(module, attribute)(*args, **kwargs)

    return call


def supported_backends() -> tuple[str, ...]:
    """Return the backend names accepted by the page OCR workflow."""
    return _BACKEND_NAMES


__all__ = ["OCRBackendSpec", "get_backend", "get_backend_spec", "supported_backends"]
