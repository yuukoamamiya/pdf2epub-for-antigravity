import json
from pathlib import Path

import fitz
import pytest

from pdf2epub.ocr.artifacts import OCRPageResult
from pdf2epub.ocr_consensus import (
    compare_ocr_texts,
    compare_ocr_layouts,
    common_ocr_risk_reasons,
    consensus_is_current,
    load_consensus_manifest,
    rebuild_ocr_consensus,
    secondary_backend_name,
    secondary_ocr_enabled,
    write_page_consensus,
)
from pdf2epub.ocr_pages import preflight_secondary_ocr, run_secondary_ocr_consensus


def _make_pdf(path: Path, page_count: int = 2) -> None:
    document = fitz.open()
    for _ in range(page_count):
        document.new_page()
    document.save(path)
    document.close()


def test_consensus_ignores_markdown_layout_only_differences():
    config = {"ocr": {"consensus": {}}}

    report = compare_ocr_texts("# Heading\nA *short* line", "Heading\nA short line", config)

    assert report["status"] == "agree"
    assert report["reasons"] == []


def test_consensus_ignores_backend_specific_footnote_delimiters():
    config = {"ocr": {"consensus": {}}}

    report = compare_ocr_texts(
        "Body\n\n[^1]: A note.",
        "Body\n1 A note.",
        config,
    )

    assert report["status"] == "agree"
    assert report["reasons"] == []


def test_consensus_flags_missing_lines_and_numeric_changes():
    config = {"ocr": {"consensus": {}}}

    report = compare_ocr_texts(
        "Chapter 12\nThe second line is present.",
        "Chapter 13",
        config,
    )

    assert report["status"] == "review_required"
    assert "numeric markers differ" in report["reasons"]
    assert "OCR text differs above the configured threshold" in report["reasons"]


def test_consensus_flags_one_missing_line_even_when_text_is_long():
    config = {"ocr": {"consensus": {}}}
    primary = "\n".join(["shared line"] * 40 + ["important final line"])
    secondary = "\n".join(["shared line"] * 40)

    report = compare_ocr_texts(primary, secondary, config)

    assert report["status"] == "review_required"
    assert "non-empty line counts differ" in report["reasons"]


def test_chandra_paddle_ignores_line_and_global_numeric_layout_differences():
    config = {"ocr": {"consensus": {}}}
    body = " ".join(["shared prose"] * 120)

    report = compare_ocr_texts(
        f"{body} 2024\n{body}",
        f"{body}\n{body} 2025",
        config,
        primary_backend="chandra",
        secondary_backend="paddle",
    )

    assert "non-empty line counts differ" not in report["reasons"]
    assert "numeric markers differ" not in report["reasons"]
    assert report["diagnostics"]["heterogeneous_layout_pair"] is True


def test_layout_disagreement_is_evidence_not_page_text_block(tmp_path: Path):
    record = write_page_consensus(
        tmp_path,
        source_name="page_001.md",
        primary_text="Body text",
        secondary_text="Body text",
        primary_backend="chandra",
        secondary_backend="paddle",
        config={"ocr": {"consensus": {}}},
        primary_layout={
            "blocks": [
                {"label": "Footnote", "bbox": [0, 700, 1000, 800], "text": "1 note"}
            ]
        },
        secondary_layout={"blocks": [{"label": "Text", "bbox": [0, 700, 1000, 800], "text": "1 note"}]},
    )

    assert record["layout_review"] is True
    assert record["comparison"]["status"] == "agree"
    assert record["action"] == "auto_accept"


def test_layout_consensus_flags_single_engine_footnote_label():
    primary = {
        "blocks": [
            {"label": "Text", "html": "<p>body</p>"},
            {"label": "Footnote", "html": "<p>1 note</p>"},
        ]
    }
    secondary = {
        "blocks": [
            {"label": "Text", "text": "body"},
            {"label": "Text", "text": "body at bottom"},
        ]
    }

    report = compare_ocr_layouts(primary, secondary)

    assert report["status"] == "review_required"
    assert "footnote label presence differs" in report["reasons"]


def test_layout_consensus_flags_different_footnote_vertical_coverage():
    primary = {
        "blocks": [
            {"label": "Footnote", "bbox": [100, 650, 900, 750], "text": "1 note"},
        ]
    }
    secondary = {
        "blocks": [
            {"label": "Footnote", "bbox": [100, 800, 900, 900], "text": "1 note"},
        ]
    }

    report = compare_ocr_layouts(primary, secondary)

    assert report["status"] == "review_required"
    assert "footnote vertical coverage differs" in report["reasons"]


def test_common_ocr_risk_flags_anomalously_sparse_internal_page():
    page_texts = {
        "page_001.md": {"primary": "word\n" * 500, "secondary": "word\n" * 500},
        "page_002.md": {"primary": "word\n" * 100, "secondary": "word\n" * 100},
        "page_003.md": {"primary": "word\n" * 500, "secondary": "word\n" * 500},
    }

    risks = common_ocr_risk_reasons(
        page_texts,
        {"ocr": {"consensus": {"sample_every": 0}}},
    )

    assert "page_002.md" in risks
    assert any("density" in reason for reason in risks["page_002.md"])


def test_secondary_ocr_switch_controls_consensus_and_keeps_legacy_alias():
    disabled = {
        "ocr": {"secondary": {"enabled": False, "backend": "paddle"}}
    }
    enabled = {
        "ocr": {"secondary": {"enabled": True, "backend": "paddle"}}
    }
    missing_backend = {"ocr": {"secondary": {"enabled": True}}}
    legacy = {"ocr": {"secondary_backend": "paddle"}}

    assert secondary_ocr_enabled(disabled) is False
    assert secondary_backend_name(disabled) is None
    assert secondary_ocr_enabled(enabled) is True
    assert secondary_backend_name(enabled) == "paddle"
    assert secondary_ocr_enabled(missing_backend) is True
    assert secondary_backend_name(missing_backend) is None
    assert secondary_ocr_enabled(legacy) is True
    assert secondary_backend_name(legacy) == "paddle"


def test_secondary_preflight_blocks_paddle_cpu_before_primary_pages(tmp_path: Path):
    config = {
        "ocr": {
            "secondary": {"enabled": True, "backend": "paddle"},
            "backends": {"paddle": {"device": "cpu"}},
        }
    }

    with pytest.raises(RuntimeError, match="GPU-only"):
        preflight_secondary_ocr(tmp_path, 674, config)

    report = json.loads(
        (tmp_path / "ocr_secondary_preflight.json").read_text(encoding="utf-8")
    )
    assert report["status"] == "blocked"
    assert report["reason"] == "cpu_device_not_allowed"


def test_secondary_preflight_requires_explicit_slow_run_confirmation(tmp_path: Path):
    config = {
        "ocr": {
            "secondary": {
                "enabled": True,
                "backend": "paddle",
                "performance": {
                    "estimated_seconds_per_page": 20,
                    "max_estimated_seconds": 100,
                },
            },
            "backends": {"paddle": {"device": "gpu:0"}},
        }
    }

    with pytest.raises(RuntimeError, match="requires confirmation"):
        preflight_secondary_ocr(tmp_path, 10, config)

    report = preflight_secondary_ocr(tmp_path, 10, config, allow_slow=True)

    assert report["status"] == "allowed_with_confirmation"
    assert report["estimated_seconds"] == 200.0
    assert report["estimated_hours"] == round(200 / 3600, 3)


def test_pagewise_secondary_consensus_scopes_visual_review(monkeypatch, tmp_path: Path):
    pdf_path = tmp_path / "input.pdf"
    _make_pdf(pdf_path)
    output_dir = tmp_path / "output"
    pages_dir = output_dir / "pages"
    pages_dir.mkdir(parents=True)
    (pages_dir / "page_001.md").write_text("same text\n", encoding="utf-8")
    (pages_dir / "page_002.md").write_text("Chapter 12\nline two\n", encoding="utf-8")

    def fake_secondary(*args, **kwargs):
        page_number = kwargs["page_number"]
        text = "same text\n" if page_number == 1 else "Chapter 13\n"
        return OCRPageResult(markdown=text, backend="fake")

    monkeypatch.setattr("pdf2epub.ocr_pages.ocr_pdf_page", fake_secondary)
    config = {
        "ocr": {
            "backend": "chandra",
            "secondary_backend": "fake",
            "backends": {"fake": {"max_workers": 1}},
            "consensus": {},
        }
    }

    summary = run_secondary_ocr_consensus(
        ocr_pdf=pdf_path,
        output_dir=output_dir,
        total_pages=2,
        primary_backend="chandra",
        config=config,
        max_workers=1,
        resume=False,
    )

    assert summary["failed_pages"] == []
    assert summary["review_pages"] == ["page_002.md"]
    manifest = load_consensus_manifest(output_dir)
    assert manifest["complete"] is True
    assert consensus_is_current(
        output_dir,
        config,
        primary_backend="chandra",
    )
    record = json.loads(
        (output_dir / "ocr_consensus" / "page_002.json").read_text(encoding="utf-8")
    )
    assert record["action"] == "visual_review"


def test_rebuild_consensus_reuses_existing_page_artifacts(tmp_path: Path):
    output_dir = tmp_path / "output"
    pages_dir = output_dir / "pages"
    secondary_dir = output_dir / "ocr_secondary"
    pages_dir.mkdir(parents=True)
    secondary_dir.mkdir()
    for page in (1, 2):
        name = f"page_{page:03d}.md"
        (pages_dir / name).write_text("same text\n", encoding="utf-8")
        (secondary_dir / name).write_text("same text\n", encoding="utf-8")
        sidecar = {
            "page_number": page,
            "blocks": [{"label": "Text", "text": "same text", "bbox": [0, 0, 1000, 1000]}],
        }
        (pages_dir / f"page_{page:03d}.ocr.json").write_text(
            json.dumps(sidecar), encoding="utf-8"
        )
        (secondary_dir / f"page_{page:03d}.ocr.json").write_text(
            json.dumps(sidecar), encoding="utf-8"
        )
    (pages_dir / "ocr_progress.json").write_text(
        json.dumps(
            {
                "total_pages": 2,
                "backend": "chandra",
                "source_sha256": "source-sha",
            }
        ),
        encoding="utf-8",
    )
    config = {
        "ocr": {
            "secondary": {"enabled": True, "backend": "paddle"},
            "consensus": {"common_miss": {"sample_every": 0}},
        }
    }

    summary = rebuild_ocr_consensus(output_dir, config)

    assert summary["failed_pages"] == []
    assert summary["review_pages"] == []
    manifest = load_consensus_manifest(output_dir)
    assert manifest["schema_version"] == 4
    assert manifest["rebuild_mode"] == "offline_existing_artifacts"


def test_paddle_preflight_writes_diagnostics_before_workers(monkeypatch, tmp_path: Path):
    pdf_path = tmp_path / "input.pdf"
    _make_pdf(pdf_path, page_count=1)
    pages_dir = tmp_path / "pages"
    pages_dir.mkdir()
    (pages_dir / "page_001.md").write_text("text", encoding="utf-8")

    monkeypatch.setattr(
        "pdf2epub.ocr.backends.paddle.preflight",
        lambda _config: (
            {
                "status": "failed",
                "error_type": "RuntimeError",
                "error": "OneDNN operator is unavailable",
                "paddlepaddle_distribution": "3.0.0",
                "paddleocr_distribution": "3.0.0",
                "protobuf_distribution": "7.0.0",
            },
            None,
        ),
    )

    config = {
        "ocr": {
            "backend": "chandra",
            "secondary": {"enabled": True, "backend": "paddle"},
        }
    }
    with pytest.raises(RuntimeError, match="preflight failed"):
        run_secondary_ocr_consensus(
            ocr_pdf=pdf_path,
            output_dir=tmp_path,
            total_pages=1,
            primary_backend="chandra",
            config=config,
            max_workers=1,
            resume=False,
        )

    diagnostics = json.loads(
        (tmp_path / "ocr_secondary_preflight.json").read_text(encoding="utf-8")
    )
    assert diagnostics["error_type"] == "RuntimeError"
    assert diagnostics["protobuf_distribution"] == "7.0.0"


def test_paddle_worker_is_closed_after_secondary_batch(monkeypatch, tmp_path: Path):
    pdf_path = tmp_path / "input.pdf"
    _make_pdf(pdf_path, page_count=1)
    output_dir = tmp_path / "output"
    pages_dir = output_dir / "pages"
    pages_dir.mkdir(parents=True)
    (pages_dir / "page_001.md").write_text("primary text\n", encoding="utf-8")

    class FakeWorker:
        def __init__(self):
            self.closed = False

        def close(self):
            self.closed = True

    worker = FakeWorker()
    monkeypatch.setattr(
        "pdf2epub.ocr.backends.paddle.preflight",
        lambda _config: (
            {
                "status": "ready",
                "worker_mode": "subprocess",
                "device_actual": "gpu:0",
            },
            worker,
        ),
    )
    monkeypatch.setattr(
        "pdf2epub.ocr_pages.ocr_pdf_page",
        lambda *args, **kwargs: OCRPageResult(
            markdown="secondary text\n", backend="paddle"
        ),
    )

    summary = run_secondary_ocr_consensus(
        ocr_pdf=pdf_path,
        output_dir=output_dir,
        total_pages=1,
        primary_backend="chandra",
        config={
            "ocr": {
                "secondary": {"enabled": True, "backend": "paddle"},
                "backends": {"paddle": {"max_workers": 4}},
                "consensus": {},
            }
        },
        max_workers=1,
        resume=False,
    )

    assert summary["failed_pages"] == []
    assert worker.closed is True
