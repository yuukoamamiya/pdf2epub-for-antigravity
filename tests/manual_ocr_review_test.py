"""Synthetic end-to-end coverage for explicitly selected visual OCR review."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from pdf2epub.commands import ocr as ocr_commands
from pdf2epub.ocr_correction import corrected_page_dir, ocr_correction_is_current, select_refinement_pages
from pdf2epub.ocr_progress import new_progress
from pdf2epub.ocr_review_selection import (
    manual_review_selection_state,
    update_manual_review_selection,
)
from pdf2epub.workflow_contracts import sha256_file


SINGLE = {"ocr": {"secondary": {"enabled": False, "backend": "synthetic"}}}


def _workspace(root: Path, page_count: int = 3) -> SimpleNamespace:
    root.mkdir(parents=True, exist_ok=True)
    pdf = root / "input_original.pdf"
    pdf.write_bytes(b"synthetic pdf bytes")
    pages = root / "pages"
    pages.mkdir()
    for page in range(1, page_count + 1):
        name = f"page_{page:03d}"
        (pages / f"{name}.md").write_text(f"Page {page} text.\n", encoding="utf-8")
        (pages / f"{name}.ocr.json").write_text(
            json.dumps({"page_number": page}), encoding="utf-8"
        )
    progress = new_progress(
        source_sha256=sha256_file(pdf),
        total_pages=page_count,
        backend="synthetic",
    )
    progress["pages_processed"] = list(range(1, page_count + 1))
    progress["missing_pages"] = []
    (pages / "ocr_progress.json").write_text(json.dumps(progress), encoding="utf-8")
    (root / "pdf_text_probe.json").write_text(
        json.dumps(
            {
                "schema_version": 3,
                "classification": "scanned",
                "recommendation": "ocr_required",
                "page_count": page_count,
                "source_sha256": sha256_file(pdf),
            }
        ),
        encoding="utf-8",
    )
    config_path = root / "synthetic.yaml"
    config_path.write_text("synthetic", encoding="utf-8")
    config = {**SINGLE, "title": root.name, "input_pdf": str(pdf)}
    context = SimpleNamespace(
        config=config,
        config_path=config_path,
        book_title=root.name,
        output_dir=root,
    )
    return context


def _patch_commands(monkeypatch, context, *, review_pages=()):
    monkeypatch.setattr(ocr_commands, "load_book_context", lambda *_args: context)

    def render(output_dir, *, dpi=150, resume=False):
        image_dir = Path(output_dir) / "ocr_review_images"
        image_dir.mkdir(exist_ok=True)
        page_count = len(list((Path(output_dir) / "pages").glob("page_*.md")))
        images = [f"page_{page:03d}.png" for page in range(1, page_count + 1)]
        for number, image in enumerate(images, 1):
            (image_dir / image).write_bytes(f"synthetic image {number}".encode())
        manifest = {
            "schema_version": 1,
            "source_pdf": "input_original.pdf",
            "source_sha256": sha256_file(Path(output_dir) / "input_original.pdf"),
            "dpi": dpi,
            "page_count": page_count,
            "image_dir": "ocr_review_images",
            "images": images,
        }
        (Path(output_dir) / "ocr_review_images.json").write_text(
            json.dumps(manifest), encoding="utf-8"
        )
        return manifest

    monkeypatch.setattr("pdf2epub.ocr_correction.render_review_images", render)
    if review_pages:
        monkeypatch.setattr(ocr_commands, "secondary_backend_name", lambda _config: "synthetic-secondary")
        monkeypatch.setattr(ocr_commands, "_consensus_is_current_for_context", lambda *_args: True)
        monkeypatch.setattr(ocr_commands, "review_required_files", lambda _root: list(review_pages))
        monkeypatch.setattr(
            ocr_commands,
            "auto_accepted_files",
            lambda _root: sorted(
                path.name for path in (context.output_dir / "pages").glob("page_*.md")
            ),
        )


def _args(**overrides):
    values = {"file": [], "resume": False, "dpi": 150, "input": None}
    values.update(overrides)
    return SimpleNamespace(**values)


def _write_worker_results(output_dir: Path, files: list[str]) -> None:
    targets = output_dir / "ocr_corrected_pages"
    targets.mkdir(exist_ok=True)
    records = output_dir / "ocr_correction_reviews"
    records.mkdir(exist_ok=True)
    for name in files:
        source = output_dir / "pages" / name
        target = targets / name
        source_text = source.read_text(encoding="utf-8")
        target_text = (
            source_text.replace("Page 1 text.", "Page 1 corrected text.")
            if name == "page_001.md"
            else source_text
        )
        target.write_text(target_text, encoding="utf-8")
        image = output_dir / "ocr_review_images" / f"{source.stem}.png"
        source_stats = source.read_text(encoding="utf-8").splitlines()
        target_stats = target.read_text(encoding="utf-8").splitlines()
        record = {
            "schema_version": 1,
            "source_file": name,
            "visual_file": f"ocr_review_images/{source.stem}.png",
            "reviewed": True,
            "coverage": "complete",
            "uncertain": False,
            "source_line_count": len(source_stats),
            "source_nonempty_line_count": sum(bool(line.strip()) for line in source_stats),
            "target_line_count": len(target_stats),
            "target_nonempty_line_count": sum(bool(line.strip()) for line in target_stats),
            "source_sha256": sha256_file(source),
            "target_sha256": sha256_file(target),
            "image_sha256": sha256_file(image),
        }
        (records / f"{source.stem}.json").write_text(json.dumps(record), encoding="utf-8")


def test_manual_ocr_review_prepare_single_file_and_full_validation_e2e(tmp_path, monkeypatch):
    context = _workspace(tmp_path / "e2e")
    _patch_commands(monkeypatch, context)

    assert ocr_commands.ocr_correct_command(_args(file=["page_001.md"])) == 0
    first_manifest = json.loads(
        (context.output_dir / "ocr-correct_subagent_manifest.json").read_text()
    )
    assert first_manifest["manual_review_files"] == ["page_001.md"]
    selection_path = context.output_dir / "ocr_correction_selection.json"
    selection_path.unlink()
    missing_state = manual_review_selection_state(context.output_dir)
    assert missing_state["present"]
    assert missing_state["files"] == ["page_001.md"]
    assert "no matching selection contract" in missing_state["detail"]
    with pytest.raises(ValueError, match="no matching selection contract"):
        select_refinement_pages(context.output_dir)
    recovered = update_manual_review_selection(
        context.output_dir, ["page_002.md"]
    )
    assert set(recovered["requested_files"]) == {"page_001.md", "page_002.md"}

    assert ocr_commands.ocr_correct_command(
        _args(file=["page_002.md"], resume=True)
    ) == 0
    assert ocr_commands.ocr_correct_command(_args(resume=True)) == 0
    manifest = json.loads(
        (context.output_dir / "ocr-correct_subagent_manifest.json").read_text()
    )
    assert manifest["manual_review_files"] == ["page_001.md", "page_002.md"]
    _write_worker_results(context.output_dir, manifest["manual_review_files"])

    assert ocr_commands.ocr_correct_validate_command(
        _args(file="page_001.md")
    ) == 0
    assert not (context.output_dir / "ocr-correct_validation.json").exists()
    assert not ocr_correction_is_current(context.output_dir, context.config)

    assert ocr_commands.ocr_correct_validate_command(_args()) == 0
    report = json.loads(
        (context.output_dir / "ocr-correct_validation.json").read_text()
    )
    assert report["all_passed"] is True
    assert report["visual_review_files"] == ["page_001.md", "page_002.md"]
    assert ocr_correction_is_current(context.output_dir, context.config)
    selected, kind = select_refinement_pages(
        context.output_dir, require_correction=True, config=context.config
    )
    assert selected == corrected_page_dir(context.output_dir)
    assert kind == "ocr_corrected"
    assert (selected / "page_001.md").read_text(encoding="utf-8") == "Page 1 corrected text.\n"
    assert (context.output_dir / "pages" / "page_001.md").read_text(encoding="utf-8") == "Page 1 text.\n"
    (context.output_dir / "ocr_review_images" / "page_001.png").write_bytes(b"changed image")
    assert not ocr_correction_is_current(context.output_dir, context.config)


def test_dual_ocr_manual_selection_is_union_with_consensus_pages(tmp_path, monkeypatch):
    context = _workspace(tmp_path / "dual")
    secondary = context.output_dir / "ocr_secondary"
    secondary.mkdir()
    for source in (context.output_dir / "pages").glob("page_*.md"):
        (secondary / source.name).write_text("secondary candidate\n", encoding="utf-8")
    _patch_commands(monkeypatch, context, review_pages=("page_002.md",))
    update_manual_review_selection(context.output_dir, ["page_001.md"])

    assert ocr_commands.ocr_correct_command(_args(resume=True)) == 0
    manifest = json.loads(
        (context.output_dir / "ocr-correct_subagent_manifest.json").read_text()
    )
    assert manifest["manual_review_files"] == ["page_001.md"]
    assert manifest["pending_files"] == ["page_001.md", "page_002.md"]
    prompt = (context.output_dir / "ocr-correct_subagent_prompt.md").read_text(encoding="utf-8")
    assert "plus any explicitly selected pages" in prompt


def test_selection_contract_fails_closed_on_missing_corrupt_and_stale_inputs(tmp_path):
    context = _workspace(tmp_path / "stale")
    root = context.output_dir
    update_manual_review_selection(root, ["page_001.md"])
    contract_path = root / "ocr_correction_selection.json"
    (root / "ocr-correct_validation.json").write_text(
        json.dumps({"manual_review_files": ["page_001.md"]}), encoding="utf-8"
    )
    contract_path.unlink()
    state = manual_review_selection_state(root)
    assert state["present"] and not state["current"]
    with pytest.raises(ValueError, match="no matching selection contract"):
        select_refinement_pages(root)

    contract = update_manual_review_selection(root, ["page_001.md"])
    contract_path.write_text("{invalid", encoding="utf-8")
    state = manual_review_selection_state(root)
    assert state["present"] and not state["current"]
    assert "corrupt" in state["detail"]
    with pytest.raises(ValueError):
        select_refinement_pages(root)

    contract_path.write_text(json.dumps(contract), encoding="utf-8")
    page = root / "pages" / "page_001.md"
    page.write_text("changed raw page", encoding="utf-8")
    assert not manual_review_selection_state(root)["current"]
    with pytest.raises(ValueError):
        select_refinement_pages(root)

    update_manual_review_selection(root, ["page_001.md"])
    sidecar = root / "pages" / "page_001.ocr.json"
    sidecar.write_text('{"page_number":1,"changed":true}', encoding="utf-8")
    assert not manual_review_selection_state(root)["current"]
    with pytest.raises(ValueError, match="unknown OCR page selection"):
        update_manual_review_selection(root, ["page_004.md"])

    update_manual_review_selection(root, ["page_001.md"])
    (root / "input_original.pdf").write_bytes(b"replaced source pdf")
    assert not manual_review_selection_state(root)["current"]


def test_selection_service_handles_malformed_progress_contract_and_io_errors(tmp_path, monkeypatch):
    context = _workspace(tmp_path / "malformed")
    root = context.output_dir
    update_manual_review_selection(root, ["page_001.md"])
    monkeypatch.setattr(
        "pdf2epub.ocr_review_selection.sha256_file",
        lambda _path: (_ for _ in ()).throw(PermissionError("synthetic denied")),
    )
    state = manual_review_selection_state(root)
    assert state["present"] and not state["current"]
    monkeypatch.undo()

    contract_path = root / "ocr_correction_selection.json"
    valid_contract = json.loads(contract_path.read_text(encoding="utf-8"))

    def write_resigned(payload):
        payload = dict(payload)
        payload.pop("selection_sha256", None)
        canonical = json.dumps(
            payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        payload["selection_sha256"] = hashlib.sha256(canonical).hexdigest()
        contract_path.write_text(json.dumps(payload), encoding="utf-8")

    malformed_pages = dict(valid_contract)
    malformed_pages["page_files"] = [["unhashable"]]
    write_resigned(malformed_pages)
    state = manual_review_selection_state(root)
    assert state["present"] and not state["current"]

    duplicate_request = dict(valid_contract)
    duplicate_request["requested_files"] = ["page_001.md", "page_001.md"]
    write_resigned(duplicate_request)
    state = manual_review_selection_state(root)
    assert state["present"] and not state["current"]

    (root / "ocr-correct_subagent_manifest.json").write_text(
        json.dumps({"manual_review_files": ["page_001.md"]}), encoding="utf-8"
    )
    recovered = update_manual_review_selection(root, ["page_002.md"])
    assert set(recovered["requested_files"]) == {"page_001.md", "page_002.md"}

    (root / "pages" / "ocr_progress.json").write_text("[]", encoding="utf-8")
    state = manual_review_selection_state(root)
    assert state["present"] and not state["current"]
    assert "JSON object" in state["detail"]


def test_missing_contract_with_selection_hash_but_no_file_list_requests_reselection(tmp_path):
    context = _workspace(tmp_path / "hash-only")
    (context.output_dir / "ocr-correct_subagent_manifest.json").write_text(
        json.dumps(
            {
                "manual_review_files": [],
                "manual_review_selection_sha256": "a" * 64,
            }
        ),
        encoding="utf-8",
    )

    state = manual_review_selection_state(context.output_dir)
    assert state["present"] and not state["current"]
    assert state["files"] == []
    assert "reselect pages with ocr-correct --file" in state["detail"]



def test_selection_inventory_is_numerically_ordered_past_page_999(tmp_path):
    context = _workspace(tmp_path / "thousand", page_count=1000)
    contract = update_manual_review_selection(
        context.output_dir, ["page_1000.md", "page_999.md"]
    )

    assert contract["page_files"][-2:] == ["page_999.md", "page_1000.md"]
    assert set(contract["requested_files"]) == {"page_1000.md", "page_999.md"}
