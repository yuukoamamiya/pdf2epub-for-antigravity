import hashlib
import json
from pathlib import Path

import pytest

from pdf2epub.refine.footnote_prepare import (
    prepare_footnote_candidates,
    prepare_footnote_subagent,
    validate_footnote_decisions,
)
from pdf2epub.refine.footnote_apply import apply_footnote_normalization


def _write_sidecar(
    output_dir: Path,
    page: int,
    blocks: list[dict],
    *,
    page_box: list[int] | None = None,
) -> None:
    pages_dir = output_dir / "pages"
    pages_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": 1,
        "page_number": page,
        "backend": "test",
        "page_box": page_box or [0, 0, 1000, 1000],
        "blocks": blocks,
    }
    (pages_dir / f"page_{page:03d}.ocr.json").write_text(
        json.dumps(payload, ensure_ascii=False),
        encoding="utf-8",
    )


def _block(text: str, order: int, *, label: str = "Text", y0: int = 100) -> dict:
    return {
        "order": order,
        "label": label,
        "bbox": [80, y0, 920, min(1000, y0 + 50)],
        "html": text,
    }


def test_candidate_report_preserves_body_then_continuation_then_new_note_order(
    tmp_path: Path,
):
    # The second page intentionally has body text before the continuation.  A
    # page adjacency algorithm must not move that continuation to page start.
    _write_sidecar(
        tmp_path,
        1,
        [
            _block("A page body", 0, y0=120),
            _block("36 Footnote starts on A", 1, label="Footnote", y0=820),
        ],
    )
    _write_sidecar(
        tmp_path,
        2,
        [
            _block("B page body", 0, y0=120),
            _block("Footnote 36 continues", 1, label="Footnote", y0=820),
            _block("37 New footnote", 2, label="Footnote", y0=900),
        ],
    )

    report = prepare_footnote_candidates(tmp_path)

    page_two = report["pages"][1]
    assert [item["block"] for item in page_two["candidates"]] == [1, 2]
    assert report["cross_page_windows"][0]["pages"] == [1, 2]
    assert [item["block"] for item in report["cross_page_windows"][0]["right_candidates"]] == [1, 2]
    assert report["high_confidence_candidate_count"] == 2
    assert report["review_candidate_count"] == 1
    # Page 1 is included because the reviewer needs the preceding footnote
    # block to decide whether page 2 block 1 is its continuation.
    assert report["review_pages"] == [1, 2]


def test_unlabelled_numbered_bottom_block_is_review_only(tmp_path: Path):
    _write_sidecar(
        tmp_path,
        1,
        [_block("36 Possibly a footnote", 0, y0=820)],
    )

    report = prepare_footnote_candidates(tmp_path)

    candidate = report["pages"][0]["candidates"][0]
    assert candidate["key"] == "36"
    assert candidate["confidence"] == "review"
    assert candidate["disposition"] == "review_required"
    assert report["review_pages"] == [1]


def test_consensus_visual_review_disables_local_footnote_auto_accept(tmp_path: Path):
    _write_sidecar(
        tmp_path,
        1,
        [_block("36 Possibly a footnote", 0, label="Footnote", y0=820)],
    )
    (tmp_path / "ocr_consensus.json").write_text(
        json.dumps(
            {
                "records": {
                    "page_001.md": {
                        "action": "visual_review",
                        "layout_comparison": {
                            "status": "review_required",
                            "reasons": ["footnote label presence differs"],
                        },
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    secondary = tmp_path / "ocr_secondary"
    secondary.mkdir()
    (secondary / "page_001.ocr.json").write_text(
        (tmp_path / "pages" / "page_001.ocr.json").read_text(encoding="utf-8"),
        encoding="utf-8",
    )

    paths = prepare_footnote_subagent(tmp_path, book_title="Test Book")
    report = json.loads((tmp_path / "footnote_candidates.json").read_text(encoding="utf-8"))
    candidate = report["pages"][0]["candidates"][0]
    prompt = paths["prompt"].read_text(encoding="utf-8")

    assert candidate["confidence"] == "review"
    assert candidate["review_reason"] == "ocr_consensus_visual_review"
    assert report["consensus_visual_review_pages"] == [1]
    assert "ocr_secondary/page_001.ocr.json" in prompt


def test_single_ocr_config_ignores_stale_consensus_artifact(tmp_path: Path):
    _write_sidecar(
        tmp_path,
        1,
        [_block("36 Definitely a footnote", 0, label="Footnote", y0=820)],
    )
    (tmp_path / "ocr_consensus.json").write_text(
        json.dumps({"records": {"page_001.md": {"action": "visual_review"}}}),
        encoding="utf-8",
    )

    report = prepare_footnote_candidates(
        tmp_path,
        config={"ocr": {"secondary": {"enabled": False, "backend": "paddle"}}},
    )

    assert report["ocr_evidence_mode"] == "single_ocr"
    assert report["consensus_visual_review_pages"] == []
    assert report["pages"][0]["candidates"][0]["confidence"] == "high"


def test_two_ocr_config_requires_current_consensus(tmp_path: Path):
    _write_sidecar(
        tmp_path,
        1,
        [_block("36 Footnote", 0, label="Footnote", y0=820)],
    )
    config = {"ocr": {"secondary": {"enabled": True, "backend": "paddle"}}}

    with pytest.raises(ValueError, match="current ocr_consensus"):
        prepare_footnote_candidates(tmp_path, config=config)


def test_footnote_validation_rejects_switching_ocr_mode(tmp_path: Path):
    _write_sidecar(
        tmp_path,
        1,
        [_block("36 Footnote", 0, label="Footnote", y0=820)],
    )
    prepare_footnote_candidates(
        tmp_path,
        config={"ocr": {"secondary": {"enabled": False}}},
    )

    result = validate_footnote_decisions(
        tmp_path,
        config={"ocr": {"secondary": {"enabled": True, "backend": "paddle"}}},
    )

    assert result["valid"] is False
    assert "requires two_ocr" in result["errors"][0]


def test_two_ocr_candidate_difference_is_sent_to_review(tmp_path: Path, monkeypatch):
    _write_sidecar(
        tmp_path,
        1,
        [_block("36 Footnote", 0, label="Footnote", y0=820)],
    )
    secondary = tmp_path / "ocr_secondary"
    secondary.mkdir()
    (secondary / "page_001.ocr.json").write_text(
        json.dumps(
            {
                "page_number": 1,
                "page_box": [0, 0, 1000, 1000],
                "blocks": [_block("Bottom body", 0, label="Text", y0=820)],
            }
        ),
        encoding="utf-8",
    )
    (secondary / "page_001.md").write_text("Bottom body", encoding="utf-8")
    monkeypatch.setattr(
        "pdf2epub.refine.footnote_prepare.consensus_is_current",
        lambda output_dir, config: True,
    )
    config = {"ocr": {"secondary": {"enabled": True, "backend": "paddle"}}}

    report = prepare_footnote_candidates(tmp_path, config=config)

    candidate = report["pages"][0]["candidates"][0]
    assert report["ocr_evidence_mode"] == "two_ocr"
    assert report["pages"][0]["consensus_status"] == "disagree"
    assert candidate["confidence"] == "review"
    assert candidate["review_reason"] == "ocr_candidate_presence_differs"


def test_bibliography_block_is_not_a_footnote_candidate(tmp_path: Path):
    _write_sidecar(
        tmp_path,
        1,
        [_block("36 Source entry", 0, label="Bibliography", y0=820)],
    )

    report = prepare_footnote_candidates(tmp_path)

    assert report["pages"] == []
    assert report["high_confidence_candidate_count"] == 0
    assert report["review_candidate_count"] == 0


def test_prepare_subagent_writes_compact_contract(tmp_path: Path):
    _write_sidecar(
        tmp_path,
        1,
        [_block("36 Possibly a footnote", 0, y0=820)],
    )
    (tmp_path / "toc_tree.json").write_text("{}", encoding="utf-8")
    (tmp_path / "ocr_markdown").mkdir()

    paths = prepare_footnote_subagent(tmp_path, book_title="Test Book")
    manifest = json.loads(paths["manifest"].read_text(encoding="utf-8"))
    prompt = paths["prompt"].read_text(encoding="utf-8")

    assert manifest["task"] == "footnote-prepare"
    assert manifest["review_pages"] == [1]
    assert manifest["candidate_report"] == "footnote_candidates.json"
    assert "Do not reread or rewrite the whole book" in prompt
    assert "footnote_decisions.json" in prompt


def test_validated_decisions_are_projected_to_sparse_unit_context(tmp_path: Path):
    _write_sidecar(
        tmp_path,
        1,
        [_block("36 Possibly a footnote", 0, y0=820)],
    )
    (tmp_path / "toc_tree.json").write_text("{}", encoding="utf-8")
    progress_dir = tmp_path / "ocr_markdown"
    progress_dir.mkdir()
    (progress_dir / "tree_progress.json").write_text(
        json.dumps(
            {
                "units": [
                    {
                        "file": "chapter_1.md",
                        "page_range": [1, 1],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    prepare_footnote_subagent(tmp_path, book_title="Test Book")
    (tmp_path / "footnote_decisions.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "decisions": [
                    {
                        "page": 1,
                        "block": 0,
                        "role": "footnote_start",
                        "key": "36",
                        "confidence": "high",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    result = validate_footnote_decisions(tmp_path)

    assert result["valid"] is True
    context = json.loads(
        (tmp_path / "footnote_contexts" / "chapter_1.md.json").read_text(
            encoding="utf-8"
        )
    )
    assert context["decisions"][0]["key"] == "36"


def test_unresolved_decision_requires_human_review(tmp_path: Path):
    _write_sidecar(
        tmp_path,
        1,
        [_block("36 Possibly a footnote", 0, y0=820)],
    )
    prepare_footnote_subagent(tmp_path, book_title="Test Book")
    (tmp_path / "footnote_decisions.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "decisions": [
                    {
                        "page": 1,
                        "block": 0,
                        "role": "review_required",
                        "reason": "The scan is ambiguous",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    result = validate_footnote_decisions(tmp_path)

    assert result["valid"] is False
    assert result["status"] == "human_review_required"


def test_apply_moves_cross_page_notes_but_keeps_body_order(tmp_path: Path):
    _write_sidecar(
        tmp_path,
        1,
        [
            _block("A body <sup>36</sup>", 0, y0=120),
            _block("36 Footnote starts on A", 1, label="Footnote", y0=820),
        ],
    )
    _write_sidecar(
        tmp_path,
        2,
        [
            _block("B body <sup>37</sup>", 0, y0=120),
            _block("continuation text", 1, label="Footnote", y0=820),
            _block("37 New footnote", 2, label="Footnote", y0=900),
        ],
    )
    source = tmp_path / "ocr_markdown"
    source.mkdir()
    (source / "chapter_1.md").write_text(
        "A body <sup>36</sup>\n\n"
        "36 Footnote starts on A\n\n"
        "B body <sup>37</sup>\n\n"
        "continuation text\n\n"
        "37 New footnote\n",
        encoding="utf-8",
    )
    (source / "tree_progress.json").write_text(
        json.dumps(
            {
                "units": [
                    {
                        "unit_id": "chapter_1",
                        "index_path": [1],
                        "file": "chapter_1.md",
                        "page_range": [1, 2],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    (tmp_path / "toc_tree.json").write_text("{}", encoding="utf-8")

    prepare_footnote_subagent(tmp_path, book_title="Test Book")
    (tmp_path / "footnote_decisions.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "decisions": [
                    {
                        "page": 2,
                        "block": 1,
                        "role": "footnote_continuation",
                        "key": "36",
                        "confidence": "high",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    assert validate_footnote_decisions(tmp_path)["valid"] is True

    result = apply_footnote_normalization(tmp_path)

    assert result["valid"] is True
    normalized = (tmp_path / "footnote_normalized" / "chapter_1.md").read_text(
        encoding="utf-8"
    )
    assert "A body [^36]" in normalized
    assert "B body [^37]" in normalized
    assert normalized.index("B body") < normalized.index("[^36]:")
    assert "[^36]: Footnote starts on A continuation text" in normalized
    assert "[^37]: New footnote" in normalized
    assert "36 Footnote starts on A" not in normalized


def test_citation_decision_is_preserved_and_not_moved(tmp_path: Path):
    _write_sidecar(
        tmp_path,
        1,
        [_block("36 See Smith 2020", 0, y0=820)],
    )
    source = tmp_path / "ocr_markdown"
    source.mkdir()
    original = "A citation follows: 36 See Smith 2020\n"
    (source / "chapter_1.md").write_text(original, encoding="utf-8")
    (source / "tree_progress.json").write_text(
        json.dumps(
            {
                "units": [
                    {
                        "unit_id": "chapter_1",
                        "index_path": [1],
                        "file": "chapter_1.md",
                        "page_range": [1, 1],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    prepare_footnote_subagent(tmp_path, book_title="Test Book")
    (tmp_path / "footnote_decisions.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "decisions": [
                    {
                        "page": 1,
                        "block": 0,
                        "role": "citation",
                        "key": "36",
                        "confidence": "high",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    assert validate_footnote_decisions(tmp_path)["valid"] is True

    result = apply_footnote_normalization(tmp_path)

    assert result["valid"] is True
    assert result["preserved_roles"] == {"citation": 1}
    assert (tmp_path / "footnote_normalized" / "chapter_1.md").read_text(
        encoding="utf-8"
    ) == original


def test_restarted_footnote_numbers_use_separate_toc_unit_scopes(tmp_path: Path):
    _write_sidecar(
        tmp_path,
        1,
        [
            _block("Section A <sup>1</sup>", 0, y0=120),
            _block("1 Note for A", 1, label="Footnote", y0=820),
        ],
    )
    _write_sidecar(
        tmp_path,
        2,
        [
            _block("Section B <sup>1</sup>", 0, y0=120),
            _block("1 Note for B", 1, label="Footnote", y0=820),
        ],
    )
    source = tmp_path / "ocr_markdown"
    source.mkdir()
    (source / "chapter_1.1.md").write_text(
        "Section A <sup>1</sup>\n\n1 Note for A\n", encoding="utf-8"
    )
    (source / "chapter_1.2.md").write_text(
        "Section B <sup>1</sup>\n\n1 Note for B\n", encoding="utf-8"
    )
    (source / "tree_progress.json").write_text(
        json.dumps(
            {
                "units": [
                    {
                        "unit_id": "chapter_1.1",
                        "index_path": [1, 1],
                        "file": "chapter_1.1.md",
                        "page_range": [1, 1],
                    },
                    {
                        "unit_id": "chapter_1.2",
                        "index_path": [1, 2],
                        "file": "chapter_1.2.md",
                        "page_range": [2, 2],
                    },
                ]
            }
        ),
        encoding="utf-8",
    )

    prepare_footnote_subagent(tmp_path, book_title="Test Book")
    assert validate_footnote_decisions(tmp_path)["valid"] is True

    result = apply_footnote_normalization(tmp_path)

    assert result["valid"] is True
    normalized_a = (tmp_path / "footnote_normalized" / "chapter_1.1.md").read_text(
        encoding="utf-8"
    )
    normalized_b = (tmp_path / "footnote_normalized" / "chapter_1.2.md").read_text(
        encoding="utf-8"
    )
    assert "Section A [^1]" in normalized_a
    assert "[^1]: Note for A" in normalized_a
    assert "Section B [^1]" in normalized_b
    assert "[^1]: Note for B" in normalized_b
    assert "[^1-2]" not in normalized_b


def test_apply_uses_validated_correction_when_sidecar_text_changed(tmp_path: Path):
    _write_sidecar(
        tmp_path,
        1,
        [
            _block("A body <sup>36</sup>", 0, y0=120),
            _block("36 Footnote starts on A", 1, label="Footnote", y0=820),
        ],
    )
    raw_page = "A body <sup>36</sup>\n\n36 Footnote starts on A\n"
    corrected_page = "A body <sup>36</sup>\n\n36 Footnote starts on Å\n"
    (tmp_path / "pages" / "page_001.md").write_text(raw_page, encoding="utf-8")
    corrected_dir = tmp_path / "ocr_corrected_pages" / "validated"
    corrected_dir.mkdir(parents=True)
    (corrected_dir / "page_001.md").write_text(corrected_page, encoding="utf-8")

    source = tmp_path / "ocr_markdown"
    source.mkdir()
    (source / "chapter_1.md").write_text(corrected_page, encoding="utf-8")
    (source / "tree_progress.json").write_text(
        json.dumps(
            {
                "units": [
                    {
                        "unit_id": "chapter_1",
                        "index_path": [1],
                        "file": "chapter_1.md",
                        "page_range": [1, 1],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    digest = lambda path: hashlib.sha256(path.read_bytes()).hexdigest()
    (tmp_path / "ocr-correct_validation.json").write_text(
        json.dumps(
            {
                "all_passed": True,
                "source_sha256": {"page_001.md": digest(tmp_path / "pages" / "page_001.md")},
                "target_sha256": {
                    "page_001.md": digest(corrected_dir / "page_001.md")
                },
            }
        ),
        encoding="utf-8",
    )

    prepare_footnote_subagent(tmp_path, book_title="Test Book")
    assert validate_footnote_decisions(tmp_path)["valid"] is True

    result = apply_footnote_normalization(tmp_path)

    assert result["valid"] is True
    normalized = (tmp_path / "footnote_normalized" / "chapter_1.md").read_text(
        encoding="utf-8"
    )
    assert "A body [^36]" in normalized
    assert "[^36]: Footnote starts on Å" in normalized
