import json
from pathlib import Path

from pdf2epub.refine.pdf_evidence import (
    EVIDENCE_RECEIPT_SCHEMA_VERSION,
    build_evidence_receipt,
    evidence_receipt_is_current,
    write_evidence_receipt,
)
from pdf2epub.workflow_contracts import schema_major_matches


def _write_evidence(root: Path) -> None:
    pages = root / "pages"
    pages.mkdir()
    (pages / "page_001.md").write_text("Body text.", encoding="utf-8")
    (pages / "page_001.ocr.json").write_text(
        json.dumps({"page_number": 1, "blocks": []}), encoding="utf-8"
    )


def test_evidence_receipt_minor_upgrade_forward_rolls(tmp_path: Path):
    _write_evidence(tmp_path)
    receipt = write_evidence_receipt(tmp_path, {})
    stored = receipt.to_dict()

    current, errors = evidence_receipt_is_current(stored, tmp_path, {})
    assert current is True, errors

    stored["schema_version"] = f"{EVIDENCE_RECEIPT_SCHEMA_VERSION}.1"
    current, errors = evidence_receipt_is_current(stored, tmp_path, {})
    assert current is True, errors

    stored["schema_version"] = str(EVIDENCE_RECEIPT_SCHEMA_VERSION + 1)
    current, errors = evidence_receipt_is_current(stored, tmp_path, {})
    assert current is False
    assert any("malformed" in error for error in errors)


def test_evidence_receipt_detects_changed_pages_and_policy(tmp_path: Path):
    _write_evidence(tmp_path)
    receipt = build_evidence_receipt(tmp_path, {})

    (tmp_path / "pages" / "page_001.md").write_text("Changed body.", encoding="utf-8")
    current, errors = evidence_receipt_is_current(receipt.to_dict(), tmp_path, {})
    assert current is False
    assert any("page Markdown" in error for error in errors)

    (tmp_path / "pages" / "page_001.md").write_text("Body text.", encoding="utf-8")
    receipt = build_evidence_receipt(tmp_path, {})
    changed_policy = {"ocr": {"secondary": {"enabled": True, "backend": "vllm"}}}
    current, errors = evidence_receipt_is_current(receipt.to_dict(), tmp_path, changed_policy)
    assert current is False
    assert any("policy changed" in error for error in errors)


def test_schema_major_matches_only_compares_major():
    assert schema_major_matches(5, 5) is True
    assert schema_major_matches("5.1", 5) is True
    assert schema_major_matches(5, "5.2") is True
    assert schema_major_matches(5, 6) is False
    assert schema_major_matches("x", 5) is False
