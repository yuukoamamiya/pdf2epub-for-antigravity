"""Pending-reason classification for Markdown hand-off resumes.

A projection/schema upgrade and a real content change both make every unit
pending on the first ``--resume``.  ``pending_reasons`` tells an operator which
is which, without changing the reuse decision itself.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from pdf2epub.markdown_handoff import prepare_markdown_subagent
from pdf2epub.markdown_handoff_context import (
    MarkdownHandoffRequest,
    normalize_handoff_context,
)
from pdf2epub.markdown_handoff_planning import plan_markdown_handoff
from pdf2epub.markdown_handoff_resume import decide_resume, unit_context_mismatch_reason
from pdf2epub.workflow_contracts import MARKDOWN_VALIDATION_SCHEMA_VERSION, sha256_file


def _request(
    tmp_path: Path,
    *,
    task: str = "translate",
    resume: bool = True,
    heading_contexts: dict | None = None,
) -> tuple[MarkdownHandoffRequest, Path, Path]:
    source_dir = tmp_path / "source"
    target_dir = tmp_path / "target"
    source_dir.mkdir(exist_ok=True)
    target_dir.mkdir(exist_ok=True)
    source = source_dir / "unit.md"
    target = target_dir / "unit.md"
    if not source.exists():
        source.write_text("Source paragraph.", encoding="utf-8")
    if not target.exists():
        target.write_text("Target paragraph.", encoding="utf-8")
    return (
        MarkdownHandoffRequest(
            output_dir=tmp_path,
            task=task,
            source_dir=source_dir,
            target_dir=target_dir,
            source_language="English",
            target_language="Chinese",
            resume=resume,
            heading_contexts=heading_contexts,
        ),
        source,
        target,
    )


def _write_manifest(tmp_path: Path, payload: dict, *, task: str = "translate") -> None:
    (tmp_path / f"{task}_subagent_manifest.json").write_text(
        json.dumps(payload), encoding="utf-8"
    )


def _write_ledger(tmp_path: Path, source: Path, target: Path, *, task: str = "translate") -> None:
    (tmp_path / f"{task}_file_validation.json").write_text(
        json.dumps(
            {
                "schema_version": MARKDOWN_VALIDATION_SCHEMA_VERSION,
                "task": task,
                "scope": "file-checkpoints",
                "files": {
                    "unit.md": {
                        "schema_version": MARKDOWN_VALIDATION_SCHEMA_VERSION,
                        "valid": True,
                        "source_sha256": sha256_file(source),
                        "target_sha256": sha256_file(target),
                    }
                },
            }
        ),
        encoding="utf-8",
    )


def _decision(tmp_path: Path, request, source: Path):
    plan = plan_markdown_handoff(request.source_dir, request.task)
    context = normalize_handoff_context(request, ["unit.md"])
    return decide_resume(request, plan, context), source


def test_projection_version_bump_is_reported_as_version_only(tmp_path: Path):
    request, source, target = _request(
        tmp_path, heading_contexts={"unit.md": {"toc_title": "T", "projection_version": 5}}
    )
    _write_manifest(
        tmp_path,
        {
            "source_sha256": {"unit.md": sha256_file(source)},
            "toc_heading_contexts": {"unit.md": {"toc_title": "T", "projection_version": 4}},
            "unit_context_sha256": {},
            "special_unit_modes": {},
        },
    )
    _write_ledger(tmp_path, source, target)

    decision, _ = _decision(tmp_path, request, source)

    assert decision.pending_files == ("unit.md",)
    assert decision.pending_reasons == {"unit.md": "version_only"}


def test_changed_projection_payload_is_reported_as_bytes_changed(tmp_path: Path):
    request, source, target = _request(
        tmp_path,
        heading_contexts={"unit.md": {"toc_title": "New title", "projection_version": 5}},
    )
    _write_manifest(
        tmp_path,
        {
            "source_sha256": {"unit.md": sha256_file(source)},
            "toc_heading_contexts": {"unit.md": {"toc_title": "Old title", "projection_version": 5}},
            "unit_context_sha256": {},
            "special_unit_modes": {},
        },
    )
    _write_ledger(tmp_path, source, target)

    decision, _ = _decision(tmp_path, request, source)

    assert decision.pending_files == ("unit.md",)
    assert decision.pending_reasons == {"unit.md": "projection_bytes_changed"}


def test_source_change_is_reported_as_source_changed(tmp_path: Path):
    request, source, target = _request(tmp_path)
    _write_manifest(
        tmp_path,
        {
            "source_sha256": {"unit.md": "a" * 64},
            "toc_heading_contexts": {},
            "unit_context_sha256": {},
            "special_unit_modes": {},
        },
    )
    # The receipt was written against an older source as well, so reuse fails
    # and the reason must name the real cause rather than "receipt_missing".
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
                        "source_sha256": "a" * 64,
                        "target_sha256": sha256_file(target),
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    decision, _ = _decision(tmp_path, request, source)

    assert decision.pending_reasons == {"unit.md": "source_changed"}


def test_missing_receipt_is_distinguished_from_a_context_change(tmp_path: Path):
    request, source, _target = _request(tmp_path)
    _write_manifest(
        tmp_path,
        {
            "source_sha256": {"unit.md": sha256_file(source)},
            "toc_heading_contexts": {},
            "unit_context_sha256": {},
            "special_unit_modes": {},
        },
    )

    decision, _ = _decision(tmp_path, request, source)

    assert decision.pending_files == ("unit.md",)
    assert decision.pending_reasons == {"unit.md": "receipt_missing"}


def test_persistent_review_signal_is_reported_as_review_reopened(tmp_path: Path):
    request, source, target = _request(tmp_path)
    _write_manifest(
        tmp_path,
        {
            "source_sha256": {"unit.md": sha256_file(source)},
            "toc_heading_contexts": {},
            "unit_context_sha256": {},
            "special_unit_modes": {},
        },
    )
    (tmp_path / "translate_validation.json").write_text(
        json.dumps(
            {
                "schema_version": MARKDOWN_VALIDATION_SCHEMA_VERSION,
                "valid_files": [],
                "review_required": [{"file": "unit.md", "code": "bilingual_warnings"}],
                "source_sha256": {"unit.md": sha256_file(source)},
                "target_sha256": {"unit.md": sha256_file(target)},
            }
        ),
        encoding="utf-8",
    )

    decision, _ = _decision(tmp_path, request, source)

    assert decision.pending_reasons == {"unit.md": "review_reopened"}


def test_fresh_run_and_missing_target_are_reported(tmp_path: Path):
    fresh_request, source, _target = _request(tmp_path, resume=False)
    fresh_decision, _ = _decision(tmp_path, fresh_request, source)
    assert fresh_decision.pending_reasons == {"unit.md": "fresh_run"}

    request, source, target = _request(tmp_path, resume=True)
    _write_manifest(
        tmp_path,
        {
            "source_sha256": {"unit.md": sha256_file(source)},
            "toc_heading_contexts": {},
            "unit_context_sha256": {},
            "special_unit_modes": {},
        },
    )
    _write_ledger(tmp_path, source, target)
    target.unlink()

    decision, _ = _decision(tmp_path, request, source)

    assert decision.pending_reasons == {"unit.md": "target_missing"}


def test_unit_context_mismatch_reason_reports_the_changed_component():
    previous = {
        "context_sha256": {},
        "unit_context_sha256": {"a.md": "old"},
        "toc_heading_contexts": {"a.md": {"toc_title": "A", "projection_version": 5}},
        "special_unit_modes": {},
    }
    common = dict(
        previous_manifest=previous,
        controlled_files={},
        context_sha256={},
        heading_contexts={"a.md": {"toc_title": "A", "projection_version": 5}},
        resume=True,
    )

    assert (
        unit_context_mismatch_reason("a.md", unit_context_sha256={"a.md": "new"}, **common)
        == "unit_context"
    )
    assert unit_context_mismatch_reason("a.md", unit_context_sha256={"a.md": "old"}, **common) is None
    assert (
        unit_context_mismatch_reason(
            "a.md",
            unit_context_sha256={"a.md": "old"},
            **{
                **common,
                "heading_contexts": {"a.md": {"toc_title": "A", "projection_version": 4}},
            },
        )
        == "heading_context"
    )


def test_parent_manifest_exposes_pending_reasons_and_counts(tmp_path: Path):
    source_dir = tmp_path / "source"
    target_dir = tmp_path / "target"
    source_dir.mkdir()
    target_dir.mkdir()
    (source_dir / "unit.md").write_text("Source paragraph.", encoding="utf-8")

    first = prepare_markdown_subagent(
        MarkdownHandoffRequest(
            output_dir=tmp_path,
            task="translate",
            source_dir=source_dir,
            target_dir=target_dir,
            source_language="English",
            target_language="Chinese",
        )
    )
    first_manifest = json.loads(first["manifest"].read_text(encoding="utf-8"))
    assert first_manifest["pending_reasons"] == {"unit.md": "fresh_run"}
    assert first_manifest["pending_reason_counts"] == {"fresh_run": 1}

    second = prepare_markdown_subagent(
        MarkdownHandoffRequest(
            output_dir=tmp_path,
            task="translate",
            source_dir=source_dir,
            target_dir=target_dir,
            source_language="English",
            target_language="Chinese",
            resume=True,
        )
    )
    second_manifest = json.loads(second["manifest"].read_text(encoding="utf-8"))
    assert second_manifest["pending_files"] == ["unit.md"]
    # The worker never wrote the target, so the first resume must say so
    # rather than blaming the (also missing) receipt.
    assert second_manifest["pending_reasons"] == {"unit.md": "target_missing"}
    assert second_manifest["pending_reason_counts"] == {"target_missing": 1}
