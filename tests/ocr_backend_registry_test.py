import pytest
from types import SimpleNamespace

from pdf2epub.ocr.backends import (
    get_backend_spec,
    supported_backends,
)
import pdf2epub.ocr_pages as ocr_pages


def test_registry_exposes_all_page_ocr_backends():
    assert set(supported_backends()) == {
        "mistral",
        "vertex",
        "vllm",
        "azure",
        "vision",
        "chandra",
    }
    assert get_backend_spec("mistral").chunk_processor is not None
    assert get_backend_spec("vertex").chunk_processor is not None
    assert get_backend_spec("vllm").chunk_processor is not None
    assert get_backend_spec("azure").image_page_processor is not None
    assert get_backend_spec("vision").image_page_processor is not None
    assert get_backend_spec("chandra").native_page_processor is not None


def test_registry_normalizes_names_and_rejects_unknown_backend():
    assert get_backend_spec(" CHANDRA ").name == "chandra"
    with pytest.raises(ValueError, match="Unknown backend"):
        get_backend_spec("not-a-backend")


def test_page_pipeline_dispatches_normalized_chunk_backend(monkeypatch):
    calls = {}

    def process(**kwargs):
        calls.update(kwargs)
        return "text", [], 1

    monkeypatch.setattr(
        ocr_pages,
        "get_backend_spec",
        lambda name: SimpleNamespace(chunk_processor=process),
    )

    result = ocr_pages.ocr_pdf_chunk(
        b"pdf", backend=" MISTRAL ", api_key="test-key"
    )

    assert result == ("text", [], 1)
    assert calls["api_key"] == "test-key"
