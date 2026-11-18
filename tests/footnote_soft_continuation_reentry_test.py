from __future__ import annotations

import json
import runpy
from pathlib import Path

from pdf2epub.polish_footnote_semantics import build_semantic_review_contract, sha256_text
from pdf2epub.refine.footnote_handoff import prepare_footnote_subagent
from pdf2epub.refine.footnote_continuation import _suppress_soft_continuation_candidates


def test_exact_polish_issue_reenters_a_soft_suppressed_block_as_review_candidate(tmp_path, monkeypatch):
    helper = runpy.run_path(str(Path(__file__).with_name("footnote_review_gates_test.py")))
    helper["_prepare_continuation_book"](tmp_path, monkeypatch)
    import pdf2epub.refine.footnote_handoff as handoff_module

    raw = "In text <sup>36</sup>.\n\nBody sentence. 36 Note body.\n"
    source = tmp_path / "ocr_markdown" / "chapter_1.md"
    source.write_text(raw, encoding="utf-8")
    sidecar_path = tmp_path / "pages" / "page_001.ocr.json"
    sidecar = json.loads(sidecar_path.read_text(encoding="utf-8"))
    sidecar["blocks"][0].update({"label": "Text", "html": "Body sentence. 36 Note body."})
    sidecar_path.write_text(json.dumps(sidecar), encoding="utf-8")
    normalized = "In text [^36].\n\nBody sentence. 36 Note body.\n"
    normalized_dir = tmp_path / "footnote_normalized"
    normalized_dir.mkdir()
    (normalized_dir / "chapter_1.md").write_text(normalized, encoding="utf-8")
    target_dir = tmp_path / "polished_markdown" / "validated"
    target_dir.mkdir(parents=True)
    (target_dir / "chapter_1.md").write_text(normalized, encoding="utf-8")
    contract = build_semantic_review_contract("chapter_1.md", normalized)
    manifest = {
        "task": "polish",
        "source_dir": "footnote_normalized",
        "target_dir": "polished_markdown/validated",
        "source_sha256": {"chapter_1.md": sha256_text(normalized)},
        "unit_context_sha256": {},
        "boundary_context_sha256": {},
        "footnote_semantics": {"enabled": True, "files": {"chapter_1.md": contract}},
    }
    (tmp_path / "polish_subagent_manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    (tmp_path / contract["sidecar"]).parent.mkdir(parents=True, exist_ok=True)
    (tmp_path / contract["sidecar"]).write_text(json.dumps({
        "schema_version": 1,
        "file": "chapter_1.md",
        "source_sha256": sha256_text(normalized),
        "target_sha256": sha256_text(normalized),
        "context_sha256": contract["context_sha256"],
        "status": "issues",
        "reviewed_all": True,
        "reviewed_note_ids": ["36"],
        "coverage": {
            "body_reviewed": {"status": "reviewed"},
            "definitions_reviewed": {"status": "reviewed"},
            "cross_boundary_reviewed": {"status": "reviewed"},
        },
        "issues": [{
            "issue_id": "soft-continuation-issue",
            "code": "body_note_mixed_block",
            "source_excerpt": "36 Note body.",
            "target_excerpt": "Body sentence. 36 Note body.",
            "reason": "The exact source excerpt requires visual ownership review.",
            "unit_id": "chapter_1",
            "file": "chapter_1.md",
            "source_sha256": sha256_text(normalized),
            "target_sha256": sha256_text(normalized),
            "context_sha256": contract["context_sha256"],
        }],
    }), encoding="utf-8")

    original_prepare = handoff_module.prepare_footnote_candidates
    source_block = "Body sentence. 36 Note body."

    def prepare_with_soft_suppression(*args, **kwargs):
        report = original_prepare(*args, **kwargs)
        page_row = next((row for row in report["pages"] if row.get("page") == 1), None)
        if page_row is None:
            page_row = {"page": 1, "candidates": [], "audit_candidates": [], "secondary_only_candidates": []}
            report["pages"].append(page_row)
        raw_candidate = {
            "page": 1,
            "source": "primary",
            "block": 0,
            "label": "Text",
            "text": source_block,
            "continuous_region_evidence": True,
            "review_reason": "orphan_continuation",
        }
        _active, suppressed = _suppress_soft_continuation_candidates([raw_candidate])
        assert len(suppressed) == 1
        page_row["suppressed_continuation_candidates"] = suppressed
        return report

    monkeypatch.setattr(handoff_module, "prepare_footnote_candidates", prepare_with_soft_suppression)
    paths = prepare_footnote_subagent(tmp_path, book_title="Soft Continuation Reentry")
    report = json.loads(paths["report"].read_text(encoding="utf-8"))
    page = next(row for row in report["pages"] if row["page"] == 1)

    assert report["result_audit"]["polish_semantic_blockers"] == []
    assert report["result_audit"]["polish_semantic_stale_files"] == []
    assert page["suppressed_continuation_candidates"][0]["suppressed_as_context"] is True
    assert any(
        candidate.get("result_audit_admitted") is True
        and candidate.get("block") == 0
        and candidate.get("source") == "primary"
        for candidate in page["candidates"]
    )
    assert any(
        task.get("polish_issue", {}).get("issue_id") == "soft-continuation-issue"
        and task.get("source_address", {}).get("block") == 0
        for task in report["result_audit"]["findings"]
    )
