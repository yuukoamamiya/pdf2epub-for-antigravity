import json
from pathlib import Path

import pytest

from pdf2epub.local_credentials import LocalCredentialsError
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


def test_chandra_rejects_corrupt_credentials_instead_of_sending_401(
    tmp_path: Path, monkeypatch
):
    secret_dir = tmp_path / ".secrets"
    secret_dir.mkdir()
    (secret_dir / "chandra-access.json").write_text("{broken", encoding="utf-8")
    monkeypatch.delenv("CHANDRA_CF_ACCESS_CLIENT_ID", raising=False)
    monkeypatch.delenv("CHANDRA_CF_ACCESS_CLIENT_SECRET", raising=False)
    monkeypatch.setattr(chandra, "OpenAI", lambda **kwargs: None)

    with pytest.raises(LocalCredentialsError, match="invalid Chandra credentials"):
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
