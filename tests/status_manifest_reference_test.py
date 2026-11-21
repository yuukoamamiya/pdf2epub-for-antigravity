import hashlib
import json
from pathlib import Path

from pdf2epub.commands.diagnostics import _review_gates


def _write(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload), encoding="utf-8")


def _manifest(output_dir: Path, *, source_hashes: dict[str, str]) -> None:
    _write(
        output_dir / "translate_subagent_manifest.json",
        {
            "status": "pending_review",
            "pending_files": ["chapter_1.md", "chapter_2.md"],
            "pending_reasons": {
                "chapter_1.md": "source_changed",
                "chapter_2.md": "source_changed",
            },
            "source_sha256": source_hashes,
            "worker_handoffs": [{"worker_id": "w1", "dispatch_status": "available"}],
            "dispatch_plan": [{"wave": 1}],
        },
    )


def test_review_gates_reference_manifest_reasons_and_validation(tmp_path: Path):
    _manifest(
        tmp_path,
        source_hashes={"chapter_1.md": "a", "chapter_2.md": "b"},
    )
    _write(
        tmp_path / "translate_validation.json",
        {
            "valid_files": ["chapter_1.md"],
            "source_sha256": {"chapter_1.md": "a", "chapter_2.md": "b"},
        },
    )

    gate = {item["name"]: item for item in _review_gates(tmp_path)}["translate"]

    assert gate["pending_reasons"] == {
        "chapter_1.md": "source_changed",
        "chapter_2.md": "source_changed",
    }
    # The counter comes from the resume taxonomy, not a diagnostics copy.
    assert gate["pending_reason_counts"] == {"source_changed": 2}
    assert gate["validation"]["completed"] == ["chapter_1.md"]
    assert gate["validation"]["pending"] == ["chapter_2.md"]
    assert gate["source_manifest_sha"] == hashlib.sha256(
        (tmp_path / "translate_subagent_manifest.json").read_bytes()
    ).hexdigest()
    assert gate["status"] == "pending_review"
    assert gate["waves"] == 1


def test_review_gates_flag_manifest_validation_source_mismatch(tmp_path: Path):
    _manifest(tmp_path, source_hashes={"chapter_1.md": "new-hash"})
    _write(
        tmp_path / "translate_validation.json",
        {
            "valid_files": ["chapter_1.md"],
            "source_sha256": {"chapter_1.md": "old-hash"},
        },
    )

    gate = {item["name"]: item for item in _review_gates(tmp_path)}["translate"]

    assert gate["status"] == "stale_manifest"
    assert gate["needs_subagent"] is True
    assert "rerun translate" in gate["detail"]
