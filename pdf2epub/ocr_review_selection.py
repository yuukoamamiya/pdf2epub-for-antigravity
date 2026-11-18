"""Persistent identity contract for explicitly requested OCR page reviews."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any, Iterable, Mapping

from pdf2epub.workflow_contracts import atomic_write_json, sha256_file


OCR_REVIEW_SELECTION_SCHEMA_VERSION = 1
OCR_REVIEW_SELECTION_NAME = "ocr_correction_selection.json"


def _selection_path(output_dir: Path) -> Path:
    return Path(output_dir) / OCR_REVIEW_SELECTION_NAME


def _canonical_hash(value: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _is_sha256(value: Any) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def _is_hash_map(value: Any, expected_names: list[str]) -> bool:
    return (
        isinstance(value, dict)
        and set(value) == set(expected_names)
        and all(_is_sha256(digest) for digest in value.values())
    )


def _read_raw_identity(output_dir: Path) -> dict[str, Any]:
    output_dir = Path(output_dir)
    pages_dir = output_dir / "pages"
    pdf_path = output_dir / "input_original.pdf"
    if not pdf_path.is_file():
        pdf_path = output_dir / "input.pdf"
    if not pdf_path.is_file():
        raise ValueError("the current source PDF is missing from the book workspace")
    page_hashes = {
        path.name: sha256_file(path)
        for path in sorted(pages_dir.glob("page_*.md"))
        if path.is_file()
    }
    if not page_hashes:
        raise ValueError("raw OCR pages are missing")
    try:
        progress = json.loads(
            (pages_dir / "ocr_progress.json").read_text(encoding="utf-8")
        )
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("OCR progress is missing or invalid") from exc
    if not isinstance(progress, Mapping):
        raise ValueError("OCR progress must be a JSON object")
    source_pdf_sha256 = sha256_file(pdf_path)
    if progress.get("source_sha256") != source_pdf_sha256:
        raise ValueError("OCR pages do not match the current source PDF; rerun ocr-pages --resume")
    raw_total = progress.get("total_pages")
    if not isinstance(raw_total, int) or isinstance(raw_total, bool):
        raise ValueError("OCR progress has an invalid total page count")
    try:
        total_pages = int(raw_total)
    except (TypeError, ValueError) as exc:
        raise ValueError("OCR progress has an invalid total page count") from exc
    expected_names = [f"page_{number:03d}.md" for number in range(1, total_pages + 1)]
    expected_sidecars = [f"page_{number:03d}.ocr.json" for number in range(1, total_pages + 1)]
    if total_pages <= 0 or len(page_hashes) != total_pages or set(page_hashes) != set(expected_names):
        raise ValueError("OCR page inventory is incomplete or does not match progress")
    from pdf2epub.ocr_progress import assess_progress

    readiness = assess_progress(
        pages_dir,
        expected_total_pages=total_pages,
        expected_source_sha256=source_pdf_sha256,
        require_sidecars=True,
    )
    if not readiness["ready"]:
        raise ValueError(
            "OCR pages are incomplete or stale; rerun ocr-pages --resume: "
            + "; ".join(readiness["errors"][:3])
        )
    sidecar_hashes = {
        name: sha256_file(pages_dir / name) for name in expected_sidecars
    }
    return {
        "source_pdf": pdf_path.name,
        "source_pdf_sha256": source_pdf_sha256,
        "ocr_source_sha256": str(progress.get("source_sha256") or ""),
        "page_files": expected_names,
        "page_sha256": page_hashes,
        "sidecar_sha256": sidecar_hashes,
    }


def _read_contract(output_dir: Path) -> tuple[dict[str, Any] | None, str | None]:
    path = _selection_path(output_dir)
    try:
        if not path.exists():
            return None, None
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None, "manual OCR page selection is corrupt; do not continue with raw pages"
    if not isinstance(value, dict):
        return None, "manual OCR page selection is invalid; do not continue with raw pages"
    recorded_hash = value.get("selection_sha256")
    payload = {key: item for key, item in value.items() if key != "selection_sha256"}
    if (
        value.get("schema_version") != OCR_REVIEW_SELECTION_SCHEMA_VERSION
        or not isinstance(recorded_hash, str)
        or recorded_hash != _canonical_hash(payload)
        or not isinstance(value.get("requested_files"), list)
        or not value.get("requested_files")
        or not isinstance(value.get("page_files"), list)
        or not all(isinstance(name, str) for name in value.get("page_files", []))
        or len(value.get("page_files", [])) != len(set(value.get("page_files", [])))
    ):
        return None, "manual OCR page selection is invalid or unsupported; do not continue with raw pages"
    requested = value["requested_files"]
    if not all(isinstance(name, str) for name in requested):
        return None, "manual OCR page selection contains invalid filenames"
    if len(requested) != len(set(requested)):
        return None, "manual OCR page selection contains duplicate filenames"
    if any(
        not isinstance(name, str)
        or Path(name).name != name
        or not re.fullmatch(r"page_\d{3,}\.md", name)
        for name in requested
    ):
        return None, "manual OCR page selection contains invalid filenames; do not continue with raw pages"
    if not set(requested).issubset(value["page_files"]):
        return None, "manual OCR page selection references a page outside its bound page set"
    page_files = value["page_files"]
    if any(not re.fullmatch(r"page_\d{3,}\.md", name) for name in page_files):
        return None, "manual OCR page selection has an invalid bound page inventory"
    if not _is_sha256(value.get("source_pdf_sha256")) or not _is_sha256(
        value.get("ocr_source_sha256")
    ):
        return None, "manual OCR page selection has invalid source hashes"
    expected_sidecars = [f"{Path(name).stem}.ocr.json" for name in page_files]
    if not _is_hash_map(value.get("page_sha256"), page_files) or not _is_hash_map(
        value.get("sidecar_sha256"), expected_sidecars
    ):
        return None, "manual OCR page selection has invalid page or sidecar hashes"
    return value, None


def _contract_identity(contract: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: contract.get(key)
        for key in (
            "source_pdf",
            "source_pdf_sha256",
            "ocr_source_sha256",
            "page_files",
            "page_sha256",
            "sidecar_sha256",
        )
    }


def _manual_handoff_reference(output_dir: Path) -> tuple[bool, list[str], str | None]:
    """Find durable manifest/receipt references to an explicit selection."""
    references: list[tuple[str, Path]] = [
        ("handoff", Path(output_dir) / "ocr-correct_subagent_manifest.json"),
        ("validation", Path(output_dir) / "ocr-correct_validation.json"),
    ]
    files: set[str] = set()
    referenced = False
    for label, path in references:
        if not path.exists():
            continue
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            return True, sorted(files), f"manual OCR {label} is invalid; do not continue with raw pages"
        if not isinstance(value, dict):
            return True, sorted(files), f"manual OCR {label} is invalid; do not continue with raw pages"
        names = value.get("manual_review_files")
        selection_hash = value.get("manual_review_selection_sha256")
        if names or selection_hash:
            referenced = True
            if isinstance(names, list):
                files.update(
                    name
                    for name in names
                    if isinstance(name, str)
                    and re.fullmatch(r"page_\d{3,}\.md", name)
                )
    return referenced, sorted(files), None


def manual_review_selection_state(output_dir: Path) -> dict[str, Any]:
    """Describe whether a persisted explicit page selection is present/current."""
    contract, error = _read_contract(output_dir)
    if contract is None:
        manual_reference, referenced_files, reference_error = _manual_handoff_reference(
            Path(output_dir)
        )
        return {
            "present": bool(_selection_path(output_dir).exists() or manual_reference),
            "current": False,
            "files": referenced_files,
            "selection_sha256": None,
            "detail": error or reference_error or (
                (
                    "manual OCR selection hash remains in a handoff or receipt but selected filenames are unavailable; reselect pages with ocr-correct --file"
                    if not referenced_files
                    else "manual OCR handoff or receipt has no matching selection contract"
                )
                if manual_reference else ""
            ),
        }
    try:
        identity = _read_raw_identity(output_dir)
    except (OSError, UnicodeError, ValueError, TypeError) as exc:
        return {
            "present": True,
            "current": False,
            "files": list(contract["requested_files"]),
            "selection_sha256": contract["selection_sha256"],
            "detail": str(exc),
        }
    current = _contract_identity(contract) == identity
    return {
        "present": True,
        "current": current,
        "files": sorted(str(name) for name in contract["requested_files"]),
        "selection_sha256": contract["selection_sha256"],
        "detail": "" if current else "manual OCR page selection is stale; reaffirm it with ocr-correct --file",
    }


def has_manual_review_selection(output_dir: Path) -> bool:
    """Return whether a manual selection or a receipt referencing one exists."""
    return manual_review_selection_state(output_dir)["present"]


def update_manual_review_selection(
    output_dir: Path,
    requested_files: Iterable[str],
) -> dict[str, Any]:
    """Accumulate explicit pages and bind the request to the current page set.

    Supplying new page names is an explicit reaffirmation after source changes.
    Existing requested page names are retained; removed page names block instead
    of being silently dropped.
    """
    output_dir = Path(output_dir)
    supplied = sorted({str(name) for name in requested_files})
    contract, error = _read_contract(output_dir)
    manual_reference, referenced_files, reference_error = _manual_handoff_reference(
        output_dir
    )
    if error and not supplied:
        raise ValueError(
            error + "; recover by explicitly reselecting every desired page with ocr-correct --file"
        )
    if reference_error and not supplied:
        raise ValueError(
            reference_error + "; recover by explicitly reselecting every desired page with ocr-correct --file"
        )
    identity = _read_raw_identity(output_dir)
    if contract and _contract_identity(contract) != identity and not supplied:
        raise ValueError(
            "manual OCR page selection is stale; reaffirm selected pages with ocr-correct --file"
        )
    old_names = (
        list(contract["requested_files"])
        if contract
        else referenced_files if manual_reference else []
    )
    if manual_reference and not referenced_files and not contract and supplied:
        # A hash-only reference cannot reveal the previous page names.  The
        # supplied --file values are therefore an explicit complete reselection.
        old_names = []
    missing = sorted(set(old_names) - set(identity["page_files"]))
    if missing:
        raise ValueError(
            "manual OCR selection references pages no longer present: "
            + ", ".join(missing)
        )
    for name in supplied:
        if Path(name).name != name or not name.endswith(".md") or name not in identity["page_files"]:
            raise ValueError(f"unknown OCR page selection: {name}")
    selected = sorted(set(old_names) | set(supplied))
    if not selected:
        raise ValueError("at least one page must be selected with --file")
    payload: dict[str, Any] = {
        "schema_version": OCR_REVIEW_SELECTION_SCHEMA_VERSION,
        "requested_files": selected,
        **identity,
    }
    payload["selection_sha256"] = _canonical_hash(payload)
    atomic_write_json(_selection_path(output_dir), payload)
    return payload


__all__ = [
    "OCR_REVIEW_SELECTION_NAME",
    "OCR_REVIEW_SELECTION_SCHEMA_VERSION",
    "has_manual_review_selection",
    "manual_review_selection_state",
    "update_manual_review_selection",
]
