import hashlib
import json
from pathlib import Path

from pdf2epub.commands.diagnostics import build_doctor, build_status
from pdf2epub.ocr_progress import new_progress


def _config(tmp_path: Path, *, pipeline: str | None = None, backend: str = "chandra") -> Path:
    input_dir = tmp_path / "input"
    input_dir.mkdir(exist_ok=True)
    input_pdf = input_dir / "book.pdf"
    input_pdf.write_bytes(b"test pdf placeholder")
    lines = [
        "title: Diagnostics Book",
        "input_pdf: input/book.pdf",
        "ocr:",
        f"  backend: {backend}",
        "  secondary:",
        "    enabled: false",
    ]
    if pipeline:
        lines.append(f"pipeline: {pipeline}")
    config_path = tmp_path / "config.yaml"
    config_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return config_path


def test_status_empty_output_points_to_ocr(tmp_path: Path, monkeypatch):
    config_path = _config(tmp_path)
    monkeypatch.chdir(tmp_path)

    report = build_status(config_path)

    assert report["input_type"] == "pdf"
    assert report["stages"][0]["name"] == "ocr-pages"
    assert report["stages"][0]["status"] == "pending"
    assert report["next_command"].endswith("ocr-pages --resume")


def test_status_reports_complete_ocr_but_missing_toc(tmp_path: Path, monkeypatch):
    config_path = _config(tmp_path)
    monkeypatch.chdir(tmp_path)
    output_dir = tmp_path / "output" / "Diagnostics Book"
    pages_dir = output_dir / "pages"
    pages_dir.mkdir(parents=True)
    source_hash = hashlib.sha256((tmp_path / "input" / "book.pdf").read_bytes()).hexdigest()
    (output_dir / "pdf_text_probe.json").write_text(
        json.dumps({"page_count": 1, "source_sha256": source_hash}), encoding="utf-8"
    )
    progress = new_progress(source_sha256=source_hash, total_pages=1, backend="chandra")
    progress["pages_processed"] = [1]
    progress["missing_pages"] = []
    (pages_dir / "ocr_progress.json").write_text(json.dumps(progress), encoding="utf-8")
    (pages_dir / "page_001.md").write_text("page", encoding="utf-8")

    report = build_status(config_path)
    stages = {item["name"]: item for item in report["stages"]}

    assert stages["ocr-pages"]["status"] == "passed"
    assert stages["refine-prepare"]["status"] == "pending"
    assert report["next_command"].endswith("refine-prepare")


def test_conversion_status_skips_translation_stages(tmp_path: Path, monkeypatch):
    config_path = _config(tmp_path, pipeline="epub_conversion")
    monkeypatch.chdir(tmp_path)

    report = build_status(config_path)
    stages = {item["name"]: item for item in report["stages"]}

    assert stages["extract-entities"]["status"] == "skipped"
    assert stages["translate-toc"]["status"] == "skipped"
    assert stages["translate"]["status"] == "skipped"


def test_doctor_blocks_retired_mistral_backend(tmp_path: Path, monkeypatch):
    config_path = _config(tmp_path, backend="mistral")
    monkeypatch.chdir(tmp_path)

    report = build_doctor(config_path)
    checks = {item["name"]: item for item in report["checks"]}

    assert report["ok"] is False
    assert checks["ocr.primary_backend"]["status"] == "blocked"
    assert checks["ocr.configuration"]["status"] == "blocked"


def test_doctor_blocks_retired_paddle_secondary_backend(tmp_path: Path, monkeypatch):
    config_path = _config(tmp_path)
    config_path.write_text(
        config_path.read_text(encoding="utf-8").replace(
            "    enabled: false", "    enabled: true\n    backend: paddle"
        ),
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)

    report = build_doctor(config_path)
    checks = {item["name"]: item for item in report["checks"]}

    assert report["ok"] is False
    assert checks["ocr.secondary_backend"]["status"] == "blocked"
    assert checks["ocr.configuration"]["status"] == "blocked"
    assert checks["dependency.paddle"]["status"] == "blocked"


def test_status_detects_stale_ocr_source_hash(tmp_path: Path, monkeypatch):
    config_path = _config(tmp_path)
    monkeypatch.chdir(tmp_path)
    output_dir = tmp_path / "output" / "Diagnostics Book"
    pages_dir = output_dir / "pages"
    pages_dir.mkdir(parents=True)
    progress = new_progress(source_sha256="old", total_pages=1, backend="chandra")
    progress["pages_processed"] = [1]
    progress["missing_pages"] = []
    (pages_dir / "ocr_progress.json").write_text(json.dumps(progress), encoding="utf-8")
    (pages_dir / "page_001.md").write_text("page", encoding="utf-8")

    report = build_status(config_path)

    assert report["stages"][0]["status"] == "pending"
    assert "match" in report["stages"][0]["detail"]
