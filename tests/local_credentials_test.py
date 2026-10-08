import json
from pathlib import Path

from pdf2epub.ocr.backends import chandra
from pdf2epub.ocr_pages import _resolve_mistral_credentials


def test_mistral_resolver_reads_gitignored_local_file(tmp_path: Path, monkeypatch):
    secret_dir = tmp_path / ".secrets"
    secret_dir.mkdir()
    (secret_dir / "mistral_api_key").write_text("local-mistral-key\n", encoding="utf-8")
    monkeypatch.delenv("MISTRAL_API_KEY", raising=False)

    key, base_url = _resolve_mistral_credentials(
        {
            "credentials": {"local_dir": str(secret_dir)},
            "ocr": {"backends": {"mistral": {}}},
        }
    )

    assert key == "local-mistral-key"
    assert base_url == "https://api.mistral.ai/v1"


def test_chandra_reads_local_json_credentials(tmp_path: Path, monkeypatch):
    secret_dir = tmp_path / ".secrets"
    secret_dir.mkdir()
    (secret_dir / "chandra-access.json").write_text(
        json.dumps({"client_id": "local-client", "client_secret": "local-secret"}),
        encoding="utf-8",
    )
    captured = {}

    class FakeOpenAI:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    monkeypatch.setattr(chandra, "OpenAI", FakeOpenAI)
    chandra.ChandraClient(
        {
            "credentials": {"local_dir": str(secret_dir)},
            "ocr": {
                "backends": {
                    "chandra": {
                        "base_url": "https://chandra.example/v1",
                        "credentials_file": "chandra-access.json",
                    }
                }
            },
        }
    )

    assert captured["default_headers"] == {
        "CF-Access-Client-Id": "local-client",
        "CF-Access-Client-Secret": "local-secret",
    }
