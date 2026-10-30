from __future__ import annotations

import json
import importlib.util
import shutil
import subprocess
import uuid
from pathlib import Path

import pytest
import yaml


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "sanitize_output.sh"
GIT_BASH = Path(r"C:\Users\AmamiyaYuuko\scoop\apps\git\current\bin\bash.exe")
SANITIZER_SPEC = importlib.util.spec_from_file_location(
    "sanitize_output", REPO_ROOT / "scripts" / "sanitize_output.py"
)
assert SANITIZER_SPEC is not None and SANITIZER_SPEC.loader is not None
SANITIZER = importlib.util.module_from_spec(SANITIZER_SPEC)
SANITIZER_SPEC.loader.exec_module(SANITIZER)


def _bash() -> str:
    if GIT_BASH.exists():
        return str(GIT_BASH)
    bash = shutil.which("bash")
    if bash is None:
        pytest.skip("Bash is required to exercise the shell entry point")
    return bash


def _inventory(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file() and not path.is_symlink()
    }


def _invoke(cwd: Path, source: Path, destination: Path | None = None) -> subprocess.CompletedProcess[str]:
    command = [_bash(), str(SCRIPT), str(source)]
    if destination is not None:
        command.append(str(destination))
    return subprocess.run(command, cwd=cwd, text=True, capture_output=True, check=False)


def test_shell_entry_creates_sanitized_copy_and_preserves_source_and_cwd_configs(tmp_path: Path) -> None:
    source = tmp_path / "project" / "output"
    (source / "nested").mkdir(parents=True)
    (source / "nested" / "report.json").write_text(
        json.dumps(
            {
                "api_key": "sk-" + "x" * 32,
                "authorization": "Bearer " + "y" * 28,
                "proxy": "https://api.shenshei.fans/v1/private",
                "path": r"C:\Users\Private\account\file.txt",
            }
        ),
        encoding="utf-8",
    )
    (source / "nested" / "input.epub").write_bytes(b"synthetic epub bytes")
    (source / "input_original.pdf").write_bytes(b"synthetic pdf bytes")
    (source / "config.yaml").write_text("api_key: " + "z" * 28, encoding="utf-8")
    (source / "config_local.yaml").write_text("ignored: true\n", encoding="utf-8")
    (source / "book.epub").write_bytes(b"\x00binary\xffpayload")
    before = _inventory(source)

    cwd = tmp_path / "elsewhere"
    cwd.mkdir()
    (cwd / "config.yaml").write_text("keep root config\n", encoding="utf-8")
    (cwd / "config_private.yaml").write_text("keep private config\n", encoding="utf-8")
    cwd_configs_before = _inventory(cwd)
    destination = tmp_path / "export" / "sanitized"

    result = _invoke(cwd, source, destination)

    assert result.returncode == 0, result.stderr
    assert destination.is_dir()
    assert _inventory(source) == before
    assert _inventory(cwd) == cwd_configs_before
    assert (destination / "book.epub").read_bytes() == b"\x00binary\xffpayload"
    assert not (destination / "config.yaml").exists()
    assert not (destination / "config_local.yaml").exists()
    assert not (destination / "nested" / "input.epub").exists()
    assert not (destination / "input_original.pdf").exists()
    safe_json = json.loads((destination / "nested" / "report.json").read_text(encoding="utf-8"))
    serialized = json.dumps(safe_json)
    for secret in ("x" * 32, "y" * 28, "api.shenshei.fans", "Private"):
        assert secret not in serialized
    assert "[REDACTED]" in serialized
    assert "[REDACTED_URL]" in serialized
    assert "[REDACTED_PATH]" in serialized


def test_default_destination_is_fresh_export_copy(tmp_path: Path) -> None:
    source = tmp_path / "output"
    source.mkdir()
    (source / "artifact.bin").write_bytes(b"unchanged")
    cwd = tmp_path / "caller"
    cwd.mkdir()

    result = _invoke(cwd, source)

    assert result.returncode == 0, result.stderr
    export_path = Path(result.stdout.split(" at ", 1)[1].splitlines()[0])
    assert export_path.is_dir()
    assert export_path.parent == source.parent / ".work" / "sanitized-exports"
    assert (export_path / "artifact.bin").read_bytes() == b"unchanged"
    assert (source / "artifact.bin").read_bytes() == b"unchanged"


def test_escaped_json_strings_are_redacted_structurally(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    raw = json.dumps(
        {
            "nested": [
                {"path": 'C:\\Users\\alice\\quoted"file\\tail', "token": "sk-" + "a" * 28},
                {"note": 'literal \\ and " quote'},
            ]
        }
    )
    (source / "escaped.json").write_text(raw, encoding="utf-8")
    destination = tmp_path / "export"

    result = _invoke(tmp_path, source, destination)

    assert result.returncode == 0, result.stderr
    exported_text = (destination / "escaped.json").read_text(encoding="utf-8")
    exported = json.loads(exported_text)
    assert "alice" not in exported_text
    assert "a" * 28 not in exported_text
    assert exported["nested"][0]["path"].startswith("[REDACTED_PATH]")
    assert exported["nested"][1]["note"] == 'literal \\ and " quote'


def test_yaml_redaction_preserves_types_and_quoted_and_plain_values(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "settings.yaml").write_text(
        "api_key: sk-" + "a" * 28 + "\nquoted: 'Bearer " + "b" * 28 + "'\n"
        "enabled: true\ncount: 3\nitems: [one, two]\n",
        encoding="utf-8",
    )
    destination = tmp_path / "export"

    result = _invoke(tmp_path, source, destination)

    assert result.returncode == 0, result.stderr
    value = yaml.safe_load((destination / "settings.yaml").read_text(encoding="utf-8"))
    assert value["api_key"] == "[REDACTED]"
    assert value["quoted"] == "Bearer [REDACTED]"
    assert value["enabled"] is True
    assert value["count"] == 3
    assert value["items"] == ["one", "two"]


def test_generic_sensitive_json_values_are_redacted_recursively(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    plain_api_secret = "p" * 25
    authorization_secret = str(uuid.uuid4())
    token_secret = f"synthetic-token-{uuid.uuid4().hex}"
    document = {
        "api_key": plain_api_secret,
        "nested": {
            "Authorization": authorization_secret,
            "service_access_token": token_secret,
            "count": 7,
            "enabled": False,
        },
    }
    (source / "secrets.json").write_text(json.dumps(document), encoding="utf-8")
    destination = tmp_path / "export"

    result = _invoke(tmp_path, source, destination)

    assert result.returncode == 0, result.stderr
    exported = json.loads((destination / "secrets.json").read_text(encoding="utf-8"))
    assert exported["api_key"] == "[REDACTED]"
    assert exported["nested"]["Authorization"] == "[REDACTED]"
    assert exported["nested"]["service_access_token"] == "[REDACTED]"
    assert exported["nested"]["count"] == 7
    assert exported["nested"]["enabled"] is False


def test_generic_sensitive_yaml_values_are_redacted_recursively(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    uuid_secret = str(uuid.uuid4())
    (source / "secrets.yaml").write_text(
        "api-key: plain-api-secret-value\n"
        f"nested:\n  Authorization: {uuid_secret}\n"
        "  refresh_token: [plain-refresh-secret, another-secret]\n"
        "  count: 9\n  enabled: false\n",
        encoding="utf-8",
    )
    destination = tmp_path / "export"

    result = _invoke(tmp_path, source, destination)

    assert result.returncode == 0, result.stderr
    exported = yaml.safe_load((destination / "secrets.yaml").read_text(encoding="utf-8"))
    assert exported["api-key"] == "[REDACTED]"
    assert exported["nested"]["Authorization"] == "[REDACTED]"
    assert exported["nested"]["refresh_token"] == ["[REDACTED]", "[REDACTED]"]
    assert exported["nested"]["count"] == 9
    assert exported["nested"]["enabled"] is False


def test_malformed_structured_file_fails_without_changing_source(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "broken.json").write_text('{"path": "unterminated value}', encoding="utf-8")
    before = _inventory(source)
    destination = tmp_path / "export"

    result = _invoke(tmp_path, source, destination)

    assert result.returncode != 0
    assert "invalid JSON" in result.stderr
    assert _inventory(source) == before


def test_junction_detection_guard_is_exercised_with_synthetic_path(tmp_path: Path, monkeypatch) -> None:
    source = tmp_path / "source"
    source.mkdir()
    junction = source / "junction"
    junction.mkdir()
    monkeypatch.setattr(SANITIZER, "_is_reparse_point", lambda path: path == junction)

    with pytest.raises(SANITIZER.ExportError, match="symlink or junction"):
        SANITIZER._validate_tree(source)


@pytest.mark.parametrize("destination_kind", ["inside", "ancestor", "existing"])
def test_unsafe_or_existing_destination_fails_before_writes(tmp_path: Path, destination_kind: str) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "safe.txt").write_text("synthetic", encoding="utf-8")
    if destination_kind == "inside":
        destination = source / "export"
    elif destination_kind == "ancestor":
        destination = tmp_path
    else:
        destination = tmp_path / "already-there"
        destination.mkdir()
        (destination / "keep.txt").write_text("keep", encoding="utf-8")
    before_source = _inventory(source)
    before_destination = _inventory(destination) if destination.exists() else None

    result = _invoke(tmp_path, source, destination)

    assert result.returncode != 0
    assert _inventory(source) == before_source
    if before_destination is not None:
        assert _inventory(destination) == before_destination
    if destination_kind == "inside":
        assert not destination.exists()


def test_symlink_source_entry_is_rejected_without_creating_export(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("synthetic external data", encoding="utf-8")
    link = source / "external.txt"
    try:
        link.symlink_to(outside)
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation is not available")
    destination = tmp_path / "export"

    result = _invoke(tmp_path, source, destination)

    assert result.returncode != 0
    assert "symlink or junction" in result.stderr
    assert outside.read_text(encoding="utf-8") == "synthetic external data"
    assert not destination.exists()
