from pathlib import Path

from pdf2epub.ocr_anomalies import detect_ocr_commentary, scan_ocr_pages
from pdf2epub.commands.sources import _require_reviewed_ocr_anomalies
from pdf2epub.ocr_correction import OCR_PAGE_REVIEW_SCHEMA_VERSION, validate_ocr_correction_reviews
from pdf2epub.workflow_contracts import sha256_file
import pytest
import json
from types import SimpleNamespace
import hashlib


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


def _long_text() -> str:
    return " ".join(["transcribed"] * 90)


def _write_scan_pdf(output_dir: Path, draw=None) -> str:
    import pymupdf as fitz

    pdf = output_dir / "input_original.pdf"
    document = fitz.open()
    page = document.new_page(width=300, height=400)
    if draw is not None:
        draw(page)
    document.save(pdf)
    document.close()
    digest = hashlib.sha256(pdf.read_bytes()).hexdigest()
    pages = output_dir / "pages"
    pages.mkdir(exist_ok=True)
    (pages / "ocr_progress.json").write_text(
        json.dumps({"source_sha256": digest, "total_pages": 1}), encoding="utf-8"
    )
    return digest


def test_long_text_on_blank_pdf_page_requires_targeted_review_and_reuses_evidence(tmp_path):
    from pdf2epub.ocr_anomalies import scan_ocr_page_evidence

    _write_scan_pdf(tmp_path)
    pages = tmp_path / "pages"
    (pages / "page_001.md").write_text(_long_text(), encoding="utf-8")
    first = scan_ocr_page_evidence(tmp_path, persist=True).report
    assert first["review_required_files"] == ["page_001.md"]
    assert first["anomalies"]["page_001.md"]["image_evidence"] == "blank_body"

    (pages / "page_002.md").write_text("An unrelated page was edited.", encoding="utf-8")
    second = scan_ocr_page_evidence(tmp_path, persist=False).report
    assert second["review_required_files"] == ["page_001.md"]
    assert second["anomalies"]["page_001.md"]["sha256"] == first["anomalies"]["page_001.md"]["sha256"]


def test_processed_pdf_stamps_do_not_replace_original_ocr_evidence(tmp_path):
    import pymupdf as fitz
    from pdf2epub.ocr_anomalies import scan_ocr_pages

    digest = _write_scan_pdf(tmp_path)
    (tmp_path / "pages" / "page_001.md").write_text(_long_text(), encoding="utf-8")
    processed = fitz.open()
    page = processed.new_page(width=300, height=400)
    page.draw_rect(fitz.Rect(0, 0, 300, 20), color=(0, 0, 0), fill=(0, 0, 0))
    page.insert_text((8, 15), "synthetic OCR page stamp", fontsize=8, color=(1, 1, 1))
    processed.save(tmp_path / "input.pdf")
    processed.close()

    report = scan_ocr_pages(tmp_path)

    assert report["source_sha256"] == digest
    assert report["review_required_files"] == ["page_001.md"]


def test_changed_pdf_and_tampered_rendered_image_cannot_certify_blankness(tmp_path):
    from pdf2epub.ocr_anomalies import _render_page, scan_ocr_pages

    digest = _write_scan_pdf(tmp_path)
    pages = tmp_path / "pages"
    (pages / "page_001.md").write_text(_long_text(), encoding="utf-8")
    assert scan_ocr_pages(tmp_path)["review_required_files"] == ["page_001.md"]

    import pymupdf as fitz
    pdf = tmp_path / "input_original.pdf"
    doc = fitz.open()
    page = doc.new_page(width=300, height=400)
    page.insert_text((100, 160), "changed PDF", fontsize=10)
    replacement = tmp_path / "replacement.pdf"
    doc.save(replacement)
    doc.close()
    pdf.write_bytes(replacement.read_bytes())
    changed = scan_ocr_pages(tmp_path, persist=False)
    assert changed["review_required_files"] == []
    assert changed["scan_diagnostics"]

    # Restore a current source, then prove an image without a matching content
    # hash in the render receipt cannot supply blankness evidence.
    digest = _write_scan_pdf(tmp_path)
    image_dir = tmp_path / "ocr_review_images"
    image_dir.mkdir(exist_ok=True)
    image_path = image_dir / "page_001.png"
    image_bytes = _render_page(tmp_path / "input_original.pdf", 1)
    image_path.write_bytes(image_bytes)
    (tmp_path / "ocr_review_images.json").write_text(json.dumps({
        "source_sha256": digest,
        "page_count": 1,
        "images": ["page_001.png"],
        "image_sha256": {"page_001.png": hashlib.sha256(image_bytes).hexdigest()},
    }), encoding="utf-8")
    image_path.write_bytes(b"tampered")
    (tmp_path / "ocr_anomaly_report.json").unlink(missing_ok=True)
    stale_image = scan_ocr_pages(tmp_path, persist=False)
    assert stale_image["review_required_files"] == []
    assert "blankness_evidence_insufficient" in stale_image["advisory_pages"]["page_001.md"]


def test_malformed_cached_candidate_is_recomputed(tmp_path, monkeypatch):
    from pdf2epub import ocr_anomalies

    _write_scan_pdf(tmp_path)
    (tmp_path / "pages" / "page_001.md").write_text(_long_text(), encoding="utf-8")
    original = ocr_anomalies.scan_ocr_pages(tmp_path)
    original["visual_candidates"]["page_001.md"]["body_ink_ratio"] = "invalid"
    (tmp_path / "ocr_anomaly_report.json").write_text(json.dumps(original), encoding="utf-8")
    renders = []
    render = ocr_anomalies._render_page

    def counted_render(pdf, page_number):
        renders.append(page_number)
        return render(pdf, page_number)

    monkeypatch.setattr(ocr_anomalies, "_render_page", counted_render)
    report = ocr_anomalies.scan_ocr_pages(tmp_path)
    assert renders == [1]
    assert report["review_required_files"] == ["page_001.md"]


def test_blankness_candidates_are_conservative_for_noise_sparse_text_and_edge_imprint(tmp_path):
    from pdf2epub.ocr_anomalies import scan_ocr_pages

    def draw_scanner_noise(page):
        for x, y in ((140, 190), (142, 220), (160, 205), (180, 180)):
            page.draw_circle((x, y), 0.15, color=(0, 0, 0), fill=(0, 0, 0))

    _write_scan_pdf(tmp_path, draw_scanner_noise)
    pages = tmp_path / "pages"
    (pages / "page_001.md").write_text(_long_text(), encoding="utf-8")
    report = scan_ocr_pages(tmp_path)
    assert report["review_required_files"] == ["page_001.md"]

    def draw_sparse(page):
        page.insert_text((145, 200), "A citation", fontsize=10)

    _write_scan_pdf(tmp_path, draw_sparse)
    (pages / "page_001.md").write_text(_long_text(), encoding="utf-8")
    report = scan_ocr_pages(tmp_path)
    assert report["review_required_files"] == []
    assert "long_ocr_candidate_with_visible_body_content" in report["advisory_pages"]["page_001.md"]

    def draw_edge_imprint(page):
        page.insert_text((2, 9), "XVII", fontsize=6)

    _write_scan_pdf(tmp_path, draw_edge_imprint)
    report = scan_ocr_pages(tmp_path)
    assert report["review_required_files"] == []
    assert "body_sparse_but_full_page_content_present" in report["advisory_pages"]["page_001.md"]


def test_repeated_long_ocr_text_is_advisory_and_missing_pdf_is_insufficient(tmp_path):
    from pdf2epub.ocr_anomalies import scan_ocr_pages

    pages = tmp_path / "pages"
    pages.mkdir()
    text = _long_text()
    (pages / "page_001.md").write_text(text, encoding="utf-8")
    (pages / "page_002.md").write_text(text, encoding="utf-8")
    (pages / "ocr_progress.json").write_text('{"source_sha256":"missing"}', encoding="utf-8")
    report = scan_ocr_pages(tmp_path)
    assert report["review_required_files"] == []
    assert "repeated_ocr_text" in report["advisory_pages"]["page_001.md"]
    assert "blankness_evidence_insufficient" in report["advisory_pages"]["page_001.md"]
    assert report["scan_diagnostics"]


def test_read_only_scan_does_not_render_or_write_when_candidate_image_is_missing(tmp_path, monkeypatch):
    from pdf2epub import ocr_anomalies

    digest = _write_scan_pdf(tmp_path)
    (tmp_path / "pages" / "page_001.md").write_text(_long_text(), encoding="utf-8")
    monkeypatch.setattr(
        ocr_anomalies,
        "_render_page",
        lambda *_args: (_ for _ in ()).throw(AssertionError("read-only scan rendered")),
    )

    report = ocr_anomalies.scan_ocr_pages(tmp_path, persist=False)

    assert report["source_sha256"] == digest
    assert report["review_required_files"] == []
    assert report["scan_diagnostics"]
    assert not (tmp_path / "ocr_anomaly_report.json").exists()


def test_genuine_english_citation_with_visible_ink_is_not_a_blank_page_anomaly(tmp_path):
    import pymupdf as fitz
    from pdf2epub.ocr_anomalies import scan_ocr_pages

    citation = "Smith, J. (2020). Evidence and inference in historical research. Journal of History, 12(3), 44–61. " * 5
    def draw_citation(page):
        page.insert_textbox(fitz.Rect(35, 80, 265, 240), citation, fontsize=10)

    _write_scan_pdf(tmp_path, draw_citation)
    (tmp_path / "pages" / "page_001.md").write_text(citation, encoding="utf-8")
    report = scan_ocr_pages(tmp_path)
    assert report["review_required_files"] == []


def test_ocr_correct_cli_resume_reuses_unchanged_page_review(tmp_path, monkeypatch):
    import pymupdf as fitz
    from pdf2epub.commands import ocr as ocr_commands
    from pdf2epub.ocr_correction import ocr_correction_is_current
    from pdf2epub.ocr_progress import new_progress

    pdf = tmp_path / "input_original.pdf"
    document = fitz.open()
    document.new_page(width=300, height=400)
    document.new_page(width=300, height=400)
    document.new_page(width=300, height=400)
    document.save(pdf)
    document.close()
    digest = sha256_file(pdf)
    pages = tmp_path / "pages"
    pages.mkdir()
    long_text = _long_text()
    for number in (1, 2):
        (pages / f"page_{number:03d}.md").write_text(long_text, encoding="utf-8")
    (pages / "page_003.md").write_text("Ordinary printed page.", encoding="utf-8")
    progress = new_progress(source_sha256=digest, total_pages=3, backend="synthetic")
    progress["pages_processed"] = [1, 2, 3]
    progress["missing_pages"] = []
    (pages / "ocr_progress.json").write_text(json.dumps(progress), encoding="utf-8")

    context = SimpleNamespace(
        output_dir=tmp_path,
        config={"ocr": {"secondary": {"enabled": False}}},
        config_path=tmp_path / "config.yaml",
    )
    monkeypatch.setattr(ocr_commands, "load_book_context", lambda *_args: context)
    monkeypatch.setattr(ocr_commands, "_require_complete_ocr_pages", lambda *_args, **_kwargs: True)
    first_args = SimpleNamespace(resume=False, file=None, dpi=None)
    assert ocr_commands.ocr_correct_command(first_args) == 0

    source_dir = pages
    work_dir = tmp_path / "ocr_corrected_pages"
    image_dir = tmp_path / "ocr_review_images"
    record_dir = tmp_path / "ocr_correction_reviews"
    work_dir.mkdir(exist_ok=True)
    record_dir.mkdir(exist_ok=True)
    for number in (1, 2):
        name = f"page_{number:03d}.md"
        source = source_dir / name
        target = work_dir / name
        target.write_text(f"Printed ledger entry {number}.", encoding="utf-8")
        image = image_dir / f"page_{number:03d}.png"
        record = {
            "schema_version": OCR_PAGE_REVIEW_SCHEMA_VERSION,
            "source_file": name,
            "visual_file": f"ocr_review_images/page_{number:03d}.png",
            "reviewed": True,
            "coverage": "complete",
            "uncertain": False,
            "source_line_count": 1,
            "source_nonempty_line_count": 1,
            "target_line_count": 1,
            "target_nonempty_line_count": 1,
            "anomaly_decision": "transcribed",
            "source_sha256": sha256_file(source),
            "target_sha256": sha256_file(target),
            "image_sha256": sha256_file(image),
        }
        (record_dir / f"page_{number:03d}.json").write_text(json.dumps(record), encoding="utf-8")

    assert ocr_commands.ocr_correct_validate_command(SimpleNamespace(file=None)) == 0
    assert ocr_correction_is_current(tmp_path, context.config) is True

    from pdf2epub.ocr_correction import render_review_images
    render_review_images(tmp_path, resume=True)
    assert ocr_correction_is_current(tmp_path, context.config) is True
    manifest_path = tmp_path / "ocr_review_images.json"
    manifest_before = manifest_path.read_bytes()
    render_review_images(tmp_path, resume=True, page_names=["page_001.md"])
    assert manifest_path.read_bytes() == manifest_before

    (pages / "page_002.md").write_text(_long_text() + " revised", encoding="utf-8")
    from pdf2epub import ocr_anomalies
    rendered_candidates = []
    original_render = ocr_anomalies._render_page

    def track_render(pdf_path, page_number):
        rendered_candidates.append(page_number)
        return original_render(pdf_path, page_number)

    monkeypatch.setattr(ocr_anomalies, "_render_page", track_render)
    second_args = SimpleNamespace(resume=True, file=None, dpi=None)
    assert ocr_commands.ocr_correct_command(second_args) == 0
    assert rendered_candidates == [2]
    manifest = json.loads((tmp_path / "ocr-correct_subagent_manifest.json").read_text(encoding="utf-8"))
    assert manifest["pending_files"] == ["page_002.md"]
    assert manifest["completed_files"] == ["page_001.md"]


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
    monkeypatch.setattr("pdf2epub.ocr_correction.review_images_are_current", lambda *_args: True)

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
