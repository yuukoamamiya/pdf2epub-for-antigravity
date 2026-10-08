from types import SimpleNamespace

import pdf2epub.ocr_pages as ocr_pages


def test_mistral_page_adapter_resolves_environment_credentials(monkeypatch, tmp_path):
    calls = {}

    def fake_chunk(**kwargs):
        calls.update(kwargs)
        return "secondary text", [], 0

    monkeypatch.setattr(
        ocr_pages,
        "get_backend_spec",
        lambda _name: SimpleNamespace(
            chunk_processor=fake_chunk,
            image_page_processor=None,
            native_page_processor=None,
        ),
    )
    monkeypatch.setenv("TEST_MISTRAL_KEY", "secret-for-test")

    result = ocr_pages.ocr_pdf_page(
        b"pdf",
        backend="mistral",
        config={
            "ocr": {
                "backends": {
                    "mistral": {
                        "api_key_env": "TEST_MISTRAL_KEY",
                        "base_url": "https://mistral.example/v1",
                    }
                }
            },
            "credentials": {"local_dir": str(tmp_path)},
        },
    )

    assert result.markdown == "secondary text"
    assert calls["api_key"] == "secret-for-test"
    assert calls["base_url"] == "https://mistral.example/v1"


def test_mistral_chunk_adapter_passes_secondary_request_settings(monkeypatch):
    calls = {}

    def fake_chunk(**kwargs):
        calls.update(kwargs)
        return "text", [], 0

    monkeypatch.setattr(
        ocr_pages,
        "get_backend_spec",
        lambda _name: SimpleNamespace(chunk_processor=fake_chunk),
    )

    result = ocr_pages.ocr_pdf_chunk(
        b"pdf",
        backend="mistral",
        api_key="test-key",
        config={
            "ocr": {
                "backends": {
                    "mistral": {
                        "model": "mistral-ocr-test",
                        "include_image_base64": False,
                        "request_timeout": 17,
                    }
                }
            }
        },
    )

    assert result == ("text", [], 0)
    assert calls["model"] == "mistral-ocr-test"
    assert calls["include_image_base64"] is False
    assert calls["request_timeout"] == 17
