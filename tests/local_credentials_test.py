import json
from pathlib import Path

from pdf2epub.ocr.backends import chandra


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
