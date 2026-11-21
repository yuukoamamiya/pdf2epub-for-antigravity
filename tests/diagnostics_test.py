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


def test_unknown_pipeline_is_blocked_by_doctor_and_status(tmp_path: Path, monkeypatch):
    config_path = _config(tmp_path, pipeline="mystery")
    monkeypatch.chdir(tmp_path)
    status = build_status(config_path)
    assert status["config_valid"] is False
    assert status["stages"][0]["status"] == "blocked"
    doctor = build_doctor(config_path)
    pipeline = next(item for item in doctor["checks"] if item["name"] == "pipeline")
    assert pipeline["status"] == "blocked"


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


def test_doctor_blocks_removed_pdf_text_configuration(tmp_path: Path, monkeypatch):
    config_path = _config(tmp_path)
    config_path.write_text(
        config_path.read_text(encoding="utf-8") + "pdf_text:\n  mode: native\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)

    report = build_doctor(config_path)
    checks = {item["name"]: item for item in report["checks"]}

    assert report["ok"] is False
    assert checks["ocr.configuration"]["status"] == "blocked"
    assert "pdf_text is no longer supported" in checks["ocr.configuration"]["detail"]


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


def test_status_only_skips_ocr_correct_for_explicit_native_legacy(tmp_path: Path, monkeypatch):
    config_path = _config(tmp_path)
    config_path.write_text(
        config_path.read_text(encoding="utf-8").replace(
            "    enabled: false", "    enabled: true\n    backend: vllm"
        ),
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    output_dir = tmp_path / "output" / "Diagnostics Book"
    output_dir.mkdir(parents=True)

    (output_dir / "pdf_text_probe.json").write_text(
        json.dumps(
            {
                "classification": "native_text",
                "recommendation": "ocr_required",
            }
        ),
        encoding="utf-8",
    )
    report = build_status(config_path)
    stages = {item["name"]: item for item in report["stages"]}
    assert stages["ocr-correct"]["status"] != "skipped"

    (output_dir / "pdf_text_probe.json").write_text(
        json.dumps(
            {
                "classification": "native_text",
                "recommendation": "use_text_layer",
            }
        ),
        encoding="utf-8",
    )
    (output_dir / "pages").mkdir(exist_ok=True)
    (output_dir / "pages" / "ocr_progress.json").write_text(
        json.dumps({"mode": "native_text"}), encoding="utf-8"
    )
    report = build_status(config_path)
    stages = {item["name"]: item for item in report["stages"]}
    assert stages["ocr-correct"]["status"] == "skipped"


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


def test_status_marks_downstream_artifacts_stale_and_withholds_command(
    tmp_path: Path, monkeypatch
):
    """A missing source must not let a finished EPUB look like a clean run."""

    config_path = _config(tmp_path)
    (tmp_path / "input" / "book.pdf").unlink()
    monkeypatch.chdir(tmp_path)
    output_dir = tmp_path / "output" / "Diagnostics Book"
    output_dir.mkdir(parents=True)
    (output_dir / "Diagnostics Book.epub").write_bytes(b"PK\x03\x04")

    report = build_status(config_path)
    stages = {item["name"]: item for item in report["stages"]}

    assert report["schema_version"] == 4
    assert stages["ocr-pages"]["status"] == "blocked"
    assert stages["ocr-pages"]["reachable"] is False
    assert stages["package"]["status"] == "stale"
    assert stages["package"]["current"] is False
    assert stages["package"]["reachable"] is False
    assert "artifact exists but is stale" in stages["package"]["detail"]
    assert stages["refine-prepare"]["reachable"] is False
    # No runnable command may be advertised while the ladder is blocked.
    assert report["next_command"] is None
    assert report["blocked_stage"] == "ocr-pages"
    assert "input PDF is missing" in report["blocked_reason"]


def test_status_reports_current_and_reachable_flags(tmp_path: Path, monkeypatch):
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

    assert stages["ocr-pages"]["current"] is True
    assert stages["ocr-pages"]["reachable"] is True
    assert stages["refine-prepare"]["current"] is False
    assert stages["refine-prepare"]["reachable"] is True
    assert report["next_command"].endswith("refine-prepare")


def _write_manifest(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload), encoding="utf-8")


def test_status_reports_ocr_evidence_level(tmp_path: Path, monkeypatch):
    """Single OCR must not look cross-verified in the operator summary."""
    config_path = _config(tmp_path)
    monkeypatch.chdir(tmp_path)
    output_dir = tmp_path / "output" / "Diagnostics Book"
    output_dir.mkdir(parents=True)

    report = build_status(config_path)
    assert report["schema_version"] == 4
    assert report["ocr_evidence"] == {
        "mode": "single_ocr",
        "cross_verified": False,
        "detail": "单 OCR 视觉转录；正常页未经第二套 OCR 交叉验证，只有异常页进入视觉复核",
    }

    config_path.write_text(
        config_path.read_text(encoding="utf-8").replace(
            "    enabled: false", "    enabled: true\n    backend: vllm"
        ),
        encoding="utf-8",
    )
    assert build_status(config_path)["ocr_evidence"]["mode"] == "two_ocr"
    assert build_status(config_path)["ocr_evidence"]["cross_verified"] is True

    (output_dir / "pages").mkdir(exist_ok=True)
    (output_dir / "pages" / "ocr_progress.json").write_text(
        json.dumps({"mode": "native_text"}), encoding="utf-8"
    )
    (output_dir / "pdf_text_probe.json").write_text(
        json.dumps({"recommendation": "use_text_layer"}), encoding="utf-8"
    )
    assert build_status(config_path)["ocr_evidence"]["mode"] == "legacy_native"


def test_package_stage_uses_the_build_receipt(tmp_path: Path):
    from pdf2epub.build_receipt import PDF_SOURCE, write_build_receipt
    from pdf2epub.commands.diagnostics import _package_stage

    (tmp_path / "toc_tree.json").write_text("{}", encoding="utf-8")
    (tmp_path / "polish_validation.json").write_text("{}", encoding="utf-8")
    epub = tmp_path / "book.epub"
    epub.write_bytes(b"PK\x03\x04")
    write_build_receipt(tmp_path, epub, kind=PDF_SOURCE)

    stage = _package_stage(
        tmp_path, [epub], command="build-epub", kinds=("pdf-source",)
    )
    assert stage["status"] == "passed"
    assert "matches current checkpoints" in stage["detail"]

    (tmp_path / "polish_validation.json").write_text(
        '{"revalidated": true}', encoding="utf-8"
    )
    stage = _package_stage(
        tmp_path, [epub], command="build-epub", kinds=("pdf-source",)
    )
    assert stage["status"] == "stale"
    assert "does not match current checkpoints" in stage["detail"]


def test_package_stage_keeps_legacy_artifacts_readable(tmp_path: Path):
    from pdf2epub.commands.diagnostics import _package_stage

    epub = tmp_path / "book.epub"
    epub.write_bytes(b"PK\x03\x04")

    stage = _package_stage(
        tmp_path, [epub], command="build-epub", kinds=("pdf-source",)
    )
    assert stage["status"] == "passed"
    assert "no build receipt" in stage["detail"]


def test_status_reports_the_cross_page_boundary_preview(tmp_path: Path, monkeypatch):
    config_path = _config(tmp_path)
    monkeypatch.chdir(tmp_path)
    output_dir = tmp_path / "output" / "Diagnostics Book"
    output_dir.mkdir(parents=True)
    (output_dir / "refine_boundary_preview.json").write_text(
        json.dumps(
            {
                "suspect_count": 2,
                "suspects": [{"unit": "a.md"}, {"unit": "b.md"}],
            }
        ),
        encoding="utf-8",
    )

    report = build_status(config_path)

    assert report["boundary_preview"] == {
        "report": "refine_boundary_preview.json",
        "suspect_count": 2,
        "units": ["a.md", "b.md"],
    }


def test_status_surfaces_pending_subagent_review_gates(tmp_path: Path, monkeypatch):
    config_path = _config(tmp_path)
    monkeypatch.chdir(tmp_path)
    output_dir = tmp_path / "output" / "Diagnostics Book"
    output_dir.mkdir(parents=True)
    _write_manifest(
        output_dir / "footnote_subagent_manifest.json",
        {
            "status": "pending_review",
            "review_candidate_count": 7,
            "visual_audit_task_count": 3,
        },
    )
    _write_manifest(
        output_dir / "translate_subagent_manifest.json",
        {
            "pending_files": ["chapter_1.md", "chapter_2.md"],
            "pending_reason_counts": {"version_only": 2},
            "worker_handoffs": [
                {"worker_id": "w1", "dispatch_status": "available"},
                {"worker_id": "w2", "dispatch_status": "running"},
            ],
            "dispatch_plan": [{"wave": 1}],
        },
    )

    report = build_status(config_path)
    gates = {gate["name"]: gate for gate in report["review_gates"]}

    assert gates["footnote"]["status"] == "pending_review"
    assert gates["footnote"]["needs_subagent"] is True
    assert "7 review candidate(s)" in gates["footnote"]["detail"]
    assert "3 visual audit task(s)" in gates["footnote"]["detail"]

    assert gates["translate"]["needs_subagent"] is True
    assert gates["translate"]["pending_files"] == 2
    assert gates["translate"]["pending_file_names"] == ["chapter_1.md", "chapter_2.md"]
    assert "pending reasons: version_only=2" in gates["translate"]["detail"]
    assert gates["translate"]["workers"] == 2
    assert gates["translate"]["worker_status"] == {"available": 1, "running": 1}
    assert gates["translate"]["waves"] == 1


def test_status_flags_unreadable_review_manifest(tmp_path: Path, monkeypatch):
    config_path = _config(tmp_path)
    monkeypatch.chdir(tmp_path)
    output_dir = tmp_path / "output" / "Diagnostics Book"
    output_dir.mkdir(parents=True)
    (output_dir / "footnote_subagent_manifest.json").write_text("{broken", encoding="utf-8")

    report = build_status(config_path)
    gates = {gate["name"]: gate for gate in report["review_gates"]}

    assert gates["footnote"]["status"] == "unreadable"
    assert gates["footnote"]["needs_subagent"] is True


def test_status_has_no_review_gates_without_manifests(tmp_path: Path, monkeypatch):
    config_path = _config(tmp_path)
    monkeypatch.chdir(tmp_path)

    report = build_status(config_path)

    assert report["review_gates"] == []


def test_native_text_source_is_the_single_truth_predicate(tmp_path: Path) -> None:
    import pytest

    from pdf2epub.refine.pdf_evidence import is_native_text_source

    pages = tmp_path / "pages"
    pages.mkdir()
    (pages / "ocr_progress.json").write_text(
        json.dumps({"mode": "native_text"}), encoding="utf-8"
    )
    assert is_native_text_source(tmp_path) is True

    (pages / "ocr_progress.json").write_text(
        json.dumps({"mode": "ocr"}), encoding="utf-8"
    )
    assert is_native_text_source(tmp_path) is False

    (pages / "ocr_progress.json").write_text(
        json.dumps({"mode": "native_text"}), encoding="utf-8"
    )
    (tmp_path / "pdf_text_probe.json").write_text(
        json.dumps({"recommendation": "ocr_required", "schema_version": 3}),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="conflicting PDF source metadata"):
        is_native_text_source(tmp_path)
