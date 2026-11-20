"""Read-only preview of a Markdown hand-off preparation."""

from __future__ import annotations

import json
from pathlib import Path

from pdf2epub.markdown_handoff import preview_markdown_handoff, prepare_markdown_subagent
from pdf2epub.markdown_handoff_context import MarkdownHandoffRequest


def _request(tmp_path: Path, *, resume: bool = True) -> MarkdownHandoffRequest:
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
        resume=resume,
    )


def test_preview_writes_nothing_and_matches_the_real_decision(tmp_path: Path):
    request = _request(tmp_path)

    preview = preview_markdown_handoff(request)

    assert preview["task"] == "translate"
    assert preview["pending_files"] == ["unit.md"]
    assert preview["pending_reasons"] == {"unit.md": "receipt_missing"}
    assert preview["pending_reason_counts"] == {"receipt_missing": 1}
    assert preview["worker_batches"] == [["unit.md"]]
    assert not (tmp_path / "translate_subagent_manifest.json").exists()
    assert not (tmp_path / "translate_subagent_prompt.md").exists()

    paths = prepare_markdown_subagent(request)
    manifest = json.loads(paths["manifest"].read_text(encoding="utf-8"))
    assert manifest["pending_files"] == preview["pending_files"]
    assert manifest["pending_reasons"] == preview["pending_reasons"]


def test_preview_reports_fresh_runs_without_a_resume(tmp_path: Path):
    preview = preview_markdown_handoff(_request(tmp_path, resume=False))

    assert preview["pending_reasons"] == {"unit.md": "fresh_run"}
