"""Coverage for local credential path resolution and JSON reads."""

import json
from pathlib import Path

import pytest

from pdf2epub.local_credentials import (
    LocalCredentialsError,
    local_credential_path,
    local_credentials_dir,
    read_json_file,
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


def test_read_local_json_rejects_corrupt_json(tmp_path: Path):
    config = {"credentials": {"local_dir": str(tmp_path)}}
    (tmp_path / "broken.json").write_text("{not json", encoding="utf-8")

    with pytest.raises(LocalCredentialsError) as excinfo:
        read_local_json(config, "broken.json")

    message = str(excinfo.value)
    assert "not valid JSON" in message
    assert "broken.json" in message
    assert "{not json" not in message


def test_read_local_json_rejects_non_object_root(tmp_path: Path):
    config = {"credentials": {"local_dir": str(tmp_path)}}
    (tmp_path / "array.json").write_text('["secret"]', encoding="utf-8")

    with pytest.raises(LocalCredentialsError, match="must contain a JSON object"):
        read_local_json(config, "array.json")


def test_read_local_json_rejects_unreadable_path(tmp_path: Path):
    (tmp_path / "folder.json").mkdir()

    with pytest.raises(LocalCredentialsError, match="cannot read"):
        read_json_file(tmp_path / "folder.json")


def test_local_credential_path_rejects_parent_traversal(tmp_path: Path):
    config = {"credentials": {"local_dir": str(tmp_path)}}
    with pytest.raises(ValueError):
        local_credential_path(config, "../escape.json")
