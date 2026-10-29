from __future__ import annotations

import hashlib
from pathlib import Path

from pdf2epub.markdown_validation_audit import MarkdownFileInput, audit_markdown_file
from pdf2epub.markdown_validation_context import load_markdown_validation_context


def test_single_file_audit_uses_only_immutable_bytes_and_returns_normalization_plan(
    tmp_path: Path,
):
    source_dir, target_dir = tmp_path / "source", tmp_path / "target"
    source_dir.mkdir()
    target_dir.mkdir()
    (source_dir / "unit.md").write_text("Body\n", encoding="utf-8")
    (target_dir / "unit.md").write_text("```markdown\nBody\n```\n", encoding="utf-8")
    context = load_markdown_validation_context(
        tmp_path, "polish", source_dir, target_dir
    )
    original_target = (target_dir / "unit.md").read_bytes()

    result = audit_markdown_file(
        MarkdownFileInput("unit.md", b"Body\n", original_target), context.options
    )

    assert result.valid_files == ["unit.md"]
    assert result.normalized_targets["unit.md"].replace("\r\n", "\n") == "Body\n"
    assert result.target_sha256["unit.md"] == hashlib.sha256(original_target).hexdigest()
    assert (target_dir / "unit.md").read_bytes() == original_target


def test_single_file_audit_structures_decode_errors(tmp_path: Path):
    source_dir, target_dir = tmp_path / "source", tmp_path / "target"
    source_dir.mkdir()
    target_dir.mkdir()
    context = load_markdown_validation_context(
        tmp_path, "translate", source_dir, target_dir
    )

    result = audit_markdown_file(
        MarkdownFileInput("unit.md", b"source", b"target\xff"), context.options
    )

    assert result.invalid[0]["file"] == "unit.md"
    assert "UTF-8 decode error" in result.invalid[0]["reason"]
    assert result.valid_files == []


def test_unknown_file_role_is_an_explicit_audit_failure(tmp_path: Path):
    source_dir, target_dir = tmp_path / "source", tmp_path / "target"
    source_dir.mkdir()
    target_dir.mkdir()
    context = load_markdown_validation_context(
        tmp_path, "translate", source_dir, target_dir,
        file_roles={"unit.md": "invented-role"},
    )

    result = audit_markdown_file(
        MarkdownFileInput("unit.md", b"Source\n", b"Target\n"), context.options
    )

    assert result.valid_files == []
    assert result.invalid == [{
        "file": "unit.md",
        "reason": "unknown Markdown file role: invented-role",
    }]
