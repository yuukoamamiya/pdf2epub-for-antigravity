import json
from pathlib import Path

from pdf2epub.markdown_handoff import prepare_markdown_subagent
from pdf2epub.ocr_correction import (
    OCR_REVIEW_IMAGE_SCHEMA_VERSION,
    corrected_page_dir,
    ocr_correction_is_current,
    select_refinement_pages,
    validate_ocr_correction_reviews,
)
from pdf2epub.subagent_runtime import write_worker_handoffs
from pdf2epub.workflow_contracts import MARKDOWN_VALIDATION_SCHEMA_VERSION, sha256_file


def _write_current_review_manifest(output_dir: Path) -> None:
    pdf = output_dir / "input_original.pdf"
    pdf.write_bytes(b"source pdf placeholder")
    image_dir = output_dir / "ocr_review_images"
    image_dir.mkdir()
    (image_dir / "page_001.png").write_bytes(b"png")
    (output_dir / "ocr_review_images.json").write_text(
        json.dumps(
            {
                "schema_version": OCR_REVIEW_IMAGE_SCHEMA_VERSION,
                "source_pdf": pdf.name,
                "source_sha256": sha256_file(pdf),
                "dpi": 150,
                "page_count": 1,
                "image_dir": image_dir.name,
                "images": ["page_001.png"],
            }
        ),
        encoding="utf-8",
    )


def test_ocr_correction_checkpoint_selects_validated_pages(tmp_path: Path):
    pages = tmp_path / "pages"
    pages.mkdir()
    source = pages / "page_001.md"
    source.write_text("# Source\n", encoding="utf-8")
    validated = tmp_path / "ocr_corrected_pages" / "validated"
    validated.mkdir(parents=True)
    target = validated / source.name
    target.write_text("# Corrected\n", encoding="utf-8")
    _write_current_review_manifest(tmp_path)
    review_dir = tmp_path / "ocr_correction_reviews"
    review_dir.mkdir()
    (review_dir / "page_001.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "source_file": "page_001.md",
                "visual_file": "ocr_review_images/page_001.png",
                "reviewed": True,
                "coverage": "complete",
                "uncertain": False,
                "source_line_count": 1,
                "source_nonempty_line_count": 1,
                "target_line_count": 1,
                "target_nonempty_line_count": 1,
            }
        ),
        encoding="utf-8",
    )
    (tmp_path / "pdf_text_probe.json").write_text(
        json.dumps({"classification": "scanned", "recommendation": "ocr_required"}),
        encoding="utf-8",
    )
    (tmp_path / "ocr-correct_validation.json").write_text(
        json.dumps(
            {
                "schema_version": MARKDOWN_VALIDATION_SCHEMA_VERSION,
                "task": "ocr-correct",
                "all_passed": True,
                "valid_files": [source.name],
                "source_sha256": {source.name: sha256_file(source)},
                "target_sha256": {target.name: sha256_file(target)},
            }
        ),
        encoding="utf-8",
    )

    selected, kind = select_refinement_pages(tmp_path, require_correction=True)

    assert selected == corrected_page_dir(tmp_path)
    assert kind == "ocr_corrected"
    assert ocr_correction_is_current(tmp_path)

    source.write_text("# Changed\n", encoding="utf-8")
    assert not ocr_correction_is_current(tmp_path)


def test_native_text_skips_visual_correction(tmp_path: Path):
    pages = tmp_path / "pages"
    pages.mkdir()
    (pages / "page_001.md").write_text("native", encoding="utf-8")
    (tmp_path / "pdf_text_probe.json").write_text(
        json.dumps(
            {
                "classification": "native_text",
                "recommendation": "use_text_layer",
            }
        ),
        encoding="utf-8",
    )

    selected, kind = select_refinement_pages(tmp_path, require_correction=True)

    assert selected == pages
    assert kind == "native_text"


def test_single_ocr_mode_uses_raw_pages_without_correction_checkpoint(tmp_path: Path):
    pages = tmp_path / "pages"
    pages.mkdir()
    (pages / "page_001.md").write_text("primary OCR", encoding="utf-8")
    (tmp_path / "pdf_text_probe.json").write_text(
        json.dumps(
            {
                "classification": "scanned",
                "recommendation": "ocr_required",
            }
        ),
        encoding="utf-8",
    )

    selected, kind = select_refinement_pages(
        tmp_path,
        require_correction=True,
        config={"ocr": {"secondary": {"enabled": False, "backend": "vision"}}},
    )

    assert selected == pages
    assert kind == "ocr"
    assert not ocr_correction_is_current(
        tmp_path,
        {"ocr": {"secondary": {"enabled": False, "backend": "vision"}}},
    )


def test_two_ocr_correction_checkpoint_requires_current_consensus(
    tmp_path: Path, monkeypatch
):
    pages = tmp_path / "pages"
    pages.mkdir()
    source = pages / "page_001.md"
    source.write_text("raw OCR\n", encoding="utf-8")
    validated = tmp_path / "ocr_corrected_pages" / "validated"
    validated.mkdir(parents=True)
    target = validated / source.name
    target.write_text("corrected OCR\n", encoding="utf-8")

    (tmp_path / "ocr-correct_validation.json").write_text(
        json.dumps(
            {
                "schema_version": MARKDOWN_VALIDATION_SCHEMA_VERSION,
                "task": "ocr-correct",
                "all_passed": True,
                "valid_files": [source.name],
                "source_sha256": {source.name: sha256_file(source)},
                "target_sha256": {target.name: sha256_file(target)},
            }
        ),
        encoding="utf-8",
    )

    # Simulate a legacy correction checkpoint whose page-level review evidence
    # still exists but whose required two-OCR consensus checkpoint is absent.
    monkeypatch.setattr(
        "pdf2epub.ocr_correction.review_images_are_current", lambda _output: True
    )
    monkeypatch.setattr(
        "pdf2epub.ocr_correction.validate_ocr_correction_reviews",
        lambda *args, **kwargs: {"valid": True},
    )

    assert not ocr_correction_is_current(
        tmp_path,
        {"ocr": {"secondary": {"enabled": True, "backend": "vision"}}},
    )


def test_ocr_correction_review_rejects_dropped_lines(tmp_path: Path):
    pages = tmp_path / "pages"
    pages.mkdir()
    source = pages / "page_001.md"
    source.write_text("line one\nline two\nline three\n", encoding="utf-8")
    target_dir = tmp_path / "ocr_corrected_pages"
    target_dir.mkdir()
    (target_dir / source.name).write_text("line one\nline two\n", encoding="utf-8")
    review_dir = tmp_path / "ocr_correction_reviews"
    review_dir.mkdir()
    (review_dir / "page_001.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "source_file": source.name,
                "visual_file": "ocr_review_images/page_001.png",
                "reviewed": True,
                "coverage": "complete",
                "uncertain": False,
                "source_line_count": 3,
                "source_nonempty_line_count": 3,
                "target_line_count": 2,
                "target_nonempty_line_count": 2,
            }
        ),
        encoding="utf-8",
    )

    report = validate_ocr_correction_reviews(tmp_path, pages, target_dir)

    assert report["valid"] is False
    assert any("fewer total lines" in item["reason"] for item in report["errors"])


def test_ocr_correction_handoff_scopes_visual_assets(tmp_path: Path):
    pages = tmp_path / "pages"
    pages.mkdir()
    (pages / "page_001.md").write_text("OCR text", encoding="utf-8")
    review_images = tmp_path / "ocr_review_images"
    review_images.mkdir()

    paths = prepare_markdown_subagent(
        tmp_path,
        "ocr-correct",
        pages,
        tmp_path / "ocr_corrected_pages",
        "English",
        "Original",
        ["Compare each page with its matching visual review image."],
        visual_review_dir=review_images,
        review_output_dir=tmp_path / "ocr_correction_reviews",
    )
    handoffs = write_worker_handoffs(
        tmp_path,
        paths["manifest"],
        paths["prompt"],
        handoff_dir_name="ocr-correct_worker_handoffs",
    )

    manifest = json.loads(paths["manifest"].read_text(encoding="utf-8"))
    worker_manifest = json.loads(
        (tmp_path / handoffs[0]["manifest"]).read_text(encoding="utf-8")
    )
    worker_prompt = (tmp_path / handoffs[0]["prompt"]).read_text(encoding="utf-8")
    assert manifest["visual_review_dir"] == "ocr_review_images"
    assert manifest["review_output_dir"] == "ocr_correction_reviews"
    assert worker_manifest["visual_review_dir"] == "ocr_review_images"
    assert worker_manifest["review_output_dir"] == "ocr_correction_reviews"
    assert "ocr_review_images/page_NNN.png" in worker_prompt
    assert "ocr_correction_reviews/page_NNN.json" in worker_prompt
    assert "only corrects visually evidenced OCR errors" in worker_prompt


def test_polish_handoff_defers_ocr_corrections_to_page_gate(tmp_path: Path):
    source_dir = tmp_path / "ocr_markdown"
    source_dir.mkdir()
    (source_dir / "chapter_001.md").write_text("OCR-derived text\n", encoding="utf-8")

    paths = prepare_markdown_subagent(
        tmp_path,
        "polish",
        source_dir,
        tmp_path / "polished_markdown",
        "English",
        "Original",
    )

    prompt = paths["prompt"].read_text(encoding="utf-8")
    assert "already passed the page-level OCR correction gate" in prompt
    assert "Do not perform OCR character correction" in prompt
    assert "fix obvious OCR errors" not in prompt
