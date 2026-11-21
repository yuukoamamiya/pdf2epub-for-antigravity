"""Shared evidence selection for PDF layout review stages.

Footnote and illustration review both consume the same page sidecars.  This
module owns the source/evidence decision so a legacy native-text run cannot
accidentally enter the two-OCR path in one stage but not the other.  New runs
never produce native pages; the native branch exists only for old artifacts.

It also owns the single :class:`EvidenceReceipt` that binds a checkpoint to the
page set, sidecars, consensus manifest, and OCR policy.  Validation stages
compare that receipt instead of re-hashing every file in their own way.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Optional

from ..ocr_consensus import (
    consensus_is_current,
    consensus_manifest_path,
    ocr_evidence_mode,
    secondary_config_hash,
)
from ..ocr_source import is_legacy_native_source
from ..workflow_contracts import atomic_write_json, schema_major_matches, sha256_file
from .layout_evidence import layout_sidecar_paths


EVIDENCE_RECEIPT_SCHEMA_VERSION = 1
EVIDENCE_RECEIPT_FILENAME = "evidence_receipt.json"


def is_native_text_source(output_dir: Path) -> bool:
    """Return whether the page set is a legacy native PDF text extraction."""
    return is_legacy_native_source(output_dir)

def pdf_evidence_mode(
    output_dir: Path,
    config: Optional[Mapping[str, Any]] = None,
) -> str:
    """Resolve the layout evidence mode shared by PDF review stages.

    Legacy native text has its own page-point layout evidence and therefore
    bypasses visual OCR consensus, even when the configuration enables a
    secondary OCR backend.  ``single_ocr`` is retained as the checkpoint value
    for this legacy source kind because downstream contracts already use that
    vocabulary.
    """
    if is_native_text_source(output_dir):
        return "single_ocr"
    return ocr_evidence_mode(config)


def require_current_consensus(
    output_dir: Path,
    config: Mapping[str, Any],
    *,
    stage: str,
) -> None:
    """Require a current two-OCR checkpoint when the selected mode needs it."""
    if pdf_evidence_mode(output_dir, config) != "two_ocr":
        return
    if not consensus_is_current(output_dir, config):
        raise ValueError(
            f"two-OCR {stage} review requires a current ocr_consensus.json; "
            "rerun ocr-pages and ocr-correct-validate first"
        )


def _files_fingerprint(paths: list[Path]) -> str:
    digest = hashlib.sha256()
    for path in sorted(paths):
        digest.update(path.name.encode("utf-8"))
        digest.update(hashlib.sha256(path.read_bytes()).digest())
    return digest.hexdigest()


def _page_markdown_paths(pages_dir: Path) -> list[Path]:
    return [
        path
        for path in Path(pages_dir).glob("page_*.md")
        if path.is_file()
    ]


@dataclass(frozen=True)
class EvidenceReceipt:
    """One hash-bound view of the page evidence used by layout stages."""

    pages_sha: str
    sidecar_sha: str
    consensus_sha: Optional[str]
    policy_sha: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": EVIDENCE_RECEIPT_SCHEMA_VERSION,
            "pages_sha": self.pages_sha,
            "sidecar_sha": self.sidecar_sha,
            "consensus_sha": self.consensus_sha,
            "policy_sha": self.policy_sha,
        }

    @classmethod
    def from_mapping(cls, value: Any) -> Optional["EvidenceReceipt"]:
        if not isinstance(value, Mapping):
            return None
        if not schema_major_matches(
            value.get("schema_version"), EVIDENCE_RECEIPT_SCHEMA_VERSION
        ):
            return None
        pages_sha = value.get("pages_sha")
        sidecar_sha = value.get("sidecar_sha")
        policy_sha = value.get("policy_sha")
        consensus_sha = value.get("consensus_sha")
        if not all(isinstance(item, str) for item in (pages_sha, sidecar_sha, policy_sha)):
            return None
        if consensus_sha is not None and not isinstance(consensus_sha, str):
            return None
        return cls(
            pages_sha=pages_sha,
            sidecar_sha=sidecar_sha,
            consensus_sha=consensus_sha,
            policy_sha=policy_sha,
        )


def evidence_policy_payload(
    output_dir: Path,
    config: Optional[Mapping[str, Any]] = None,
) -> dict[str, Any]:
    effective_config = config if isinstance(config, Mapping) else {}
    mode = pdf_evidence_mode(output_dir, effective_config)
    return {
        "schema_version": EVIDENCE_RECEIPT_SCHEMA_VERSION,
        "mode": mode,
        "secondary_config_sha256": secondary_config_hash(dict(effective_config)),
    }


def build_evidence_receipt(
    output_dir: Path,
    config: Optional[Mapping[str, Any]] = None,
    *,
    pages_dir: Optional[Path] = None,
    sidecar_dir: Optional[Path] = None,
) -> EvidenceReceipt:
    """Compute the current evidence receipt in one place.

    ``pages_dir`` may point at a validated corrected page set; sidecars are
    still read from the raw ``pages`` directory unless ``sidecar_dir`` is
    supplied.
    """
    output_dir = Path(output_dir)
    selected_pages = Path(pages_dir) if pages_dir is not None else output_dir / "pages"
    selected_sidecars = (
        Path(sidecar_dir) if sidecar_dir is not None else output_dir / "pages"
    )
    mode = pdf_evidence_mode(output_dir, config)
    consensus_sha = None
    if mode == "two_ocr":
        manifest = consensus_manifest_path(output_dir)
        consensus_sha = sha256_file(manifest) if manifest.is_file() else None
    payload = json.dumps(
        evidence_policy_payload(output_dir, config),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return EvidenceReceipt(
        pages_sha=_files_fingerprint(_page_markdown_paths(selected_pages)),
        sidecar_sha=_files_fingerprint(layout_sidecar_paths(selected_sidecars)),
        consensus_sha=consensus_sha,
        policy_sha=hashlib.sha256(payload.encode("utf-8")).hexdigest(),
    )


def evidence_receipt_is_current(
    recorded: Any,
    output_dir: Path,
    config: Optional[Mapping[str, Any]] = None,
    *,
    pages_dir: Optional[Path] = None,
    sidecar_dir: Optional[Path] = None,
) -> tuple[bool, list[str]]:
    """Compare a stored receipt against the current evidence."""
    stored = EvidenceReceipt.from_mapping(recorded)
    if stored is None:
        return False, ["evidence receipt is missing or malformed"]
    current = build_evidence_receipt(
        output_dir,
        config,
        pages_dir=pages_dir,
        sidecar_dir=sidecar_dir,
    )
    errors: list[str] = []
    if stored.pages_sha != current.pages_sha:
        errors.append("page Markdown evidence changed")
    if stored.sidecar_sha != current.sidecar_sha:
        errors.append("OCR sidecar evidence changed")
    if stored.consensus_sha != current.consensus_sha:
        errors.append("OCR consensus evidence changed")
    if stored.policy_sha != current.policy_sha:
        errors.append("OCR evidence policy changed")
    return not errors, errors


def write_evidence_receipt(
    output_dir: Path,
    config: Optional[Mapping[str, Any]] = None,
    *,
    pages_dir: Optional[Path] = None,
    sidecar_dir: Optional[Path] = None,
) -> EvidenceReceipt:
    receipt = build_evidence_receipt(
        output_dir,
        config,
        pages_dir=pages_dir,
        sidecar_dir=sidecar_dir,
    )
    atomic_write_json(Path(output_dir) / EVIDENCE_RECEIPT_FILENAME, receipt.to_dict())
    return receipt


def load_evidence_receipt(output_dir: Path) -> Optional[EvidenceReceipt]:
    path = Path(output_dir) / EVIDENCE_RECEIPT_FILENAME
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    return EvidenceReceipt.from_mapping(value)


__all__ = [
    "EVIDENCE_RECEIPT_FILENAME",
    "EVIDENCE_RECEIPT_SCHEMA_VERSION",
    "EvidenceReceipt",
    "build_evidence_receipt",
    "evidence_policy_payload",
    "evidence_receipt_is_current",
    "is_native_text_source",
    "load_evidence_receipt",
    "pdf_evidence_mode",
    "require_current_consensus",
    "write_evidence_receipt",
]
