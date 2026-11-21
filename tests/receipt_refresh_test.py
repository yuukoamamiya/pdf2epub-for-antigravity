"""Version-only receipt refresh for Markdown hand-off resumes."""

from __future__ import annotations

import json
from pathlib import Path

from pdf2epub.markdown_handoff import prepare_markdown_subagent
from pdf2epub.markdown_handoff_context import MarkdownHandoffRequest
from pdf2epub.workflow_contracts import MARKDOWN_VALIDATION_SCHEMA_VERSION, sha256_file


def _request(
    tmp_path: Path, *, heading: dict, refresh_checkpoints: bool = True
) -> MarkdownHandoffRequest:
    source_dir = tmp_path / "source"
    target_dir = tmp_path / "target"
    source_dir.mkdir()
    target_dir.mkdir()
    (source_dir / "unit.md").write_text("Source paragraph.", encoding="utf-8")
    (target_dir / "unit.md").write_text("Target paragraph.", encoding="utf-8")
    return MarkdownHandoffRequest(
        output_dir=tmp_path,
        task="translate",
        source_dir=source_dir,
        target_dir=target_dir,
        source_language="English",
        target_language="Chinese",
        resume=True,
        heading_contexts={"unit.md": heading},
        refresh_checkpoints=refresh_checkpoints,
    )


def _write_receipt(tmp_path: Path, *, heading: dict) -> None:
    source = tmp_path / "source" / "unit.md"
    target = tmp_path / "target" / "unit.md"
    (tmp_path / "translate_subagent_manifest.json").write_text(
        json.dumps(
            {
                "source_sha256": {"unit.md": sha256_file(source)},
                "toc_heading_contexts": {"unit.md": heading},
                "unit_context_sha256": {},
                "special_unit_modes": {},
            }
        ),
        encoding="utf-8",
    )
    snapshot = {
        "unit_context_sha256": {},
        "boundary_context_sha256": {},
        "toc_heading_contexts": {"unit.md": heading},
        "context_sha256": {},
        "special_unit_modes": {},
        "file_roles": {},
        "file_role_sources": {},
        "source_language": "English",
        "target_language": "Chinese",
    }
    (tmp_path / "translate_file_validation.json").write_text(
        json.dumps(
            {
                "schema_version": MARKDOWN_VALIDATION_SCHEMA_VERSION,
                "task": "translate",
                "scope": "file-checkpoints",
                "files": {
                    "unit.md": {
                        "schema_version": MARKDOWN_VALIDATION_SCHEMA_VERSION,
                        "valid": True,
                        "source_sha256": sha256_file(source),
                        "target_sha256": sha256_file(target),
                        "resume_context": snapshot,
                    }
                },
            }
        ),
        encoding="utf-8",
    )


def test_version_only_receipt_is_refreshed_without_revalidating(tmp_path: Path):
    request = _request(
        tmp_path, heading={"toc_title": "Chapter", "projection_version": 5}
    )
    _write_receipt(
        tmp_path,
        heading={"toc_title": "Chapter", "projection_version": 4},
    )

    paths = prepare_markdown_subagent(request)
    manifest = json.loads(paths["manifest"].read_text(encoding="utf-8"))

    assert manifest["pending_files"] == []
    assert manifest["refreshed_receipts"] == ["unit.md"]
    assert manifest["refresh_reason"] == "explicit"
    ledger = json.loads(
        (tmp_path / "translate_file_validation.json").read_text(encoding="utf-8")
    )
    assert (
        ledger["files"]["unit.md"]["resume_context"]["toc_heading_contexts"]["unit.md"][
            "projection_version"
        ]
        == 5
    )


def test_version_only_auto_forward_rolls_without_flag(tmp_path: Path):
    """A version stamp alone no longer needs prepare → validate → prepare."""
    request = _request(
        tmp_path,
        heading={"toc_title": "Chapter", "projection_version": 5},
        refresh_checkpoints=False,
    )
    _write_receipt(
        tmp_path,
        heading={"toc_title": "Chapter", "projection_version": 4},
    )

    paths = prepare_markdown_subagent(request)
    manifest = json.loads(paths["manifest"].read_text(encoding="utf-8"))

    assert manifest["pending_files"] == []
    assert manifest["refreshed_receipts"] == ["unit.md"]
    assert manifest["refresh_reason"] == "version_only_auto_forward_roll"


def test_changed_projection_payload_is_not_receipt_refreshed(tmp_path: Path):
    request = _request(
        tmp_path, heading={"toc_title": "New title", "projection_version": 5}
    )
    _write_receipt(
        tmp_path,
        heading={"toc_title": "Old title", "projection_version": 4},
    )

    paths = prepare_markdown_subagent(request)
    manifest = json.loads(paths["manifest"].read_text(encoding="utf-8"))

    assert manifest["pending_files"] == ["unit.md"]
    assert "refreshed_receipts" not in manifest
