from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from pdf2epub.refine import footnote_normalization_plan as planning
from pdf2epub.refine.footnote_source import load_footnote_source_snapshot


def _write_source(tmp_path: Path, content: bytes = b"Body<sup>1, 2</sup>.\r\n") -> Path:
    source_dir = tmp_path / "ocr_markdown"
    source_dir.mkdir()
    (source_dir / "chapter.md").write_bytes(content)
    (source_dir / "tree_progress.json").write_text(
        json.dumps({"units": [{
            "unit_id": "chapter",
            "index_path": [1],
            "file": "chapter.md",
            "page_range": [1, 1],
        }]}),
        encoding="utf-8",
    )
    return source_dir


def test_source_snapshot_hashes_bytes_and_normalizes_only_in_memory(tmp_path: Path):
    raw = b"Body<sup>1, 2</sup>.\r\n"
    source_dir = _write_source(tmp_path, raw)

    snapshot = load_footnote_source_snapshot(tmp_path)

    assert snapshot.source_texts["chapter.md"] == "Body<sup>1</sup><sup>2</sup>.\n"
    assert snapshot.source_hashes["chapter.md"] == hashlib.sha256(raw).hexdigest()
    assert (source_dir / "chapter.md").read_bytes() == raw


def test_source_snapshot_fails_closed_when_refined_unit_file_is_missing(tmp_path: Path):
    source_dir = _write_source(tmp_path)
    (source_dir / "chapter.md").unlink()

    with pytest.raises(ValueError, match="missing mapped files"):
        load_footnote_source_snapshot(tmp_path)


def test_source_snapshot_rejects_unmapped_markdown_files(tmp_path: Path):
    source_dir = _write_source(tmp_path)
    (source_dir / "orphan.md").write_text("unmapped", encoding="utf-8")

    with pytest.raises(ValueError, match="unmapped Markdown files: orphan.md"):
        load_footnote_source_snapshot(tmp_path)


def test_plan_projects_confirmed_body_review_before_resolution_without_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    _write_source(tmp_path, b"Body.\n")
    snapshot = load_footnote_source_snapshot(tmp_path)
    report = {
        "footnote_mode": "footnotes",
        "pages": [{"page": 1, "candidates": [{
            "page": 1, "block": 2, "source": "primary", "key": "1",
            "confidence": "high",
        }]}],
        "continuation_reviews": [],
    }
    decisions = {
        "decisions": [],
        "continuation_reviews": [{
            "outcome": "confirmed_not_a_footnote",
            "source_note": {"page": 1, "block": 2, "source": "primary"},
        }],
    }
    original_report = json.loads(json.dumps(report))
    original_decisions = json.loads(json.dumps(decisions))
    captured: dict = {}

    def resolve(_output, projected_report, projected_decisions, *_args):
        captured["report"] = projected_report
        captured["decisions"] = projected_decisions
        return SimpleNamespace(
            edits_by_file={}, notes_by_chapter={}, active_by_chapter_key={},
            resolved=[], preserved_roles={}, errors=[],
        )

    monkeypatch.setattr(planning, "resolve_page_decisions", resolve)
    monkeypatch.setattr(planning, "bind_note_markers", lambda *_args: ())
    monkeypatch.setattr(planning, "transform_footnote_edit_plan", lambda plan, *_args, **_kwargs: plan)

    plan = planning.build_footnote_normalization_plan(
        planning.FootnoteNormalizationPlanningInput(
            output_dir=tmp_path, report=report, decisions=decisions,
            source=snapshot, footnote_mode="footnotes",
        )
    )

    assert plan.valid
    assert captured["report"]["pages"][0]["candidates"][0]["confidence"] == "high"
    assert captured["decisions"]["decisions"] == [{
        "page": 1, "source": "primary", "block": 2, "role": "body",
        "key": "1", "override": True,
        "_trusted_decision_source": "confirmed_not_a_footnote",
        "chapter_id": "toc-unit:chapter",
    }]
    assert captured["decisions"]["effective_decisions"][0]["decision_source"] == "confirmed_not_a_footnote"
    assert report == original_report
    assert decisions == original_decisions
    assert snapshot.source_texts["chapter.md"] == "Body.\n"


def test_plan_recovers_only_the_exact_reviewed_continuation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    _write_source(tmp_path, b"Body.\n")
    snapshot = load_footnote_source_snapshot(tmp_path)
    report = {
        "footnote_mode": "footnotes",
        "pages": [],
        "continuation_reviews": [{
            "task_id": "cont-1", "boundary_kind": "hyphen",
            "source_note": {"page": 1, "text_ending": "inter-"},
        }],
    }
    decisions = {"decisions": [], "continuation_reviews": [{
        "task_id": "cont-1", "outcome": "recovered_continuation",
        "key": "1", "text": "national",
    }]}
    note = {
        "key": "1", "source_page": 1, "source_file": "chapter.md",
        "chapter_id": "toc-unit:chapter", "parts": ["inter-"],
    }

    def resolve(*_args):
        return SimpleNamespace(
            edits_by_file={}, notes_by_chapter={"toc-unit:chapter": [note]},
            active_by_chapter_key={}, resolved=[], preserved_roles={}, errors=[],
        )

    monkeypatch.setattr(planning, "resolve_page_decisions", resolve)
    monkeypatch.setattr(planning, "bind_note_markers", lambda *_args: ())
    monkeypatch.setattr(planning, "transform_footnote_edit_plan", lambda plan, *_args, **_kwargs: plan)

    plan = planning.build_footnote_normalization_plan(
        planning.FootnoteNormalizationPlanningInput(
            output_dir=tmp_path, report=report, decisions=decisions,
            source=snapshot, footnote_mode="footnotes",
        )
    )

    assert plan.valid
    assert note["parts"] == ["international"]
    assert note["recovered_continuations"] == ["cont-1"]


def test_plan_rejects_source_or_effective_receipt_changes_since_validation(tmp_path: Path):
    _write_source(tmp_path, b"Body.\n")
    snapshot = load_footnote_source_snapshot(tmp_path)
    report = {"footnote_mode": "footnotes", "pages": []}
    no_decisions = {"decisions": [], "continuation_reviews": []}

    stale_source = planning.build_footnote_normalization_plan(
        planning.FootnoteNormalizationPlanningInput(
            output_dir=tmp_path,
            report=report,
            decisions=no_decisions,
            source=snapshot,
            footnote_mode="footnotes",
            expected_source_sha256={"chapter.md": "stale"},
        )
    )
    stale_effective = planning.build_footnote_normalization_plan(
        planning.FootnoteNormalizationPlanningInput(
            output_dir=tmp_path,
            report=report,
            decisions=no_decisions,
            source=snapshot,
            footnote_mode="footnotes",
            expected_effective_decisions=[{"page": 39, "block": 0, "role": "body"}],
        )
    )

    assert not stale_source.valid
    assert any("source Markdown changed since validation" in error for error in stale_source.errors)
    assert not stale_effective.valid
    assert any("effective footnote decisions changed since validation" in error for error in stale_effective.errors)
