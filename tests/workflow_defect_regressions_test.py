"""Regression tests for the 2026-11-19 workflow execution log defect fixes."""
from __future__ import annotations

import errno
import hashlib
import json
import os
from pathlib import Path

import pytest

from pdf2epub.markdown_handoff import prepare_markdown_subagent
from pdf2epub.markdown_subagent_validation import validate_markdown_subagent
from pdf2epub.markdown_validation_persistence import validation_snapshot_changes
from pdf2epub.page_furniture_rules import candidate_for_line
from pdf2epub.polish_footnote_semantics import (
    SCHEMA_VERSION,
    build_semantic_review_contract,
    polish_issue_task_id,
    sha256_text,
    validate_prepared_file_review,
)
from pdf2epub.refine.footnote_polish_review import polish_semantic_issue_tasks
from pdf2epub.refine.footnote_unit_contexts import _project_unit_contexts
from pdf2epub.state_lock import state_lock
from pdf2epub.subagent_runtime import write_worker_handoffs


def _write_clear_sidecar(
    output_dir: Path,
    contract: dict,
    *,
    source_text: str,
    target_text: str,
) -> Path:
    sidecar_path = output_dir / contract["sidecar"]
    sidecar_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": SCHEMA_VERSION,
        "file": contract["file"],
        "source_sha256": sha256_text(source_text),
        "target_sha256": sha256_text(target_text),
        "context_sha256": contract["context_sha256"],
        "status": "clear",
        "reviewed_all": True,
        "coverage": {
            "body_reviewed": {"status": "reviewed"},
            "definitions_reviewed": {"status": "reviewed"},
            "cross_boundary_reviewed": {
                "status": "reviewed"
                if contract.get("has_boundary_context")
                else "not_applicable",
                **(
                    {}
                    if contract.get("has_boundary_context")
                    else {"reason": "single unit"}
                ),
            },
        },
        "reviewed_note_ids": list(contract["expected_note_ids"]),
        "issues": [],
    }
    sidecar_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return sidecar_path


def test_polish_resume_merges_file_validation_semantic_review_hash(tmp_path: Path):
    """Bug 1: polish --resume must honor polish-validate --file semantic review hashes."""
    source_dir = tmp_path / "footnote_normalized"
    target_dir = tmp_path / "polished_markdown"
    source_dir.mkdir()
    target_dir.mkdir()
    src_a = "Alpha paragraph with ordinary prose."
    src_b = "Beta paragraph with ordinary prose."
    (source_dir / "ch1.md").write_text(src_a, encoding="utf-8")
    (source_dir / "ch2.md").write_text(src_b, encoding="utf-8")
    (target_dir / "ch1.md").write_text(src_a, encoding="utf-8")
    (target_dir / "ch2.md").write_text(src_b, encoding="utf-8")

    prepared = prepare_markdown_subagent(
        tmp_path,
        "polish",
        source_dir,
        target_dir,
        "English",
        "English",
        footnote_semantics_enabled=True,
    )
    manifest = json.loads(prepared["manifest"].read_text(encoding="utf-8"))
    contracts = manifest["footnote_semantics"]["files"]
    _write_clear_sidecar(tmp_path, contracts["ch1.md"], source_text=src_a, target_text=src_a)

    # Full validation fails because ch2.md has no sidecar yet; ch1.md passes.
    full_report = validate_markdown_subagent(
        tmp_path,
        "polish",
        source_dir,
        target_dir,
        require_footnote_semantic_review=True,
    )
    assert full_report["valid_files"] == ["ch1.md"]
    assert not full_report["all_passed"]

    # Now finish ch2.md and run single-file validation (--file ch2.md).
    _write_clear_sidecar(tmp_path, contracts["ch2.md"], source_text=src_b, target_text=src_b)
    partial_report = validate_markdown_subagent(
        tmp_path,
        "polish",
        source_dir,
        target_dir,
        selected_files=["ch2.md"],
        require_footnote_semantic_review=True,
    )
    assert partial_report["all_passed"]
    assert partial_report["valid_files"] == ["ch2.md"]

    # Resuming polish must recognize BOTH ch1.md (full report) and ch2.md (file ledger).
    resumed = prepare_markdown_subagent(
        tmp_path,
        "polish",
        source_dir,
        target_dir,
        "English",
        "English",
        resume=True,
        footnote_semantics_enabled=True,
    )
    resumed_manifest = json.loads(resumed["manifest"].read_text(encoding="utf-8"))
    assert resumed_manifest["pending_files"] == []
    assert sorted(resumed_manifest["completed_files"]) == ["ch1.md", "ch2.md"]


def test_validation_snapshot_changes_does_not_flag_preexisting_stale_or_issues(tmp_path: Path):
    """Bug 2: unchanged stale/issues sidecars must not trigger 'changed during validation'."""
    source_dir = tmp_path / "footnote_normalized"
    target_dir = tmp_path / "polished_markdown"
    source_dir.mkdir()
    target_dir.mkdir()
    src_a = "First chapter body text."
    src_b = "Second chapter body text."
    (source_dir / "a.md").write_text(src_a, encoding="utf-8")
    (source_dir / "b.md").write_text(src_b, encoding="utf-8")
    (target_dir / "a.md").write_text(src_a, encoding="utf-8")
    (target_dir / "b.md").write_text(src_b, encoding="utf-8")

    prepared = prepare_markdown_subagent(
        tmp_path,
        "polish",
        source_dir,
        target_dir,
        "English",
        "English",
        footnote_semantics_enabled=True,
    )
    manifest = json.loads(prepared["manifest"].read_text(encoding="utf-8"))
    contracts = manifest["footnote_semantics"]["files"]

    # a.md has a stale context_sha256 sidecar; b.md has a valid clear sidecar.
    stale_contract = dict(contracts["a.md"], context_sha256="0" * 64)
    _write_clear_sidecar(tmp_path, stale_contract, source_text=src_a, target_text=src_a)
    _write_clear_sidecar(tmp_path, contracts["b.md"], source_text=src_b, target_text=src_b)

    rev_a = validate_prepared_file_review(tmp_path, "a.md", source_text=src_a, target_text=src_a, required=True)
    rev_b = validate_prepared_file_review(tmp_path, "b.md", source_text=src_b, target_text=src_b, required=True)
    assert rev_a["contract_valid"] is False
    assert rev_b["valid"] is True

    changes = validation_snapshot_changes(
        tmp_path,
        "polish",
        source_dir,
        target_dir,
        {"a.md": sha256_text(src_a), "b.md": sha256_text(src_b)},
        {"a.md": sha256_text(src_a), "b.md": sha256_text(src_b)},
        {"a.md": rev_a, "b.md": rev_b},
    )
    assert changes == {}

    # Mutating b.md's sidecar mid-validation must still be caught.
    (tmp_path / contracts["b.md"]["sidecar"]).write_text("{}", encoding="utf-8")
    changes_after = validation_snapshot_changes(
        tmp_path,
        "polish",
        source_dir,
        target_dir,
        {"a.md": sha256_text(src_a), "b.md": sha256_text(src_b)},
        {"a.md": sha256_text(src_a), "b.md": sha256_text(src_b)},
        {"a.md": rev_a, "b.md": rev_b},
    )
    assert "b.md" in changes_after
    assert "a.md" not in changes_after


def test_state_lock_windows_retry_and_timeout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Bug 3: Windows state_lock retries EACCES/EDEADLK with backoff and raises TimeoutError."""
    if os.name != "nt":
        pytest.skip("Windows-only msvcrt locking behavior")
    import msvcrt

    calls = {"count": 0}
    real_locking = msvcrt.locking

    def flaky_locking(fd, mode, nbytes):
        if mode == msvcrt.LK_NBLCK and calls["count"] < 2:
            calls["count"] += 1
            raise OSError(errno.EDEADLK, "Resource deadlock avoided")
        return real_locking(fd, mode, nbytes)

    monkeypatch.setattr(msvcrt, "locking", flaky_locking)
    state_file = tmp_path / "polish_validation.json"
    with state_lock(state_file, timeout=2.0):
        assert calls["count"] == 2

    def always_locked(fd, mode, nbytes):
        if mode == msvcrt.LK_NBLCK:
            raise OSError(errno.EACCES, "Permission denied")
        return real_locking(fd, mode, nbytes)

    monkeypatch.setattr(msvcrt, "locking", always_locked)
    with pytest.raises(TimeoutError):
        with state_lock(state_file, timeout=0.05):
            pass


def test_page_furniture_exempts_tables_figures_and_alignment_leaders():
    """Bug 4: Table/Figure captions and leader-dash table rows must not trigger warnings."""
    for caption in (
        "Table 1",
        "Table 6",
        "Figure 2",
        "Fig. 3",
        "Chart 4",
        "Diagram 2.1",
        "Appendix A",
        "Tabelle 5",
        "II Essence ----- Force and Understanding",
    ):
        assert candidate_for_line(caption) is None, f"Unexpected furniture match for {caption!r}"

    # Genuine running headers/page labels remain detected.
    assert candidate_for_line("Preface XII")["kind"] == "running_title_plus_page_label"
    assert candidate_for_line("Chapter One 12")["kind"] == "running_title_plus_page_label"


def test_polish_worker_groups_isolate_extreme_units_and_respect_budget(tmp_path: Path):
    """Arch 5: polish isolates extreme/large units and caps ordinary workers by polish_max_files."""
    source_dir = tmp_path / "footnote_normalized"
    target_dir = tmp_path / "polished_markdown"
    source_dir.mkdir()
    target_dir.mkdir()

    # 1 extreme file (~30k tokens) + 10 ordinary files (~100 tokens each).
    (source_dir / "chapter_8.part14.md").write_text("word " * 30_000, encoding="utf-8")
    for idx in range(1, 11):
        (source_dir / f"chapter_8.part{idx:02d}.md").write_text(
            f"Ordinary paragraph {idx} " * 40, encoding="utf-8"
        )

    prepared = prepare_markdown_subagent(
        tmp_path,
        "polish",
        source_dir,
        target_dir,
        "English",
        "English",
        config={
            "subagent": {
                "batching": {
                    "polish_max_files": 4,
                    "max_source_tokens": 12_000,
                    "max_concurrency": 4,
                    "large_file_token_threshold": 12_000,
                    "extreme_file_token_threshold": 24_000,
                }
            }
        },
    )
    handoffs = write_worker_handoffs(tmp_path, prepared["manifest"], prepared["prompt"])
    extreme_workers = [h for h in handoffs if "chapter_8.part14.md" in h["files"]]
    assert len(extreme_workers) == 1
    assert extreme_workers[0]["files"] == ["chapter_8.part14.md"]
    assert extreme_workers[0]["dispatch_wave_reason"] == "extreme_unit"

    ordinary_workers = [h for h in handoffs if "chapter_8.part14.md" not in h["files"]]
    assert all(len(w["files"]) <= 4 for w in ordinary_workers)
    assert sum(len(w["files"]) for w in ordinary_workers) == 10


def test_footnote_unit_contexts_use_explicit_part_page_ranges_when_present():
    """Arch 6: explicit part_page_ranges prevent chapter-wide cascade invalidation."""
    page_index = {
        1: {"primary": {"path": "pages/page_001.ocr.json", "sha256": "a" * 64}},
        2: {"primary": {"path": "pages/page_002.ocr.json", "sha256": "b" * 64}},
        3: {"primary": {"path": "pages/page_003.ocr.json", "sha256": "c" * 64}},
    }
    report = {
        "source_kind": "ocr",
        "ocr_evidence_mode": "primary_only",
        "footnote_mode": "footnotes",
        "pages": [
            {"page": 1, "candidates": []},
            {"page": 2, "candidates": []},
            {"page": 3, "candidates": []},
        ],
        "cross_page_windows": [],
        "continuation_reviews": [],
        "continuation_chains": [],
        "decisions": [{"page": 3, "block": 1, "role": "footnote_start"}],
    }
    units = [
        {
            "file": "chapter_8.part1.md",
            "part_files": ["chapter_8.part1.md", "chapter_8.part2.md"],
            "page_range": [1, 3],
            "part_page_ranges": {
                "chapter_8.part1.md": [1, 2],
                "chapter_8.part2.md": [3, 3],
            },
        }
    ]
    projected_a = _project_unit_contexts(report, {"candidates": {}}, units, page_index, "packet.json")
    assert projected_a["chapter_8.part1.md"]["page_range"] == [1, 2]
    assert projected_a["chapter_8.part1.md"]["decisions"] == []
    assert projected_a["chapter_8.part2.md"]["page_range"] == [3, 3]
    assert len(projected_a["chapter_8.part2.md"]["decisions"]) == 1

    # Changing a decision on page 3 only alters part2, leaving part1 byte-identical.
    report_b = dict(report, decisions=[{"page": 3, "block": 1, "role": "body"}])
    projected_b = _project_unit_contexts(report_b, {"candidates": {}}, units, page_index, "packet.json")
    assert projected_b["chapter_8.part1.md"] == projected_a["chapter_8.part1.md"]
    assert projected_b["chapter_8.part2.md"] != projected_a["chapter_8.part2.md"]


def test_polish_semantic_issue_maps_whitespace_and_adjacent_blocks_and_closes_upstream(tmp_path: Path):
    """Arch 7: tolerant unique mapping + upstream audit closure breaks polish->footnote loops."""
    pages_dir = tmp_path / "pages"
    ocr_dir = tmp_path / "ocr_markdown"
    norm_dir = tmp_path / "footnote_normalized"
    polish_dir = tmp_path / "polished_markdown"
    for d in (pages_dir, ocr_dir, norm_dir, polish_dir):
        d.mkdir(parents=True, exist_ok=True)

    sidecar_path = pages_dir / "page_001.ocr.json"
    sidecar_data = {
        "blocks": [
            {"text": "First block ends with\nsoft wrap here."},
            {"text": "Second block continues the leaked note."},
        ]
    }
    sidecar_path.write_text(json.dumps(sidecar_data), encoding="utf-8")
    sidecar_sha = hashlib.sha256(sidecar_path.read_bytes()).hexdigest()

    raw_text = "First block ends with soft wrap here.\n\nSecond block continues the leaked note.\n"
    (ocr_dir / "ch12.md").write_bytes(raw_text.encode("utf-8"))
    (norm_dir / "ch12.md").write_bytes(raw_text.encode("utf-8"))
    (polish_dir / "ch12.md").write_bytes(raw_text.encode("utf-8"))

    prepared = prepare_markdown_subagent(
        tmp_path,
        "polish",
        norm_dir,
        polish_dir,
        "English",
        "English",
        footnote_semantics_enabled=True,
    )
    manifest = json.loads(prepared["manifest"].read_text(encoding="utf-8"))
    contract = manifest["footnote_semantics"]["files"]["ch12.md"]

    # Excerpt spans the two adjacent blocks with collapsed whitespace.
    excerpt = "ends with soft wrap here.\n\nSecond block continues"
    issue = {
        "issue_id": "issue-1",
        "code": "note_to_body_leakage",
        "file": "ch12.md",
        "unit_id": "ch12",
        "source_excerpt": excerpt,
        "target_excerpt": excerpt,
        "reason": "Leaked continuation across adjacent OCR blocks.",
        "source_sha256": sha256_text(raw_text),
        "target_sha256": sha256_text(raw_text),
        "context_sha256": contract["context_sha256"],
    }
    sidecar_review_path = tmp_path / contract["sidecar"]
    sidecar_review_path.parent.mkdir(parents=True, exist_ok=True)
    sidecar_review_path.write_text(
        json.dumps(
            {
                "schema_version": SCHEMA_VERSION,
                "file": "ch12.md",
                "source_sha256": sha256_text(raw_text),
                "target_sha256": sha256_text(raw_text),
                "context_sha256": contract["context_sha256"],
                "status": "issues",
                "reviewed_all": True,
                "coverage": {
                    "body_reviewed": {"status": "reviewed"},
                    "definitions_reviewed": {"status": "reviewed"},
                    "cross_boundary_reviewed": {
                        "status": "not_applicable",
                        "reason": "single unit",
                    },
                },
                "reviewed_note_ids": [],
                "issues": [issue],
            },
            indent=2,
        ),
        encoding="utf-8",
    )

    # 1. Mapping should succeed across the adjacent pair with 0 mapping diagnostics.
    tasks, diagnostics = polish_semantic_issue_tasks(
        tmp_path,
        records=[{"name": "ch12.md", "start_page": 1, "end_page": 1, "chapter_id": "ch12"}],
        source_texts={"ch12.md": raw_text},
        sidecar_index={1: {"primary": {"path": "pages/page_001.ocr.json", "sha256": sidecar_sha}}},
        page_report={},
    )
    assert diagnostics == []
    assert len(tasks) == 1
    expected_tid = polish_issue_task_id("ch12.md", issue)
    assert tasks[0]["task_id"] == expected_tid
    assert len(tasks[0]["candidate_refs"]) == 2

    # 2. Before upstream audit closure, polish review remains invalid (must return to footnote).
    before = validate_prepared_file_review(
        tmp_path, "ch12.md", source_text=raw_text, target_text=raw_text, required=True
    )
    assert before["valid"] is False

    # 3. After valid footnote_normalization + applied audit review closes the exact task_id,
    # polish semantic review converges to valid=True instead of looping forever.
    (tmp_path / "footnote_normalization.json").write_text(
        json.dumps({"valid": True, "status": "applied"}), encoding="utf-8"
    )
    (tmp_path / "footnote_result_audit.json").write_text(
        json.dumps(
            {
                "applied_result_audit_reviews": [
                    {"task_id": expected_tid, "outcome": "legitimate_non_footnote"}
                ]
            }
        ),
        encoding="utf-8",
    )
    after = validate_prepared_file_review(
        tmp_path, "ch12.md", source_text=raw_text, target_text=raw_text, required=True
    )
    assert after["valid"] is True
    assert after["resolved_by_upstream_audit"] is True
    assert after["resolved_issue_task_ids"] == [expected_tid]
