import hashlib
import json
from pathlib import Path

import pytest

from pdf2epub.refine.footnote_prepare import (
    prepare_footnote_candidates,
    prepare_footnote_subagent,
    validate_footnote_decisions,
)
from pdf2epub.refine.footnote_apply import (
    _find_exact_signature_spans,
    _locate_block,
    apply_footnote_normalization,
    footnote_normalization_status,
)
from pdf2epub.layout_detection import layout_config_sha256


def test_footnote_signature_does_not_match_number_inside_larger_number():
    source = "15 Auflage der Ausgabe\n\n5 Auflage der Ausgabe"

    spans = _find_exact_signature_spans(source, "5 Auflage der Ausgabe")

    assert len(spans) == 1
    start, end = spans[0]
    assert source[start:end] == "5 Auflage der Ausgabe"


def test_footnote_signature_preserves_ambiguity_for_true_duplicate_blocks():
    source = "5 Auflage der Ausgabe\n\nZwischentext\n\n5 Auflage der Ausgabe"

    with pytest.raises(ValueError, match="matched multiple source spans"):
        _locate_block(
            {"unit.md": source},
            [
                {
                    "name": "unit.md",
                    "start_page": 1,
                    "end_page": 1,
                }
            ],
            1,
            ["5 Auflage der Ausgabe"],
        )


def test_footnote_locator_does_not_fallback_to_another_refinement_unit():
    with pytest.raises(ValueError, match="could not locate OCR block text"):
        _locate_block(
            {
                "chapter_1.md": "Body in the expected chapter",
                "chapter_2.md": "36 Shared footnote text",
            },
            [
                {
                    "name": "chapter_1.md",
                    "start_page": 1,
                    "end_page": 1,
                },
                {
                    "name": "chapter_2.md",
                    "start_page": 2,
                    "end_page": 2,
                },
            ],
            1,
            ["36 Shared footnote text"],
        )


def test_footnote_signature_maps_markup_without_relaxing_boundaries():
    source = "15 Auflage\n5 <sup>Auflage</sup>"

    spans = _find_exact_signature_spans(source, "5 Auflage")

    assert len(spans) == 1
    start, end = spans[0]
    assert source[start:end] == "5 <sup>Auflage</sup>"


def test_footnote_signature_does_not_insert_space_before_punctuation_after_html():
    source = "The title is In the Spirit of Hegel, followed by prose."

    spans = _find_exact_signature_spans(
        source,
        "<i>In the Spirit of Hegel</i>,",
    )

    assert len(spans) == 1
    start, end = spans[0]
    assert source[start:end] == "In the Spirit of Hegel,"


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


def _write_layout_prediction(
    output_dir: Path,
    page: int,
    config: dict,
    *,
    source_sha256: str = "source-sha",
    boxes: list[dict] | None = None,
) -> None:
    layout_dir = output_dir / "layout_detection"
    layout_dir.mkdir(parents=True, exist_ok=True)
    prediction = {
        "schema_version": 1,
        "page_number": page,
        "backend": "pp_doclayout",
        "model_name": "PP-DocLayout-L",
        "source_pdf_sha256": source_sha256,
        "layout_config_sha256": layout_config_sha256(config),
        "coordinate_system": "pixels",
        "page_box": [0, 0, 1000, 1000],
        "boxes": boxes or [],
    }
    (layout_dir / f"page_{page:03d}.json").write_text(
        json.dumps(prediction), encoding="utf-8"
    )
    (output_dir / "layout_detection_manifest.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "status": "complete",
                "complete": True,
                "source_pdf_sha256": source_sha256,
                "layout_config_sha256": layout_config_sha256(config),
            }
        ),
        encoding="utf-8",
    )


def test_pp_doclayout_footnotes_box_discovers_unlabelled_continuation(
    tmp_path: Path,
):
    config = {
        "ocr": {
            "layout": {
                "enabled": True,
                "model_name": "PP-DocLayout-L",
                "device": "gpu:0",
            }
        }
    }
    _write_sidecar(
        tmp_path,
        1,
        [_block("continuation without a repeated number", 0, y0=820)],
    )
    _write_layout_prediction(
        tmp_path,
        1,
        config,
        boxes=[
            {
                "label": "footnotes",
                "score": 0.93,
                "bbox": [20, 780, 980, 960],
            }
        ],
    )

    report = prepare_footnote_candidates(tmp_path, config=config)

    candidate = report["pages"][0]["candidates"][0]
    assert candidate["confidence"] == "review"
    assert candidate["review_reason"] == "pp_doclayout_footnotes_region"
    assert candidate["layout_evidence"]["overlap"] > 0.5
    assert report["layout_detection_enabled"] is True


def test_pp_doclayout_stale_prediction_blocks_footnote_prepare(tmp_path: Path):
    config = {
        "ocr": {
            "layout": {
                "enabled": True,
                "model_name": "PP-DocLayout-L",
                "device": "gpu:0",
            }
        }
    }
    _write_sidecar(
        tmp_path,
        1,
        [_block("36 footnote", 0, label="Footnote", y0=820)],
    )
    _write_layout_prediction(tmp_path, 1, config, source_sha256="old-source")
    prediction_path = tmp_path / "layout_detection" / "page_001.json"
    prediction = json.loads(prediction_path.read_text(encoding="utf-8"))
    prediction["source_pdf_sha256"] = "new-source"
    prediction_path.write_text(json.dumps(prediction), encoding="utf-8")

    with pytest.raises(ValueError, match="missing or stale"):
        prepare_footnote_candidates(tmp_path, config=config)


def test_pp_doclayout_disables_auto_accept_without_region_support(tmp_path: Path):
    config = {
        "ocr": {
            "layout": {
                "enabled": True,
                "model_name": "PP-DocLayout-L",
                "device": "gpu:0",
            }
        }
    }
    _write_sidecar(
        tmp_path,
        1,
        [_block("36 OCR footnote", 0, label="Footnote", y0=820)],
    )
    _write_layout_prediction(tmp_path, 1, config, boxes=[])

    report = prepare_footnote_candidates(tmp_path, config=config)

    candidate = report["pages"][0]["candidates"][0]
    assert candidate["confidence"] == "review"
    assert candidate["review_reason"] == "ocr_candidate_without_layout_support"


def test_pp_doclayout_generic_region_is_preserved_as_review_evidence(tmp_path: Path):
    config = {
        "ocr": {
            "layout": {
                "enabled": True,
                "model_name": "PP-DocLayout-L",
                "device": "gpu:0",
            }
        },
        "footnotes": {"auto_accept": False},
    }
    _write_sidecar(
        tmp_path,
        1,
        [_block("1. Numbered section heading", 0, label="Section-Header", y0=820)],
    )
    _write_layout_prediction(
        tmp_path,
        1,
        config,
        boxes=[
            {
                "label": "paragraph_title",
                "score": 0.91,
                "bbox": [20, 780, 980, 960],
            }
        ],
    )

    report = prepare_footnote_candidates(tmp_path, config=config)

    candidate = report["pages"][0]["candidates"][0]
    assert candidate["layout_evidence"]["label"] == "paragraph_title"
    assert candidate["layout_evidence"]["matched"] is True
    assert candidate["layout_review_reason"] == "pp_doclayout_paragraph_title_region"


def test_footnote_status_reports_candidate_hash_as_root_cause(tmp_path: Path):
    source_dir = tmp_path / "ocr_markdown"
    target_dir = tmp_path / "footnote_normalized"
    source_dir.mkdir()
    target_dir.mkdir()
    source = source_dir / "unit.md"
    target = target_dir / "unit.md"
    source.write_text("body\n", encoding="utf-8")
    target.write_text("body\n", encoding="utf-8")
    candidates = tmp_path / "footnote_candidates.json"
    decisions = tmp_path / "footnote_decision_validation.json"
    candidates.write_text("{}", encoding="utf-8")
    decisions.write_text(
        json.dumps({"valid": True, "ocr_evidence_mode": "single_ocr"}),
        encoding="utf-8",
    )
    normalization = tmp_path / "footnote_normalization.json"
    normalization.write_text(
        json.dumps(
            {
                "valid": True,
                "status": "validated",
                "ocr_evidence_mode": "single_ocr",
                "source_sha256": {
                    "unit.md": hashlib.sha256(source.read_bytes()).hexdigest()
                },
                "target_sha256": {
                    "unit.md": hashlib.sha256(target.read_bytes()).hexdigest()
                },
                "candidate_report_sha256": hashlib.sha256(
                    candidates.read_bytes()
                ).hexdigest(),
                "decision_validation_sha256": hashlib.sha256(
                    decisions.read_bytes()
                ).hexdigest(),
            }
        ),
        encoding="utf-8",
    )
    candidates.write_text('{"changed": true}', encoding="utf-8")

    status = footnote_normalization_status(tmp_path)

    assert status["current"] is False
    assert status["failures"][0]["code"] == "candidate_report_stale"
    assert "footnote-prepare" in status["detail"]


def _write_native_sidecar(
    output_dir: Path,
    page: int,
    blocks: list[dict],
    *,
    body_font_size: float = 10.0,
) -> None:
    pages_dir = output_dir / "pages"
    pages_dir.mkdir(parents=True, exist_ok=True)
    (pages_dir / "ocr_progress.json").write_text(
        json.dumps(
            {
                "schema_version": 2,
                "mode": "native_text",
                "backend": "native_text",
                "total_pages": page,
                "pages_processed": list(range(1, page + 1)),
                "failed_pages": [],
                "empty_pages": [],
                "allowed_empty_pages": [],
                "missing_pages": [],
            }
        ),
        encoding="utf-8",
    )
    (pages_dir / f"page_{page:03d}.ocr.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "page_number": page,
                "backend": "native_text",
                "source_kind": "native_text",
                "coordinate_system": "page_points",
                "page_box": [0, 0, 612, 792],
                "body_font_size": body_font_size,
                "blocks": blocks,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


def _block(text: str, order: int, *, label: str = "Text", y0: int = 100) -> dict:
    return {
        "order": order,
        "label": label,
        "bbox": [80, y0, 920, min(1000, y0 + 50)],
        "html": text,
    }


def _native_block(
    text: str,
    order: int,
    *,
    y0: float,
    y1: float | None = None,
    font_size: float = 10.0,
) -> dict:
    return {
        "order": order,
        "label": "Text",
        "bbox": [72, y0, 540, y1 or y0 + 18],
        "text": text,
        "font_size": font_size,
    }


def test_native_layout_candidates_use_page_coordinates_and_ignore_page_numbers(
    tmp_path: Path,
):
    _write_native_sidecar(
        tmp_path,
        1,
        [
            _native_block("Body text", 0, y0=90),
            _native_block("125", 1, y0=752, y1=764, font_size=9),
            _native_block("36 Native footnote text", 2, y0=680, font_size=8),
        ],
    )

    report = prepare_footnote_candidates(
        tmp_path,
        config={"ocr": {"secondary": {"enabled": True, "backend": "paddle"}}},
    )

    assert report["source_kind"] == "native_text"
    assert report["ocr_evidence_mode"] == "single_ocr"
    assert report["high_confidence_candidate_count"] == 0
    assert report["review_candidate_count"] == 1
    assert report["pages"][0]["candidates"][0]["key"] == "36"
    assert report["pages"][0]["candidates"][0]["review_reason"] == "native_layout_candidate"


def test_native_candidate_with_same_page_superscript_is_locally_accepted(
    tmp_path: Path,
):
    _write_native_sidecar(
        tmp_path,
        1,
        [
            _native_block("Body text", 0, y0=90, font_size=10),
            _native_block("36 Native footnote text", 1, y0=680, font_size=8),
        ],
    )
    (tmp_path / "pages" / "page_001.md").write_text(
        "Body text <sup>36</sup>\n\n36 Native footnote text\n",
        encoding="utf-8",
    )

    report = prepare_footnote_candidates(tmp_path)

    candidate = report["pages"][0]["candidates"][0]
    assert candidate["confidence"] == "high"
    assert candidate["disposition"] == "local_candidate"
    assert candidate["same_page_superscript"] is True
    assert report["high_confidence_candidate_count"] == 1
    assert report["review_candidate_count"] == 0
    assert report["review_required"] is False


def test_native_definition_number_is_not_its_own_superscript_evidence(
    tmp_path: Path,
):
    _write_native_sidecar(
        tmp_path,
        1,
        [_native_block("36 Native footnote text", 0, y0=680, font_size=8)],
    )
    (tmp_path / "pages" / "page_001.md").write_text(
        "<sup>36</sup> Native footnote text\n",
        encoding="utf-8",
    )

    report = prepare_footnote_candidates(tmp_path)

    candidate = report["pages"][0]["candidates"][0]
    assert candidate["same_page_superscript"] is False
    assert candidate["confidence"] == "review"


def test_auto_accept_can_be_disabled(tmp_path: Path):
    _write_native_sidecar(
        tmp_path,
        1,
        [
            _native_block("Body", 0, y0=90, font_size=10),
            _native_block("36 Native footnote text", 1, y0=680, font_size=8),
        ],
    )
    (tmp_path / "pages" / "page_001.md").write_text(
        "Body <sup>36</sup>\n\n36 Native footnote text\n",
        encoding="utf-8",
    )

    report = prepare_footnote_candidates(tmp_path, auto_accept=False)

    candidate = report["pages"][0]["candidates"][0]
    assert candidate["same_page_superscript"] is True
    assert candidate["confidence"] == "review"
    assert report["auto_accept"] is False


def test_footnote_auto_accept_config_applies_to_ocr_pdf(
    tmp_path: Path,
):
    _write_sidecar(
        tmp_path,
        1,
        [_block("36 OCR footnote text", 0, label="Footnote", y0=820)],
    )

    report = prepare_footnote_candidates(
        tmp_path,
        config={"footnotes": {"auto_accept": False}},
    )

    candidate = report["pages"][0]["candidates"][0]
    assert candidate["confidence"] == "review"
    assert candidate["disposition"] == "review_required"
    assert candidate["review_reason"] == "local_auto_accept_disabled"
    assert report["auto_accept"] is False


def test_footnote_auto_accept_config_applies_to_native_pdf(
    tmp_path: Path,
):
    _write_native_sidecar(
        tmp_path,
        1,
        [
            _native_block("Body", 0, y0=90, font_size=10),
            _native_block("36 Native footnote text", 1, y0=680, font_size=8),
        ],
    )
    (tmp_path / "pages" / "page_001.md").write_text(
        "Body <sup>36</sup>\n\n36 Native footnote text\n",
        encoding="utf-8",
    )

    report = prepare_footnote_candidates(
        tmp_path,
        config={"footnotes": {"auto_accept": False}},
    )

    candidate = report["pages"][0]["candidates"][0]
    assert candidate["same_page_superscript"] is True
    assert candidate["confidence"] == "review"
    assert report["auto_accept"] is False


def test_native_footnote_decision_reuses_existing_normalization_pipeline(
    tmp_path: Path,
):
    _write_native_sidecar(
        tmp_path,
        1,
        [
            _native_block("Body text 36", 0, y0=90),
            _native_block("36 Native footnote text", 1, y0=680, font_size=8),
        ],
    )
    (tmp_path / "pages" / "page_001.md").write_text(
        "Body text <sup>36</sup>\n\n36 Native footnote text\n",
        encoding="utf-8",
    )
    (tmp_path / "toc_tree.json").write_text("{}", encoding="utf-8")
    source = tmp_path / "ocr_markdown"
    source.mkdir()
    (source / "chapter_1.md").write_text(
        "Body text <sup>36</sup>\n\n36 Native footnote text\n",
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
                        "page_range": [1, 1],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    config = {"ocr": {"secondary": {"enabled": True, "backend": "paddle"}}}
    manifest_paths = prepare_footnote_subagent(
        tmp_path, book_title="Native Test", config=config
    )
    manifest = json.loads(manifest_paths["manifest"].read_text(encoding="utf-8"))
    assert manifest["status"] == "no_subagent_review_required"

    validation = validate_footnote_decisions(tmp_path, config=config)
    assert validation["valid"] is True
    assert validation["status"] == "no_subagent_review_required"

    result = apply_footnote_normalization(tmp_path, config=config)

    assert result["valid"] is True
    normalized = (tmp_path / "footnote_normalized" / "chapter_1.md").read_text(
        encoding="utf-8"
    )
    assert "Body text [^36]" in normalized
    assert "[^36]: Native footnote text" in normalized
    assert "36 Native footnote text" not in normalized


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
        "pdf2epub.refine.pdf_evidence.consensus_is_current",
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


def test_apply_links_a_footnote_marker_from_the_previous_oversized_part(
    tmp_path: Path,
):
    _write_sidecar(tmp_path, 1, [_block("Body 45", 0)])
    _write_sidecar(
        tmp_path,
        2,
        [_block("45 Footnote text", 0, label="Footnote", y0=820)],
    )
    pages = tmp_path / "pages"
    (pages / "page_001.md").write_text("Body <sup>45</sup>\n", encoding="utf-8")
    (pages / "page_002.md").write_text("45 Footnote text\n", encoding="utf-8")

    source = tmp_path / "ocr_markdown"
    source.mkdir()
    (source / "chapter_5.2.part1.md").write_text(
        "Body <sup>45</sup>\n", encoding="utf-8"
    )
    (source / "chapter_5.2.part2.md").write_text(
        "45 Footnote text\n", encoding="utf-8"
    )
    (source / "tree_progress.json").write_text(
        json.dumps(
            {
                "units": [
                    {
                        "unit_id": "chapter_5.2",
                        "index_path": [5, 2],
                        "part_files": [
                            "chapter_5.2.part1.md",
                            "chapter_5.2.part2.md",
                        ],
                        "file": "chapter_5.2.part1.md",
                        "page_range": [1, 2],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    prepare_footnote_subagent(tmp_path, book_title="Split chapter")
    assert validate_footnote_decisions(tmp_path)["valid"] is True

    result = apply_footnote_normalization(tmp_path)

    assert result["valid"] is True
    part1 = (tmp_path / "footnote_normalized" / "chapter_5.2.part1.md").read_text(
        encoding="utf-8"
    )
    part2 = (tmp_path / "footnote_normalized" / "chapter_5.2.part2.md").read_text(
        encoding="utf-8"
    )
    assert "Body [^45]" in part1
    assert "45 Footnote text" not in part2
    assert "[^45]: Footnote text" in part2
