"""The workflow stage table is the single source of stage order.

These tests fail when the table, the CLI epilog, the registered subcommands or
``pdf2epub status`` drift apart.  They are intentionally structural: the point
is that an operator reading ``--help`` cannot be told a different pipeline than
the one ``status`` reports.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

import pytest

from pdf2epub.commands.diagnostics import build_status
from pdf2epub.commands.registry import register_command_parsers
from pdf2epub.ocr_progress import new_progress
from pdf2epub.workflow_stages import (
    HTML_WORKFLOW_STAGES,
    NOVEL_WORKFLOW_STAGES,
    PDF_STAGE_BY_KEY,
    PDF_WORKFLOW_STAGES,
    TEX_WORKFLOW_STAGES,
    render_cli_epilog,
    stage_invocation,
    workflow_stages_for,
)


def _registered_commands() -> set[str]:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers()
    register_command_parsers(subparsers)
    return set(subparsers.choices)


def _config(tmp_path: Path, *, pipeline: str | None = None) -> Path:
    input_dir = tmp_path / "input"
    input_dir.mkdir(exist_ok=True)
    (input_dir / "book.pdf").write_bytes(b"%PDF-1.7 placeholder")
    lines = [
        "title: Stage Table Book",
        "input_pdf: input/book.pdf",
        "ocr:",
        "  backend: chandra",
        "  secondary:",
        "    enabled: false",
    ]
    if pipeline:
        lines.append(f"pipeline: {pipeline}")
    path = tmp_path / "config.yaml"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def test_stage_keys_are_unique_and_ordered():
    keys = [stage.key for stage in PDF_WORKFLOW_STAGES]

    assert len(keys) == len(set(keys))
    for key in ("ocr-pages", "polish", "translate", "package"):
        assert key in PDF_STAGE_BY_KEY
    assert keys[0] == "status"
    assert keys[-1] == "package"
    # Preparation must precede translation, and translation must precede the
    # delivered build.  This is the ordering AGENTS.md documents.
    assert keys.index("refine-local") < keys.index("polish") < keys.index("translate")
    assert keys.index("translate") < keys.index("package")


def test_every_stage_command_is_a_registered_subcommand():
    registered = _registered_commands()

    missing = sorted(
        {stage.command for stage in PDF_WORKFLOW_STAGES} - registered
    )
    assert missing == []


def test_cli_epilog_renders_every_stage_invocation():
    epilog = render_cli_epilog()

    for stage in PDF_WORKFLOW_STAGES:
        assert stage_invocation(stage.key) in epilog, stage.key
    assert "status" in epilog
    assert "check-ready --stage package" in epilog


def test_cli_epilog_no_longer_documents_the_legacy_build_flag():
    epilog = render_cli_epilog()

    # ``build-epub --translated`` is the bilingual/legacy variant; the delivered
    # artifact after translate-validate is built with ``--only-translated``.
    assert "build-epub --translated" not in epilog
    assert "build-epub --only-translated" in epilog


def test_diagnostics_stage_names_and_commands_match_the_table(tmp_path: Path, monkeypatch):
    config_path = _config(tmp_path)
    monkeypatch.chdir(tmp_path)

    report = build_status(config_path)
    stages = {stage["name"]: stage for stage in report["stages"]}

    unknown = set(stages) - set(PDF_STAGE_BY_KEY)
    assert unknown == set()
    for name, stage in stages.items():
        expected_command = PDF_STAGE_BY_KEY[name].command
        actual_command = str(stage["command"]).split()[0]
        assert actual_command == expected_command, name


def test_conversion_pipeline_still_reports_known_stages(tmp_path: Path, monkeypatch):
    config_path = _config(tmp_path, pipeline="epub_conversion")
    monkeypatch.chdir(tmp_path)

    report = build_status(config_path)
    names = {stage["name"] for stage in report["stages"]}

    assert names <= set(PDF_STAGE_BY_KEY)
    assert "translate" in names
    assert report["stages"][-1]["name"] == "package"


def test_non_pdf_stage_tables_cover_their_registered_commands():
    registered = _registered_commands()

    for stages in (HTML_WORKFLOW_STAGES, NOVEL_WORKFLOW_STAGES, TEX_WORKFLOW_STAGES):
        keys = [stage.key for stage in stages]
        assert len(keys) == len(set(keys))
        assert keys[0] == "status"
        missing = sorted({stage.command for stage in stages} - registered)
        assert missing == []


def test_workflow_stages_for_normalizes_pipeline_aliases():
    assert workflow_stages_for("pdf_translation") == PDF_WORKFLOW_STAGES
    assert workflow_stages_for("translation") == PDF_WORKFLOW_STAGES
    assert workflow_stages_for("pdf_conversion") == PDF_WORKFLOW_STAGES
    assert workflow_stages_for("epub_conversion") == PDF_WORKFLOW_STAGES
    assert workflow_stages_for("html") == HTML_WORKFLOW_STAGES
    assert workflow_stages_for("novel") == NOVEL_WORKFLOW_STAGES
    assert workflow_stages_for("tex") == TEX_WORKFLOW_STAGES
    with pytest.raises(KeyError, match="unknown pipeline kind"):
        workflow_stages_for("mystery")


def test_epilog_documents_html_novel_and_tex_from_the_tables():
    epilog = render_cli_epilog()

    for stages in (HTML_WORKFLOW_STAGES, NOVEL_WORKFLOW_STAGES, TEX_WORKFLOW_STAGES):
        for stage in stages:
            if stage.key == "status":
                continue
            assert stage.invocation in epilog, stage.key


def test_check_ready_rejects_non_pdf_pipelines(tmp_path: Path, monkeypatch):
    from pdf2epub.commands.markdown_readiness import run_readiness_check

    config_path = _config(tmp_path)
    text = config_path.read_text(encoding="utf-8") + "pipeline: html\n"
    config_path.write_text(text, encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    report = run_readiness_check(str(config_path))
    assert report["ready"] is False
    assert report["pipeline"] == "html"
    assert "html-prepare" in report["errors"][0]
