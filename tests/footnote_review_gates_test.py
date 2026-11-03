import json

from pdf2epub.refine.footnote_continuation_review import (
    validate_continuation_review_decisions,
)
from pdf2epub.refine.footnote_options import footnote_mode_warnings
from pdf2epub.refine.footnote_report import build_footnote_report
from pdf2epub.refine.footnote_handoff import prepare_footnote_subagent
from pdf2epub.refine.footnote_validation import validate_footnote_decisions
from pdf2epub.refine.footnote_apply import apply_footnote_normalization
from pdf2epub.refine.footnote_status import footnote_normalization_status
from pdf2epub.refine.footnote_windows import build_missing_continuation_reviews


def _page(page, candidates):
    return {"page": page, "candidates": candidates}


def _candidate(block, text, *, order=1, confidence="high", key="36"):
    return {
        "page": 1,
        "source": "primary",
        "block": block,
        "order": order,
        "text": text,
        "key": key,
        "confidence": confidence,
        "local_role": "footnote_start",
        "label": "Footnote",
    }


def test_dangling_final_note_word_creates_missing_next_page_review():
    reviews = build_missing_continuation_reviews(
        [_page(1, [_candidate(4, "36. This continues von vorn-")])],
        page_count=2,
    )
    assert len(reviews) == 1
    assert reviews[0]["pages"] == [1, 2]
    assert reviews[0]["source_note"]["block"] == 4


def test_missing_continuation_detector_skips_prose_nonfinal_and_last_page():
    candidates = [
        {**_candidate(1, "17. ordinary prose ends-"), "label": "Text"},
        _candidate(2, "36. a complete note" , order=2),
    ]
    reviews = build_missing_continuation_reviews(
        [_page(1, candidates), _page(2, [_candidate(3, "36. vorn-", order=3)])],
        page_count=2,
    )
    assert len(reviews) == 1
    assert reviews[0]["terminal_page"] is True
    assert reviews[0]["pages"] == [2]


def test_report_uses_all_scanned_sidecars_for_last_physical_page(monkeypatch, tmp_path):
    import pdf2epub.refine.footnote_report as report_module

    images = {}
    for page in (1, 2):
        path = tmp_path / f"page_{page:03d}.png"
        path.write_bytes(f"page {page}".encode())
        images[str(page)] = path.relative_to(tmp_path).as_posix()
    monkeypatch.setattr(report_module, "ensure_visual_review_images", lambda *_: {
        "available": True, "files": images,
    })
    monkeypatch.setattr(report_module, "ensure_region_review_images", lambda *_: {})
    page_reports = [_page(1, [_candidate(4, "36. continues from vorn-")])]
    page_reports[0]["sidecar"] = "pages/page_001.ocr.json"
    page_reports[0]["footnote_regions"] = []
    report = build_footnote_report(
        tmp_path,
        source_kind="ocr",
        evidence_mode="single_ocr",
        footnote_mode="footnotes",
        configured_footnote_mode="auto",
        footnote_mode_source="default",
        bottom_ratio=0.5,
        bottom_intersection_ratio=0.08,
        context_blocks=2,
        auto_accept=True,
        native_max_font_ratio=0.88,
        consensus_review_pages=[],
        sidecar_hashes={"pages/page_001.ocr.json": "a", "pages/page_002.ocr.json": "b"},
        secondary_sidecar_hashes={},
        page_reports=page_reports,
        review_pages=set(),
        soft_continuation_context_count=0,
        soft_continuation_context_pages=set(),
        bottom_detection={},
        suspected_missed_pages=set(),
        structure_excluded_pages=[],
        native_font_profile=None,
        config=None,
    )
    assert report["continuation_reviews"][0]["pages"] == [1, 2]
    assert report["continuation_reviews"][0]["terminal_page"] is False
    assert report["continuation_reviews"][0]["visual_files"]["2"]["sha256"]
    assert report["review_pages"] == [1, 2]


def test_next_page_new_note_does_not_hide_missing_continuation():
    next_note = _candidate(8, "37. A new note", order=8, key="37")
    reviews = build_missing_continuation_reviews(
        [_page(1, [_candidate(4, "36. continues from vorn-")]), _page(2, [next_note])],
        page_count=2,
    )
    assert len(reviews) == 1
    assert reviews[0]["pages"] == [1, 2]


def test_review_all_confidence_still_queues_dangling_labelled_footnote():
    candidate = _candidate(4, "36. continues from vorn-", confidence="review")
    reviews = build_missing_continuation_reviews(
        [_page(1, [candidate])], page_count=2,
    )
    assert len(reviews) == 1


def _write_ocr_page(output_dir, page, blocks):
    pages = output_dir / "pages"
    pages.mkdir(exist_ok=True)
    (pages / f"page_{page:03d}.ocr.json").write_text(json.dumps({
        "schema_version": 1,
        "page_number": page,
        "backend": "synthetic",
        "page_box": [0, 0, 1000, 1000],
        "blocks": blocks,
    }), encoding="utf-8")


def _prepare_continuation_book(tmp_path, monkeypatch):
    import pdf2epub.refine.footnote_report as report_module
    import pdf2epub.ocr_correction
    from pdf2epub.workflow_contracts import sha256_file

    block = {"order": 1, "label": "Footnote", "bbox": [80, 820, 920, 870],
             "html": "36 Footnote continues von vorn-"}
    _write_ocr_page(tmp_path, 1, [block])
    _write_ocr_page(tmp_path, 2, [])
    (tmp_path / "toc_tree.json").write_text("{}", encoding="utf-8")
    source = tmp_path / "ocr_markdown"
    source.mkdir()
    (source / "chapter_1.md").write_text(
        "In text <sup>36</sup>\n\n36 Footnote continues von vorn-\n", encoding="utf-8"
    )
    (source / "tree_progress.json").write_text(json.dumps({"units": [{
        "unit_id": "chapter_1", "file": "chapter_1.md", "page_range": [1, 2],
    }]}), encoding="utf-8")
    images = {}
    for page in (1, 2):
        file = tmp_path / f"page_{page:03d}.png"
        file.write_bytes(f"synthetic visual page {page}".encode())
        images[str(page)] = file.relative_to(tmp_path).as_posix()
    image_manifest = tmp_path / "ocr_review_images.json"
    image_manifest.write_text(json.dumps({"source_sha256": "synthetic-pdf-hash"}), encoding="utf-8")
    monkeypatch.setattr(report_module, "ensure_visual_review_images", lambda *_: {
        "available": True, "files": images,
    })
    monkeypatch.setattr(report_module, "ensure_region_review_images", lambda *_: {})
    monkeypatch.setattr(pdf2epub.ocr_correction, "review_images_are_current", lambda _path: True)
    paths = prepare_footnote_subagent(tmp_path, book_title="Synthetic Review Book")
    return paths, sha256_file


def test_prepare_handoff_validate_apply_and_status_require_explicit_resolution(tmp_path, monkeypatch):
    paths, _ = _prepare_continuation_book(tmp_path, monkeypatch)
    manifest = json.loads(paths["manifest"].read_text(encoding="utf-8"))
    report = json.loads(paths["report"].read_text(encoding="utf-8"))
    assert manifest["status"] == "pending_review"
    task = report["continuation_reviews"][0]
    decision = {
        "page": 1, "source": "primary", "block": 0,
        "role": "footnote_start", "key": "36",
    }
    decision_file = tmp_path / "footnote_decisions.json"
    decision_file.write_text(json.dumps({
        "schema_version": report["schema_version"], "decisions": [decision],
    }), encoding="utf-8")
    assert validate_footnote_decisions(tmp_path)["valid"] is False

    decision_file.write_text(json.dumps({
        "schema_version": report["schema_version"],
        "decisions": [decision],
        "continuation_reviews": [{
            "task_id": task["task_id"],
            "outcome": "recovered_continuation",
            "key": "36",
            "text": "ehm and continues",
            "reason": "The next page image visibly supplies the broken word suffix.",
            "evidence_pages": [1, 2],
        }],
    }), encoding="utf-8")
    assert validate_footnote_decisions(tmp_path)["valid"] is True
    normalized = apply_footnote_normalization(tmp_path)
    assert normalized["valid"] is True
    assert normalized["continuation_reviews"][0]["outcome"] == "recovered_continuation"
    text = (tmp_path / "footnote_normalized" / "chapter_1.md").read_text(encoding="utf-8")
    assert "[^36]: Footnote continues von vornehm and continues" in text
    assert "von vorn-" in (tmp_path / "ocr_markdown" / "chapter_1.md").read_text(encoding="utf-8")
    assert footnote_normalization_status(tmp_path)["current"] is True

    report_path = tmp_path / "footnote_candidates.json"
    validation_path = tmp_path / "footnote_decision_validation.json"
    normalization_path = tmp_path / "footnote_normalization.json"
    normalization_bytes = normalization_path.read_bytes()
    report_bytes = report_path.read_bytes()
    report.pop("continuation_reviews")
    report_path.write_text(json.dumps(report), encoding="utf-8")
    assert footnote_normalization_status(tmp_path)["current"] is False
    assert apply_footnote_normalization(tmp_path)["valid"] is False
    normalization_path.write_bytes(normalization_bytes)
    report_path.write_bytes(report_bytes)

    validation = json.loads(validation_path.read_text(encoding="utf-8"))
    validation_bytes = validation_path.read_bytes()
    validation.pop("continuation_reviews")
    validation_path.write_text(json.dumps(validation), encoding="utf-8")
    assert footnote_normalization_status(tmp_path)["current"] is False
    assert apply_footnote_normalization(tmp_path)["valid"] is False
    normalization_path.write_bytes(normalization_bytes)

    validation = json.loads(validation_bytes)
    validation["candidate_report_sha256"] = "0" * 64
    validation_path.write_text(json.dumps(validation), encoding="utf-8")
    assert footnote_normalization_status(tmp_path)["current"] is False
    assert apply_footnote_normalization(tmp_path)["valid"] is False
    normalization_path.write_bytes(normalization_bytes)
    validation_path.write_bytes(validation_bytes)

    (tmp_path / "page_002.png").write_bytes(b"changed after validation")
    assert footnote_normalization_status(tmp_path)["current"] is False
    assert apply_footnote_normalization(tmp_path)["valid"] is False


def test_image_confirmed_body_candidate_is_not_moved(tmp_path, monkeypatch):
    paths, _ = _prepare_continuation_book(tmp_path, monkeypatch)
    report = json.loads(paths["report"].read_text(encoding="utf-8"))
    task = report["continuation_reviews"][0]
    (tmp_path / "footnote_decisions.json").write_text(json.dumps({
        "schema_version": report["schema_version"],
        "decisions": [],
        "continuation_reviews": [{
            "task_id": task["task_id"],
            "outcome": "confirmed_not_a_footnote",
            "reason": "The page image shows an ordinary body line, not a page note.",
            "evidence_pages": [1, 2],
        }],
    }), encoding="utf-8")
    assert validate_footnote_decisions(tmp_path)["valid"] is True
    result = apply_footnote_normalization(tmp_path)
    assert result["valid"] is True
    assert (tmp_path / "footnote_normalized" / "chapter_1.md").read_text(encoding="utf-8") == (
        tmp_path / "ocr_markdown" / "chapter_1.md"
    ).read_text(encoding="utf-8")


def test_continuation_resolution_requires_explicit_outcome_both_images_and_current_hash(tmp_path, monkeypatch):
    image = tmp_path / "page.png"
    image.write_bytes(b"synthetic image bytes")
    from pdf2epub.workflow_contracts import sha256_file
    import pdf2epub.ocr_correction
    monkeypatch.setattr(pdf2epub.ocr_correction, "review_images_are_current", lambda _path: True)
    manifest_path = tmp_path / "ocr_review_images.json"
    manifest_path.write_text(json.dumps({"source_sha256": "source-hash"}), encoding="utf-8")

    task = {
        "task_id": "continuation-gap-001-004",
        "pages": [1, 2],
        "source_note": {"page": 1, "key": "36"},
        "visual_files": {
            "1": {"path": "page.png", "sha256": sha256_file(image)},
            "2": {"path": "page.png", "sha256": sha256_file(image)},
        },
        "visual_image_manifest_sha256": sha256_file(manifest_path),
        "source_pdf_sha256": "source-hash",
    }
    base = {
        "task_id": task["task_id"],
        "outcome": "recovered_continuation",
        "key": "36",
        "text": "nehm continued text",
        "reason": "The next page image visibly continues the broken word.",
        "evidence_pages": [1, 2],
    }
    normalized, errors = validate_continuation_review_decisions(tmp_path, [task], [base])
    assert errors == []
    assert normalized[0]["text"] == "nehm continued text"
    page_source = tmp_path / "pages" / "page_002.md"
    page_source.parent.mkdir(exist_ok=True)
    page_source.write_text("Body text: nehm continued text", encoding="utf-8")
    _, errors = validate_continuation_review_decisions(tmp_path, [task], [base])
    assert any("already exists in primary OCR Markdown" in error for error in errors)
    page_source.unlink()

    image.write_bytes(b"changed image")
    _, errors = validate_continuation_review_decisions(tmp_path, [task], [base])
    assert any("missing or changed" in error for error in errors)


def test_malformed_continuation_queue_task_fails_closed(tmp_path):
    normalized, errors = validate_continuation_review_decisions(
        tmp_path,
        [{"pages": [1, 2], "source_note": {"page": 1, "key": "36"}}],
        [],
    )
    assert normalized == []
    assert errors == ["continuation review task 0 is missing task_id"]


def test_final_page_task_allows_only_image_backed_exception(tmp_path, monkeypatch):
    image = tmp_path / "page.png"
    image.write_bytes(b"last page")
    from pdf2epub.workflow_contracts import sha256_file
    import pdf2epub.ocr_correction
    monkeypatch.setattr(pdf2epub.ocr_correction, "review_images_are_current", lambda _path: True)
    manifest_path = tmp_path / "ocr_review_images.json"
    manifest_path.write_text(json.dumps({"source_sha256": "source-hash"}), encoding="utf-8")
    task = {
        "task_id": "continuation-gap-099-004",
        "pages": [99],
        "terminal_page": True,
        "source_note": {"page": 99, "key": "36"},
        "visual_files": {"99": {"path": "page.png", "sha256": sha256_file(image)}},
        "visual_image_manifest_sha256": sha256_file(manifest_path),
        "source_pdf_sha256": "source-hash",
    }
    exception = {
        "task_id": task["task_id"],
        "outcome": "truncated_source_exception",
        "reason": "The final page image confirms the printed note ends here.",
        "evidence_pages": [99],
    }
    _, errors = validate_continuation_review_decisions(tmp_path, [task], [exception])
    assert errors == []

    recovered = {**exception, "outcome": "recovered_continuation", "key": "36", "text": "suffix"}
    _, errors = validate_continuation_review_decisions(tmp_path, [task], [recovered])
    assert any("final physical page" in error for error in errors)


def test_real_rendered_images_are_bound_to_current_synthetic_pdf(tmp_path):
    import pymupdf as fitz
    from pdf2epub.refine.footnote_visual import ensure_visual_review_images
    from pdf2epub.workflow_contracts import sha256_file

    pdf_path = tmp_path / "input.pdf"
    document = fitz.open()
    document.new_page(width=300, height=400)
    document.new_page(width=300, height=400)
    document.save(pdf_path)
    document.close()
    visual = ensure_visual_review_images(tmp_path, [1, 2], config={})
    assert visual["available"] is True
    manifest_path = tmp_path / "ocr_review_images.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    task = {
        "task_id": "continuation-gap-001-004",
        "pages": [1, 2],
        "source_note": {"page": 1, "key": "36"},
        "visual_files": {
            page: {"path": path, "sha256": sha256_file(tmp_path / path)}
            for page, path in visual["files"].items()
        },
        "visual_image_manifest_sha256": sha256_file(manifest_path),
        "source_pdf_sha256": manifest["source_sha256"],
    }
    decision = {
        "task_id": task["task_id"],
        "outcome": "truncated_source_exception",
        "reason": "The two rendered images show the note ends at the page break.",
        "evidence_pages": [1, 2],
    }
    _, errors = validate_continuation_review_decisions(tmp_path, [task], [decision])
    assert errors == []

    changed = fitz.open(pdf_path)
    changed[1].insert_text((20, 30), "changed source")
    changed_path = tmp_path / "changed.pdf"
    changed.save(changed_path)
    changed.close()
    changed_path.replace(pdf_path)
    _, errors = validate_continuation_review_decisions(tmp_path, [task], [decision])
    assert any("stale for the current source PDF" in error for error in errors)


def test_disabled_page_footnote_mode_warns_only_for_conflicting_toc(tmp_path):
    config = {"footnotes": {"mode": "disabled"}}
    toc = tmp_path / "toc_tree.json"
    toc.write_text(json.dumps({"footnote_mode": "footnotes"}), encoding="utf-8")
    warnings = footnote_mode_warnings(tmp_path, config)
    assert warnings[0]["code"] == "page_footnote_linking_disabled_toc_footnotes"
    assert "Automatic page-footnote extraction/link creation is disabled" in warnings[0]["message"]

    toc.write_text(json.dumps({"footnote_mode": "endnotes_only"}), encoding="utf-8")
    warnings = footnote_mode_warnings(tmp_path, config)
    assert warnings[0]["code"] == "page_footnote_linking_disabled_toc_endnotes_only"
    assert "may still be built" in warnings[0]["message"]

    assert footnote_mode_warnings(tmp_path, {"footnotes": {"mode": "auto"}}) == []


def test_disabled_warning_persists_through_prepare_validate_and_apply(tmp_path):
    from pdf2epub.refine.footnote_validation import validate_footnote_decisions
    from pdf2epub.refine.footnote_apply import apply_footnote_normalization

    _write_ocr_page(tmp_path, 1, [])
    (tmp_path / "toc_tree.json").write_text(json.dumps({"footnote_mode": "footnotes"}), encoding="utf-8")
    source = tmp_path / "ocr_markdown"
    source.mkdir()
    (source / "chapter_1.md").write_text("Body [^1].\n", encoding="utf-8")
    (source / "tree_progress.json").write_text(json.dumps({"units": [{
        "unit_id": "chapter_1", "file": "chapter_1.md", "page_range": [1, 1],
    }]}), encoding="utf-8")
    config = {"footnotes": {"mode": "disabled"}}
    paths = prepare_footnote_subagent(tmp_path, book_title="Disabled mode", config=config)
    report = json.loads(paths["report"].read_text(encoding="utf-8"))
    manifest = json.loads(paths["manifest"].read_text(encoding="utf-8"))
    assert report["warnings"][0]["code"] == "page_footnote_linking_disabled_toc_footnotes"
    assert manifest["warnings"] == report["warnings"]
    assert "Automatic page-footnote extraction/link creation is disabled" in paths["prompt"].read_text(encoding="utf-8")

    validation = validate_footnote_decisions(tmp_path, config=config)
    assert validation["valid"] is True
    assert validation["warnings"] == report["warnings"]
    applied = apply_footnote_normalization(tmp_path, config=config)
    assert applied["valid"] is True, applied.get("errors")
    assert applied["warnings"] == report["warnings"]


def test_readiness_report_carries_disabled_warning_without_turning_it_into_error(tmp_path, monkeypatch):
    from pdf2epub.commands import markdown_readiness

    config = {
        "title": "Synthetic Readiness",
        "pipeline": "epub_conversion",
        "footnotes": {"mode": "disabled"},
        "ocr": {"secondary": {"enabled": False}},
    }
    (tmp_path / "toc_tree.json").write_text(json.dumps({"footnote_mode": "footnotes"}), encoding="utf-8")
    monkeypatch.setattr(markdown_readiness, "load_config", lambda *_: config)
    monkeypatch.setattr(markdown_readiness, "book_output_dir", lambda _title: tmp_path)
    report = markdown_readiness.run_readiness_check("synthetic.yaml", stage="ocr")
    assert report["warnings"][0]["code"] == "page_footnote_linking_disabled_toc_footnotes"
    assert all("page_footnote_linking_disabled_toc_footnotes" not in error for error in report["errors"])
    assert json.loads((tmp_path / "readiness.json").read_text(encoding="utf-8"))["warnings"] == report["warnings"]
