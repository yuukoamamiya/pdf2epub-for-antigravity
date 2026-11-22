import hashlib
import json
from pathlib import Path
import pytest

from pdf2epub.commands.diagnostics import build_doctor, build_status
from pdf2epub.commands.diagnostics import _entity_stage_status
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


def test_entity_status_explains_incremental_review_and_format_refresh(tmp_path: Path, monkeypatch):
    from pdf2epub.commands import sources

    output = tmp_path / "output" / "Book"
    source_dir = output / "polished_markdown" / "validated"
    source_dir.mkdir(parents=True)
    source_file = source_dir / "chapter_1.md"
    source_file.write_text("Current source", encoding="utf-8")
    monkeypatch.setattr(sources, "_resolve_pdf_markdown_source", lambda *_a, **_kw: (source_dir, "polished"))
    config = {"title": "Book", "translation": {"source_language": "English", "target_language": "Chinese"}}
    from pdf2epub.entity_context import prepare_entity_subagent_task

    prepare_entity_subagent_task(output, "Book", source_dir, "polished", "English", "Chinese", {})
    manifest_path = output / "entity_subagent_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest.update({"mode": "incremental_review", "pending_files": ["chapter_1.md"], "assigned_files": ["chapter_1.md"]})
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    ready, detail = _entity_stage_status(output, config)
    assert ready is False
    assert "incremental review required" in detail
    assert "chapter_1.md" in detail

    manifest.update({"mode": "refresh", "pending_files": [], "assigned_files": []})
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    ready, detail = _entity_stage_status(output, config)
    assert ready is False
    assert "formatting refresh only" in detail
    assert "no Subagent review needed" in detail

    source_file.write_text("A substantive source edit", encoding="utf-8")
    ready, detail = _entity_stage_status(output, config)
    assert ready is False
    assert "source changed after preparation" in detail
    assert "formatting refresh only" not in detail


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
    assert "legacy artifact is not verified" in stages["package"]["detail"]
    assert "target attribution unknown" in stages["package"]["detail"]
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
    assert stage["targets"][0]["status"] == "legacy"
    assert "without a build receipt" in stage["detail"]
    assert "target attribution unknown" in stage["detail"]


def test_package_stage_requires_every_requested_target_and_reports_paths(tmp_path: Path):
    from pdf2epub.build_receipt import PDF_SOURCE, PDF_TRANSLATED, write_build_receipt
    from pdf2epub.commands.diagnostics import _package_stage

    for name, data in (
        ("toc_tree.json", b"{}"),
        ("polish_validation.json", b"{}"),
        ("translate_validation.json", b"{}"),
    ):
        (tmp_path / name).write_bytes(data)
    source = tmp_path / "original.epub"
    translated = tmp_path / "translated.epub"
    source.write_bytes(b"en")
    translated.write_bytes(b"zh")
    write_build_receipt(tmp_path, source, kind=PDF_SOURCE)
    write_build_receipt(tmp_path, translated, kind=PDF_TRANSLATED)

    (tmp_path / "translate_validation.json").write_text('{"changed": true}', encoding="utf-8")
    stage = _package_stage(
        tmp_path, [source, translated], command="build-epub", kinds=(PDF_SOURCE, PDF_TRANSLATED)
    )
    assert stage["status"] == "stale"
    assert [target["status"] for target in stage["targets"]] == ["current", "stale"]
    assert [target["artifact"] for target in stage["targets"]] == [
        "original.epub", "translated.epub"
    ]


def test_translation_target_missing_receipt_is_pending_even_with_source_receipt(tmp_path: Path):
    from pdf2epub.build_receipt import PDF_SOURCE, PDF_TRANSLATED, write_build_receipt
    from pdf2epub.commands.diagnostics import _package_stage

    (tmp_path / "toc_tree.json").write_text("{}", encoding="utf-8")
    (tmp_path / "polish_validation.json").write_text("{}", encoding="utf-8")
    source = tmp_path / "original.epub"
    source.write_bytes(b"en")
    write_build_receipt(tmp_path, source, kind=PDF_SOURCE)

    stage = _package_stage(
        tmp_path, [source], command="build-epub --only-translated", kinds=(PDF_TRANSLATED,)
    )
    assert stage["status"] == "pending"
    assert stage["targets"][0]["status"] == "pending"
    assert stage["targets"][0]["artifact"] is None


@pytest.mark.parametrize(
    "pipeline,receipt_kind,expected_kind,expected_command",
    [
        ("pdf_translation", "pdf-source", "pdf-translated", "build-epub --only-translated"),
        ("epub_conversion", "pdf-translated", "pdf-source", "build-epub"),
    ],
)
def test_pipeline_status_recommends_missing_required_target(
    tmp_path: Path, monkeypatch, pipeline, receipt_kind, expected_kind, expected_command
):
    import pdf2epub.commands.diagnostics as diagnostics
    import pdf2epub.toc_translation_workflow as toc_translation

    config_path = _config(tmp_path)
    config_path.write_text(
        config_path.read_text(encoding="utf-8") + f"pipeline: {pipeline}\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    output_dir = tmp_path / "output" / "Diagnostics Book"
    (output_dir / "ocr_markdown").mkdir(parents=True)
    (output_dir / "ocr_markdown" / "tree_progress.json").write_text("{}", encoding="utf-8")
    (output_dir / "ocr_markdown" / "chapter.md").write_text("source", encoding="utf-8")
    (output_dir / "illustration_candidate_report.json").write_text('{"candidates":[]}', encoding="utf-8")
    (output_dir / "illustration_bindings.json").write_text('{"status":"validated"}', encoding="utf-8")
    artifact = output_dir / "original.epub"
    artifact.write_bytes(b"en")
    for name in ("toc_tree.json", "polish_validation.json", "translate_validation.json"):
        (output_dir / name).write_text("{}", encoding="utf-8")
    from pdf2epub.build_receipt import PDF_SOURCE, PDF_TRANSLATED, write_build_receipt

    write_build_receipt(
        output_dir,
        artifact,
        kind={"pdf-source": PDF_SOURCE, "pdf-translated": PDF_TRANSLATED}[receipt_kind],
    )
    monkeypatch.setattr(diagnostics, "_pdf_ocr_status", lambda *_args: ("passed", "current"))
    monkeypatch.setattr(diagnostics, "is_native_text_source", lambda _path: True)
    monkeypatch.setattr(diagnostics.PipelinePolicy, "requires_visual_ocr_review", lambda *_args: False)
    monkeypatch.setattr(diagnostics, "_pdf_tree_status", lambda *_args: ("passed", "current"))
    monkeypatch.setattr(diagnostics, "_current_polish", lambda *_args: True)
    monkeypatch.setattr(diagnostics, "_entity_stage_status", lambda *_args: (True, "current"))
    monkeypatch.setattr(toc_translation, "validate_toc_translation_subagent", lambda *_args, **_kwargs: {"valid": True})
    monkeypatch.setattr(diagnostics, "_current_translation", lambda *_args: True)
    monkeypatch.setattr(
        "pdf2epub.refine.footnote_status.footnote_normalization_is_current",
        lambda *_args, **_kwargs: True,
    )

    report = build_status(config_path)
    package = next(stage for stage in report["stages"] if stage["name"] == "package")
    assert package["status"] == "pending"
    assert package["command"] == expected_command
    assert package["targets"][0]["kind"] == expected_kind
    assert report["next_command"].endswith(expected_command)


def test_legacy_package_remains_current_when_upstream_is_current(tmp_path: Path):
    from pdf2epub.commands.diagnostics import _apply_stage_blockers, _package_stage

    artifact = tmp_path / "old.epub"
    artifact.write_bytes(b"legacy")
    package = _package_stage(
        tmp_path, [artifact], command="build-epub", kinds=("pdf-source",)
    )
    stages = _apply_stage_blockers(
        [{"name": "upstream", "command": "noop", "status": "passed", "detail": "current"}, package]
    )
    assert stages[1]["status"] == "passed"
    assert stages[1]["current"] is True
    assert stages[1]["targets"][0]["status"] == "legacy"


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
