"""Incremental TOC heading binding and merged dry-run readiness."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from pdf2epub.commands import markdown as markdown_command
from pdf2epub.commands.markdown import _aggregate_toc_heading_bindings
from pdf2epub.toc_model import TOC_HEADING_CONTEXT_PROJECTION_VERSION


def _context(title: str, node_id: str, anchor: str) -> dict:
    return {
        "projection_version": TOC_HEADING_CONTEXT_PROJECTION_VERSION,
        "node_id": node_id,
        "anchor": anchor,
        "toc_title": title,
        "binding_mode": "visible",
        "children": [],
    }


def _write_manifest_and_targets(output_dir: Path) -> None:
    (output_dir / "translated" / "validated").mkdir(parents=True)
    (output_dir / "translated" / "validated" / "a.md").write_text(
        "# Chapter A\n\nTranslated paragraph.", encoding="utf-8"
    )
    (output_dir / "translate_subagent_manifest.json").write_text(
        json.dumps(
            {
                "toc_heading_contexts": {
                    "a.md": _context("Chapter A", "n1", "a1"),
                    "b.md": _context("Chapter B", "n2", "a2"),
                }
            }
        ),
        encoding="utf-8",
    )


def test_incremental_binding_checks_only_validated_files(tmp_path: Path):
    _write_manifest_and_targets(tmp_path)
    report = {"valid_files": ["a.md"], "missing": ["b.md"]}

    heading = _aggregate_toc_heading_bindings(tmp_path, report)

    assert heading["valid"] is True
    assert heading["pending_files"] == ["b.md"]


def test_final_binding_still_checks_the_whole_book(tmp_path: Path):
    _write_manifest_and_targets(tmp_path)
    report = {"valid_files": ["a.md"], "missing": ["b.md"]}

    heading = _aggregate_toc_heading_bindings(
        tmp_path, report, force_full_final=True
    )

    assert heading["valid"] is False


def test_translate_dry_run_previews_even_when_readiness_blocked(monkeypatch):
    monkeypatch.setattr(
        markdown_command, "load_config", lambda _path: {"pipeline": "pdf_translation"}
    )
    monkeypatch.setattr(
        markdown_command,
        "run_readiness_check",
        lambda *_args, **_kwargs: {
            "stage": "translate",
            "ready": False,
            "checks": {},
            "errors": [],
            "warnings": [],
        },
    )
    calls: dict[str, str] = {}

    def fake_prepare(_args, task):
        calls["task"] = task
        return 0

    monkeypatch.setattr(markdown_command, "_prepare_pdf_markdown_task", fake_prepare)
    result = markdown_command.translate_command(
        SimpleNamespace(config="config.yaml", dry_run=True, skip_entities=False)
    )

    assert result == 0
    assert calls == {"task": "translate"}
