from pathlib import Path

from pdf2epub.ocr_anomalies import detect_ocr_commentary, scan_ocr_pages
from pdf2epub.commands.sources import _require_reviewed_ocr_anomalies
from pdf2epub.ocr_correction import OCR_PAGE_REVIEW_SCHEMA_VERSION, validate_ocr_correction_reviews
from pdf2epub.workflow_contracts import sha256_file
import pytest
import json
from types import SimpleNamespace


def test_detects_quoted_incident_and_variants():
    assert detect_ocr_commentary(
        "The following text is extremely faint and largely illegible. It appears to be a historical account. The content is too faded to be transcribed accurately."
    )
    assert detect_ocr_commentary("The text on this page cannot be read or transcribed.")
    assert detect_ocr_commentary("The text on this page cannot be read.")
    assert detect_ocr_commentary("页面文字过于模糊，无法准确转录。")


def test_does_not_flag_normal_prose_or_legitimate_markers():
    assert not detect_ocr_commentary("The result is unclear, but the argument appears to be sound.")
    assert not detect_ocr_commentary("The manuscript contains [illegible] in the margin.")
    assert not detect_ocr_commentary('The author writes, “the image is illegible,” as an example.')
    assert not detect_ocr_commentary("The text cannot be read without understanding the political situation.")
    assert not detect_ocr_commentary('The author writes: “The text cannot be read in isolation.”')
    assert not detect_ocr_commentary("It appears to be a historical account of the strike.")
    assert not detect_ocr_commentary("史家认为该文献难以辨认其政治立场。")


def test_scan_reports_current_page_hash_and_reasons(tmp_path: Path):
    pages = tmp_path / "pages"
    pages.mkdir()
    (pages / "page_001.md").write_text("This page is too faint to transcribe.", encoding="utf-8")
    (pages / "ocr_progress.json").write_text('{"source_sha256":"pdf-hash"}', encoding="utf-8")
    report = scan_ocr_pages(tmp_path)
    assert report["source_sha256"] == "pdf-hash"
    assert report["review_required_files"] == ["page_001.md"]
    assert report["page_hashes"]["page_001.md"]
    assert (tmp_path / "ocr_anomaly_report.json").is_file()


def test_detects_mixed_transcription_and_commentary_paragraph():
    text = "A real sentence from the book remains here.\n\nThis page is too faded to be transcribed accurately."
    assert detect_ocr_commentary(text)


def test_unreadable_raw_page_fails_closed(tmp_path: Path):
    pages = tmp_path / "pages"
    pages.mkdir()
    (pages / "page_001.md").write_bytes(b"\xff")
    report = scan_ocr_pages(tmp_path, persist=False)
    assert report["all_clear"] is False
    assert report["scan_errors"]
    with pytest.raises(ValueError, match="could not read every raw page"):
        _require_reviewed_ocr_anomalies(tmp_path, {})


def test_downstream_source_gate_blocks_pending_anomaly_but_ignores_no_anomaly(tmp_path: Path):
    pages = tmp_path / "pages"
    pages.mkdir()
    (pages / "page_001.md").write_text("The page cannot be read or transcribed.", encoding="utf-8")
    with pytest.raises(ValueError, match="requires visual review"):
        _require_reviewed_ocr_anomalies(tmp_path, {"ocr": {"secondary": {"enabled": False}}})
    (pages / "page_001.md").write_text("A regular paragraph with the word unclear.", encoding="utf-8")
    _require_reviewed_ocr_anomalies(tmp_path, {"ocr": {"secondary": {"enabled": False}}})


def test_anomaly_correction_requires_hash_bound_decision_and_rejects_commentary(tmp_path: Path):
    pages = tmp_path / "pages"
    target_dir = tmp_path / "ocr_corrected_pages"
    image_dir = tmp_path / "ocr_review_images"
    records = tmp_path / "ocr_correction_reviews"
    for directory in (pages, target_dir, image_dir, records):
        directory.mkdir(parents=True, exist_ok=True)
    source = pages / "page_001.md"
    target = target_dir / source.name
    image = image_dir / "page_001.png"
    source.write_text("The page is too faint to transcribe.", encoding="utf-8")
    target.write_text("A historical ledger records the transaction.", encoding="utf-8")
    image.write_bytes(b"synthetic image")

    def write_record(decision="transcribed"):
        (records / "page_001.json").write_text(json.dumps({
            "schema_version": OCR_PAGE_REVIEW_SCHEMA_VERSION,
            "source_file": source.name,
            "visual_file": "ocr_review_images/page_001.png",
            "reviewed": True,
            "coverage": "complete",
            "uncertain": False,
            "source_line_count": 1,
            "source_nonempty_line_count": 1,
            "target_line_count": len(target.read_text(encoding="utf-8").splitlines()),
            "target_nonempty_line_count": 1,
            "anomaly_decision": decision,
            "source_sha256": sha256_file(source),
            "target_sha256": sha256_file(target),
            "image_sha256": sha256_file(image),
        }), encoding="utf-8")

    write_record()
    assert validate_ocr_correction_reviews(tmp_path, pages, target_dir)["valid"]
    target.write_text("The page is too faint to transcribe.", encoding="utf-8")
    write_record()
    report = validate_ocr_correction_reviews(tmp_path, pages, target_dir)
    assert not report["valid"]
    assert any("still contains OCR commentary" in item["reason"] for item in report["errors"])


def test_single_ocr_validate_command_stages_reviewed_anomaly_and_pass_through(
    tmp_path: Path, monkeypatch
):
    from pdf2epub.commands import ocr as ocr_commands

    pages = tmp_path / "pages"
    target_dir = tmp_path / "ocr_corrected_pages"
    image_dir = tmp_path / "ocr_review_images"
    records = tmp_path / "ocr_correction_reviews"
    for directory in (pages, target_dir, image_dir, records):
        directory.mkdir(parents=True, exist_ok=True)
    anomaly = pages / "page_001.md"
    ordinary = pages / "page_002.md"
    anomaly.write_text("The page is too faint to transcribe.", encoding="utf-8")
    ordinary.write_text("Ordinary printed text.", encoding="utf-8")
    target = target_dir / anomaly.name
    target.write_text("A transcribed ledger entry.", encoding="utf-8")
    image = image_dir / "page_001.png"
    image.write_bytes(b"synthetic review image")
    (records / "page_001.json").write_text(json.dumps({
        "schema_version": OCR_PAGE_REVIEW_SCHEMA_VERSION,
        "source_file": anomaly.name,
        "visual_file": "ocr_review_images/page_001.png",
        "reviewed": True,
        "coverage": "complete",
        "uncertain": False,
        "source_line_count": 1,
        "source_nonempty_line_count": 1,
        "target_line_count": 1,
        "target_nonempty_line_count": 1,
        "anomaly_decision": "transcribed",
        "source_sha256": sha256_file(anomaly),
        "target_sha256": sha256_file(target),
        "image_sha256": sha256_file(image),
    }), encoding="utf-8")
    (pages / "ocr_progress.json").write_text('{"source_sha256":"synthetic-pdf"}', encoding="utf-8")

    context = SimpleNamespace(
        output_dir=tmp_path,
        config={"ocr": {"secondary": {"enabled": False}}},
        config_path=tmp_path / "config.yaml",
    )
    monkeypatch.setattr(ocr_commands, "load_book_context", lambda *_args: context)
    monkeypatch.setattr(ocr_commands, "_require_complete_ocr_pages", lambda _path: True)
    monkeypatch.setattr("pdf2epub.ocr_correction.review_images_are_current", lambda _path: True)

    def fake_markdown_validation(_output, _task, source_dir, work_dir, **_kwargs):
        validated = work_dir / "validated"
        validated.mkdir(exist_ok=True)
        import shutil

        for source_path in source_dir.glob("page_*.md"):
            shutil.copy2(work_dir / source_path.name, validated / source_path.name)
        return {
            "schema_version": 2,
            "task": "ocr-correct",
            "all_passed": True,
            "completed": 2,
            "total": 2,
            "missing": [],
            "invalid": [],
            "review_required": [],
            "review_required_files": [],
            "retry_required": [],
            "retry_required_files": [],
            "human_review_required": [],
            "human_review_required_files": [],
            "valid_files": [anomaly.name, ordinary.name],
            "source_sha256": {p.name: sha256_file(p) for p in source_dir.glob("page_*.md")},
            "target_sha256": {p.name: sha256_file(p) for p in validated.glob("page_*.md")},
        }

    monkeypatch.setattr(
        "pdf2epub.markdown_subagent_validation.validate_markdown_subagent",
        fake_markdown_validation,
    )
    result = ocr_commands.ocr_correct_validate_command(SimpleNamespace(file=None))
    assert result == 0
    validation = json.loads((tmp_path / "ocr-correct_validation.json").read_text(encoding="utf-8"))
    assert validation["all_passed"] is True
    assert validation["evidence_mode"] == "single_ocr_anomaly_review"
    assert validation["visual_review_files"] == [anomaly.name]
    assert validation["pass_through_files"] == [ordinary.name]
    assert (tmp_path / "ocr_corrected_pages" / "validated" / ordinary.name).is_file()
