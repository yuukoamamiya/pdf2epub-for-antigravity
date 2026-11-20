"""OCR backend registry and compatibility accessors.

The page-oriented backends are discovered lazily so optional provider SDKs do
not become import-time requirements for the default Chandra workflow.
"""

from dataclasses import dataclass
from importlib import import_module
from typing import Callable, Optional


@dataclass(frozen=True)
class OCRBackendSpec:
    """Describe one OCR backend without importing provider clients eagerly.

    ``chunk_processor`` is the compatibility interface used by the legacy
    VLLM adapter.  Chandra has a richer native page result and therefore uses
    ``native_page_processor``.
    """

    name: str
    chunk_processor: Optional[Callable] = None
    native_page_processor: Optional[Callable] = None


_BACKEND_NAMES = ("vllm", "chandra")


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

    raise ValueError(f"Unknown backend: {backend_name}. Supported: {', '.join(_BACKEND_NAMES)}")


def _lazy_backend_callable(module_name: str, attribute: str) -> Callable:
    """Resolve an optional provider module only when the callable is used."""

    def call(*args, **kwargs):
        module = import_module(module_name)
        return getattr(module, attribute)(*args, **kwargs)

    return call


def supported_backends() -> tuple[str, ...]:
    """Return the backend names accepted by the page OCR workflow."""
    return _BACKEND_NAMES


__all__ = ["OCRBackendSpec", "get_backend_spec", "supported_backends"]
