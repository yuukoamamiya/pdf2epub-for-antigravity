"""Coverage for local credential path resolution and JSON reads."""

import json
from pathlib import Path

from pdf2epub.local_credentials import (
    local_credential_path,
    local_credentials_dir,
    read_local_json,
)


def test_local_credentials_dir_uses_configured_absolute_directory(tmp_path: Path):
    config = {"credentials": {"local_dir": str(tmp_path / "secrets")}}
    assert local_credentials_dir(config) == tmp_path / "secrets"


def test_read_local_json_returns_object(tmp_path: Path):
    secret_dir = tmp_path / ".secrets"
    secret_dir.mkdir()
    (secret_dir / "provider.json").write_text(
        json.dumps({"client_id": "id", "client_secret": "secret"}), encoding="utf-8"
    )

    config = {"credentials": {"local_dir": str(secret_dir)}}

    assert local_credential_path(config, "provider.json") == secret_dir / "provider.json"
    assert read_local_json(config, "provider.json") == {
        "client_id": "id",
        "client_secret": "secret",
    }


def test_read_local_json_returns_none_for_missing_file(tmp_path: Path):
    config = {"credentials": {"local_dir": str(tmp_path)}}
    assert read_local_json(config, "absent.json") is None
