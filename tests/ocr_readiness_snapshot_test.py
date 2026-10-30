import hashlib
import json
from pathlib import Path

import pytest

from pdf2epub import ocr_anomalies
from pdf2epub.commands import markdown_readiness
from pdf2epub.commands.sources import _require_reviewed_ocr_anomalies
from pdf2epub.ocr_anomalies import scan_ocr_page_evidence
from pdf2epub.ocr_correction import select_refinement_pages
from pdf2epub.ocr_progress import new_progress
from pdf2epub.pipeline_policy import PipelinePolicy
from pdf2epub.refine.footnote_apply import apply_footnote_normalization
from pdf2epub.refine.footnote_prepare import prepare_footnote_candidates
from pdf2epub.refine.footnote_status import footnote_normalization_status
from pdf2epub.refine.footnote_validation import validate_footnote_decisions
from pdf2epub.utils.fingerprints import pages_fingerprint
from pdf2epub.workflow_contracts import MARKDOWN_VALIDATION_SCHEMA_VERSION


def _configure_readiness(monkeypatch, output_dir: Path, config: dict) -> None:
    monkeypatch.setattr(markdown_readiness, "load_config", lambda *_args: config)
    monkeypatch.setattr(
        markdown_readiness, "book_output_dir", lambda _title: output_dir
    )


def _single_ocr_config() -> dict:
    return {
        "title": "Snapshot Book",
        "pipeline": "epub_conversion",
        "footnotes": {"mode": "auto"},
        "ocr": {"secondary": {"enabled": False}},
    }


def _write_current_translation_readiness_fixture(output_dir: Path) -> dict:
    """Build a real single-OCR, footnote, and polish receipt chain for readiness."""
    config = _single_ocr_config()
    pages_dir = output_dir / "pages"
    markdown_dir = output_dir / "ocr_markdown"
    pages_dir.mkdir(parents=True)
    markdown_dir.mkdir()
    (pages_dir / "page_001.md").write_text(
        "The page should be read or transcribed.", encoding="utf-8"
    )
    (pages_dir / "page_001.ocr.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "page_number": 1,
                "backend": "chandra",
                "page_box": [0, 0, 1000, 1000],
                "blocks": [],
            }
        ),
        encoding="utf-8",
    )
    progress = new_progress(
        source_sha256="synthetic-pdf", total_pages=1, backend="chandra"
    )
    progress["pages_processed"] = [1]
    progress["missing_pages"] = []
    (pages_dir / "ocr_progress.json").write_text(
        json.dumps(progress), encoding="utf-8"
    )

    toc = {
        "chapters": [
            {
                "title": "Chapter One",
                "level": 1,
                "start_page": 1,
                "end_page": 1,
                "children": [],
            }
        ]
    }
    toc_path = output_dir / "toc_tree.json"
    toc_path.write_text(json.dumps(toc), encoding="utf-8")
    (markdown_dir / "chapter_001.md").write_text(
        "Current refined source.", encoding="utf-8"
    )
    tree_progress = {
        "units": [
            {
                "unit_id": "chapter_001",
                "file": "chapter_001.md",
                "page_range": [1, 1],
                "type": "",
            }
        ],
        "fingerprint": {
            "toc_sha256": hashlib.sha256(toc_path.read_bytes()).hexdigest(),
            "pages_sha256": pages_fingerprint(pages_dir),
        },
    }
    (markdown_dir / "tree_progress.json").write_text(
        json.dumps(tree_progress), encoding="utf-8"
    )

    # Use the actual footnote prepare/validate/apply paths to create a current
    # no-review checkpoint, then build a real polished-source validation receipt.
    candidates = prepare_footnote_candidates(output_dir, config=config)
    assert candidates["ocr_evidence_mode"] == "single_ocr"
    assert validate_footnote_decisions(output_dir, config=config)["valid"] is True
    assert apply_footnote_normalization(output_dir, config=config)["valid"] is True
    assert footnote_normalization_status(output_dir, config=config)["current"] is True

    polished_dir = output_dir / "polished_markdown" / "validated"
    polished_dir.mkdir(parents=True)
    polished_path = polished_dir / "chapter_001.md"
    polished_path.write_text("Current polished source.", encoding="utf-8")
    normalized_path = output_dir / "footnote_normalized" / "chapter_001.md"
    polished_receipt = {
        "schema_version": MARKDOWN_VALIDATION_SCHEMA_VERSION,
        "task": "polish",
        "scope": "full",
        "all_passed": True,
        "source_sha256": {
            normalized_path.name: hashlib.sha256(normalized_path.read_bytes()).hexdigest()
        },
        "target_sha256": {
            polished_path.name: hashlib.sha256(polished_path.read_bytes()).hexdigest()
        },
    }
    (output_dir / "polish_validation.json").write_text(
        json.dumps(polished_receipt), encoding="utf-8"
    )
    return config


def test_readiness_runs_one_anomaly_scan_and_reads_each_raw_page_once(
    tmp_path, monkeypatch
):
    output_dir = tmp_path / "book"
    pages_dir = output_dir / "pages"
    pages_dir.mkdir(parents=True)
    config = _single_ocr_config()
    _configure_readiness(monkeypatch, output_dir, config)
    for index in range(1, 201):
        (pages_dir / f"page_{index:03d}.md").write_text(
            f"An ordinary synthetic paragraph for page {index}.\n", encoding="utf-8"
        )

    scan_calls = 0
    original_scan = ocr_anomalies.scan_ocr_page_evidence

    def counted_scan(*args, **kwargs):
        nonlocal scan_calls
        scan_calls += 1
        return original_scan(*args, **kwargs)

    monkeypatch.setattr(
        ocr_anomalies, "scan_ocr_page_evidence", counted_scan
    )
    opened_pages = []
    original_open = Path.open

    def counted_open(path, *args, **kwargs):
        if path.parent == pages_dir and path.name.startswith("page_") and path.suffix == ".md":
            opened_pages.append(path.name)
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", counted_open)

    report = markdown_readiness.run_readiness_check("synthetic.yaml", stage="translate")

    assert report["ready"] is False  # Later-stage fixture artifacts are intentionally absent.
    # Without a TOC/tree checkpoint, this measures anomaly-scan reads only;
    # independent refinement fingerprint reads are intentionally outside scope.
    assert scan_calls == 1
    assert len(opened_pages) == 200
    assert len(set(opened_pages)) == 200


def test_next_readiness_invocation_detects_same_size_page_edit(tmp_path, monkeypatch):
    output_dir = tmp_path / "book"
    pages_dir = output_dir / "pages"
    pages_dir.mkdir(parents=True)
    config = _single_ocr_config()
    _configure_readiness(monkeypatch, output_dir, config)
    page = pages_dir / "page_001.md"
    ordinary = "The page should be read or transcribed."
    anomaly = "The page cannot be read or transcribed."
    assert len(ordinary.encode("utf-8")) == len(anomaly.encode("utf-8"))
    page.write_text(ordinary, encoding="utf-8")

    first = markdown_readiness.run_readiness_check("synthetic.yaml", stage="translate")
    assert first["checks"]["ocr_anomalies"]["ready"] is True
    page.write_text(anomaly, encoding="utf-8")

    second = markdown_readiness.run_readiness_check("synthetic.yaml", stage="translate")
    assert second["checks"]["ocr_anomalies"]["ready"] is False
    assert "page_001.md" in second["checks"]["ocr_anomalies"]["detail"]


def test_scan_errors_remain_blocking_in_readiness(tmp_path, monkeypatch):
    output_dir = tmp_path / "book"
    pages_dir = output_dir / "pages"
    pages_dir.mkdir(parents=True)
    (pages_dir / "page_001.md").write_bytes(b"\xff")
    _configure_readiness(monkeypatch, output_dir, _single_ocr_config())

    report = markdown_readiness.run_readiness_check("synthetic.yaml", stage="translate")

    assert report["checks"]["ocr_anomalies"]["ready"] is False
    assert "could not all be scanned" in report["checks"]["ocr_anomalies"]["detail"]
    with pytest.raises(ValueError, match="could not read every raw page"):
        _require_reviewed_ocr_anomalies(output_dir, _single_ocr_config())


def test_legacy_native_source_bypasses_visual_anomaly_review(tmp_path, monkeypatch):
    pages_dir = tmp_path / "pages"
    pages_dir.mkdir()
    (pages_dir / "page_001.md").write_text("Legacy native page.", encoding="utf-8")
    (pages_dir / "ocr_progress.json").write_text(
        json.dumps({"mode": "native_text"}), encoding="utf-8"
    )

    def unexpected_scan(*_args, **_kwargs):
        raise AssertionError("legacy native evidence should bypass anomaly scans")

    monkeypatch.setattr("pdf2epub.ocr_anomalies.scan_ocr_pages", unexpected_scan)
    selected, source_kind = select_refinement_pages(tmp_path, require_correction=True)
    _require_reviewed_ocr_anomalies(tmp_path, _single_ocr_config())
    policy = PipelinePolicy.from_config(_single_ocr_config())

    assert selected == pages_dir
    assert source_kind == "native_text"
    assert policy.requires_visual_ocr_review(tmp_path) is False


def test_dual_ocr_keeps_consensus_correction_semantics_with_snapshot(
    tmp_path, monkeypatch
):
    pages_dir = tmp_path / "pages"
    pages_dir.mkdir()
    (pages_dir / "page_001.md").write_text(
        "The page cannot be read or transcribed.", encoding="utf-8"
    )
    evidence = scan_ocr_page_evidence(tmp_path, persist=False)
    config = {
        "pipeline": "epub_conversion",
        "ocr": {"secondary": {"enabled": True, "backend": "vision"}},
    }
    monkeypatch.setattr(
        "pdf2epub.ocr_correction.ocr_correction_is_current",
        lambda *_args, **_kwargs: True,
    )
    monkeypatch.setattr(
        "pdf2epub.ocr_correction.corrected_page_dir",
        lambda _output: tmp_path / "corrected",
    )

    selected, source_kind = select_refinement_pages(
        tmp_path, require_correction=True, config=config, ocr_evidence=evidence
    )

    assert evidence.report["review_required_files"] == ["page_001.md"]
    assert PipelinePolicy.from_config(config).requires_visual_ocr_review(tmp_path, evidence)
    assert selected == tmp_path / "corrected"
    assert source_kind == "ocr_corrected"


def test_supported_translate_readiness_passes_then_blocks_same_size_page_edit(
    tmp_path, monkeypatch
):
    output_dir = tmp_path / "book"
    config = _write_current_translation_readiness_fixture(output_dir)
    _configure_readiness(monkeypatch, output_dir, config)

    report = markdown_readiness.run_readiness_check("synthetic.yaml", stage="translate")

    assert report["ready"] is True
    assert report["checks"]["ocr_pages"]["ready"] is True
    assert report["checks"]["ocr_anomalies"]["ready"] is True
    assert report["checks"]["footnote_normalization"]["ready"] is True
    assert report["checks"]["refine_local"]["ready"] is True
    assert report["checks"]["polish_required"]["ready"] is True
    assert report["source_stage"] == "polished"

    page = output_dir / "pages" / "page_001.md"
    anomaly = "The page cannot be read or transcribed."
    assert len(anomaly.encode("utf-8")) == len(page.read_bytes())
    page.write_text(anomaly, encoding="utf-8")

    stale_report = markdown_readiness.run_readiness_check(
        "synthetic.yaml", stage="translate"
    )

    assert stale_report["ready"] is False
    assert stale_report["checks"]["ocr_anomalies"]["ready"] is False
    assert stale_report["checks"]["refine_local"]["ready"] is False


def test_evidence_is_bound_to_its_output_directory(tmp_path):
    source = tmp_path / "source"
    other = tmp_path / "other"
    evidence = scan_ocr_page_evidence(source, persist=False)
    (other / "pages").mkdir(parents=True)
    (other / "pages" / "page_001.md").write_text("Page", encoding="utf-8")

    with pytest.raises(ValueError, match="different output directory"):
        _require_reviewed_ocr_anomalies(other, _single_ocr_config(), evidence)
