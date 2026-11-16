"""Polish semantic findings re-enter the image-backed footnote apply path."""
from __future__ import annotations

import json
import runpy
from pathlib import Path

from pdf2epub.polish_footnote_semantics import build_semantic_review_contract, sha256_text
from pdf2epub.refine.footnote_apply import apply_footnote_normalization
from pdf2epub.refine.footnote_handoff import prepare_footnote_subagent
from pdf2epub.refine.footnote_validation import validate_footnote_decisions


def test_polish_issue_recovery_uses_normalized_source_but_maps_to_raw_primary_block(tmp_path, monkeypatch):
    helper = runpy.run_path(str(Path(__file__).with_name("footnote_review_gates_test.py")))
    helper["_prepare_continuation_book"](tmp_path, monkeypatch)
    assert (tmp_path / "page_001.png").is_file()
    import pdf2epub.refine.footnote_handoff as handoff_module
    monkeypatch.setattr(handoff_module, "ensure_visual_review_images", lambda *_: {
        "available": True, "files": {"1": "page_001.png"},
    })

    raw = "In text <sup>36</sup>.\n\nBody sentence. 36 Note body.\n"
    raw_path = tmp_path / "ocr_markdown" / "chapter_1.md"
    raw_path.write_text(raw, encoding="utf-8")
    sidecar_path = tmp_path / "pages" / "page_001.ocr.json"
    sidecar = json.loads(sidecar_path.read_text(encoding="utf-8"))
    sidecar["blocks"][0].update({
        "label": "Footnote",
        "html": "Body sentence. 36 Note body.",
    })
    sidecar_path.write_text(json.dumps(sidecar), encoding="utf-8")

    normalized = "In text [^36].\n\n[^36]: 36 Note body.\n"
    normalized_dir = tmp_path / "footnote_normalized"
    normalized_dir.mkdir()
    (normalized_dir / "chapter_1.md").write_text(normalized, encoding="utf-8")
    target = "In text [^36].\n\n[^36]: 36 Note body.\n"
    target_dir = tmp_path / "polished_markdown" / "validated"
    target_dir.mkdir(parents=True)
    (target_dir / "chapter_1.md").write_text(target, encoding="utf-8")

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
        "target_sha256": sha256_text(target),
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
            "issue_id": "issue-1",
            "code": "body_note_mixed_block",
            "source_excerpt": "36 Note body.",
            "target_excerpt": "[^36]: 36 Note body.",
            "reason": "The page image shows only the numbered tail belongs to the note.",
            "unit_id": "chapter_1",
            "file": "chapter_1.md",
            "source_sha256": sha256_text(normalized),
            "target_sha256": sha256_text(target),
            "context_sha256": contract["context_sha256"],
        }],
    }), encoding="utf-8")

    paths = prepare_footnote_subagent(tmp_path, book_title="Synthetic Review Book")
    report = json.loads(paths["report"].read_text(encoding="utf-8"))
    task = next(item for item in report["result_audit"]["findings"] if item["code"] == "polish_semantic_issue")
    assert task["source_address"]["source"] == "primary"
    assert task["pages"] == [1]
    assert task["polish_issue"]["polish_source_sha256"] == sha256_text(normalized)
    assert task["evidence_sha256"] != task["polish_issue"]["raw_source_excerpt_sha256"]
    assert task["visual_files"], task

    decision = {
        "page": 1,
        "source": "primary",
        "block": 0,
        "role": "body",
        "override": True,
        "segments": [
            {"role": "body", "text": "Body sentence."},
            {"role": "footnote_start", "key": "36", "text": "36 Note body."},
        ],
    }
    review = {
        "task_id": task["task_id"],
        "evidence_sha256": task["evidence_sha256"],
        "outcome": "confirmed_footnote",
        "reason": "The page image separates body text from the note range.",
        "evidence_pages": [1],
        "candidate_ref": {"page": 1, "source": "primary", "block": 0},
    }
    (tmp_path / "footnote_decisions.json").write_text(json.dumps({
        "schema_version": report["schema_version"],
        "decisions": [decision],
        "result_audit_reviews": [review],
    }), encoding="utf-8")
    validation = validate_footnote_decisions(tmp_path)
    assert validation["valid"] is True, validation["errors"]

    applied = apply_footnote_normalization(tmp_path)
    assert applied["valid"] is True, applied.get("errors")
    normalized_result = (tmp_path / "footnote_normalized" / "chapter_1.md").read_text(encoding="utf-8")
    assert "Body sentence." in normalized_result
    assert "[^36]: Note body." in normalized_result

    # Applying the source correction invalidates the old polish contract. A
    # subsequent prepare must remain usable while explicitly reporting that
    # the old semantic issue is stale and needs a refreshed polish review.
    stale_paths = prepare_footnote_subagent(tmp_path, book_title="Synthetic Review Book")
    stale_report = json.loads(stale_paths["report"].read_text(encoding="utf-8"))
    assert stale_report["result_audit"]["polish_semantic_stale_files"] == ["chapter_1.md"]
    assert not any(item["code"] == "polish_semantic_issue" for item in stale_report["result_audit"]["findings"])

    refreshed_source = (normalized_dir / "chapter_1.md").read_text(encoding="utf-8")
    refreshed_target = refreshed_source
    (target_dir / "chapter_1.md").write_text(refreshed_target, encoding="utf-8")
    refreshed_contract = build_semantic_review_contract("chapter_1.md", refreshed_source)
    manifest["footnote_semantics"]["files"]["chapter_1.md"] = refreshed_contract
    manifest["source_sha256"] = {"chapter_1.md": sha256_text(refreshed_source)}
    (tmp_path / "polish_subagent_manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    refreshed_sidecar = {
        "schema_version": 1,
        "file": "chapter_1.md",
        "source_sha256": sha256_text(refreshed_source),
        "target_sha256": sha256_text(refreshed_target),
        "context_sha256": refreshed_contract["context_sha256"],
        "status": "clear",
        "reviewed_all": True,
        "reviewed_note_ids": refreshed_contract["expected_note_ids"],
        "coverage": {
            "body_reviewed": {"status": "reviewed"},
            "definitions_reviewed": {"status": "reviewed"},
            "cross_boundary_reviewed": {"status": "reviewed"},
        },
        "issues": [],
    }
    (tmp_path / refreshed_contract["sidecar"]).write_text(json.dumps(refreshed_sidecar), encoding="utf-8")
    clear_paths = prepare_footnote_subagent(tmp_path, book_title="Synthetic Review Book")
    clear_report = json.loads(clear_paths["report"].read_text(encoding="utf-8"))
    assert clear_report["result_audit"].get("polish_semantic_stale_files") == []
    assert not any(item["code"] == "polish_semantic_issue" for item in clear_report["result_audit"]["findings"])
