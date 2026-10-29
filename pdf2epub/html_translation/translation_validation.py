"""Single-unit and full-book HTML translation validation and checkpointing."""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from pdf2epub.document_roles import infer_translation_file_role, is_density_exempt_role
from pdf2epub.glossary import validate_translation_context
from pdf2epub.markdown_validation import target_language_ratio_check
from pdf2epub.special_role_audit import validate_special_role_markers
from pdf2epub.subagent_safety import detect_refusal
from pdf2epub.workflow_contracts import atomic_write_json
from .validation import nonempty_lines, tag_mismatch_count
from .metadata_validation import HTMLMetadataValidator, MetadataValidationWorkspace
from .unit_files import HTMLUnitStore, HTMLUnitWorkspace


def validate_file_name(file_name: Optional[str]) -> Optional[str]:
    """Reject any non-basename or non-Markdown unit before workspace access."""
    if file_name is None:
        return None
    requested = str(file_name)
    candidate = Path(requested)
    if (
        candidate.name != requested
        or candidate.suffix.lower() != ".md"
        or requested in {".", ".."}
    ):
        raise ValueError(
            "file_name must be a direct .md filename inside compressed_units"
        )
    return requested


@dataclass(frozen=True)
class HTMLValidationWorkspace:
    output_dir: Path
    compressed_units_dir: Path
    translated_dir: Path
    config: Dict[str, Any]


class HTMLTranslationValidator:
    """Validate file/book checkpoints without invoking EPUB extraction or build."""
    def __init__(self, workspace: HTMLValidationWorkspace):
        self.output_dir = Path(workspace.output_dir)
        self.compressed_units_dir = Path(workspace.compressed_units_dir)
        self.translated_dir = Path(workspace.translated_dir)
        self.config = workspace.config

    def _unit_store(self):
        return HTMLUnitStore(HTMLUnitWorkspace(self.compressed_units_dir, self.translated_dir, self.output_dir / "final_xhtml"))

    def declared_translation_files(self):
        return self._unit_store().declared_translation_files()

    def validate_translated_metadata(self):
        return HTMLMetadataValidator(MetadataValidationWorkspace(self.output_dir)).validate()

    def _declared_html_source_names(self) -> List[str]:
        """Load the manifest inventory, falling back to local mappings."""
        manifest_path = self.output_dir / "translate-html_subagent_manifest.json"
        if manifest_path.is_file():
            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                names = manifest.get("files")
                if isinstance(names, list) and all(isinstance(name, str) for name in names):
                    # A manifest is local workflow data, but it can be left
                    # behind by an interrupted or manually edited run. Keep
                    # path traversal names out of the source/target resolver;
                    # the validator will report the resulting inventory
                    # mismatch instead of reading outside compressed_units.
                    return list(
                        dict.fromkeys(
                            name
                            for name in names
                            if Path(name).name == name
                            and Path(name).suffix.lower() == ".md"
                        )
                    )
            except (OSError, json.JSONDecodeError, AttributeError, TypeError):
                pass
        names = self.declared_translation_files()
        return names or sorted(
            path.name for path in self.compressed_units_dir.glob("*.md") if path.is_file()
        )
    def validate(self, file_name: Optional[str] = None) -> Dict[str, Any]:
        """
        Validate translated compressed units against their sources.

        When ``file_name`` is supplied, only that source/target pair is checked.
        Single-file validation deliberately skips book-level metadata and
        completeness checks so a Subagent can validate a file immediately after
        writing it.  The returned ``scope`` and ``book_complete`` fields make
        that distinction explicit.

        Checks:
        1. Translated file exists
        2. Non-empty line count matches original
        3. HTML tag sequence matches on each line
        4. Target language content exists (Chinese characters)
        5. Content omission checks

        Args:
            file_name: Optional source filename, restricted to a direct ``.md``
                basename inside ``compressed_units``.

        Returns:
            Dict summary with validation counts and status fields.
        """
        scope = "file" if file_name is not None else "book"
        requested_file = validate_file_name(file_name)
        missing = []
        declared_names = self._declared_html_source_names()
        mapping_names = self.declared_translation_files()
        manifest_path = self.output_dir / "translate-html_subagent_manifest.json"
        manifest: Dict[str, Any] = {}
        manifest_file_roles: Dict[str, str] = {}
        snapshot_errors: List[str] = []
        snapshot_expected_hashes: Dict[str, str] = {}
        if manifest_path.is_file():
            try:
                loaded_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                if isinstance(loaded_manifest, dict):
                    manifest = loaded_manifest
                    raw_roles = manifest.get("file_roles", {})
                    if isinstance(raw_roles, dict):
                        manifest_file_roles = {
                            str(name): str(role).strip().lower()
                            for name, role in raw_roles.items()
                            if str(name).strip() and str(role).strip()
                        }
                else:
                    snapshot_errors.append("HTML translation manifest is not a JSON object")
            except (OSError, UnicodeError, json.JSONDecodeError) as exc:
                snapshot_errors.append(f"invalid HTML translation manifest: {exc}")
        elif file_name is None:
            # Direct object-level tests and pre-contract diagnostic runs may
            # not have a hand-off yet.  A real full-book workflow always has
            # one because html-prepare is the source of the inventory.
            snapshot_errors.append("translate-html_subagent_manifest.json is missing")

        if manifest:
            manifest_files = manifest.get("files")
            if not isinstance(manifest_files, list) or not all(
                isinstance(name, str) for name in manifest_files
            ):
                snapshot_errors.append("HTML manifest files inventory is invalid")
                manifest_files = []
            manifest_files = list(dict.fromkeys(manifest_files))
            if set(manifest_files) != set(mapping_names):
                snapshot_errors.append(
                    "HTML mapping inventory differs from the Subagent manifest"
                )
            expected_hashes = manifest.get("source_sha256")
            if not isinstance(expected_hashes, dict):
                snapshot_errors.append("HTML manifest has no source_sha256 snapshot")
                expected_hashes = {}
            snapshot_expected_hashes = {
                str(name): str(value)
                for name, value in expected_hashes.items()
                if isinstance(name, str) and isinstance(value, str)
            }
            if set(snapshot_expected_hashes) != set(manifest_files):
                snapshot_errors.append(
                    "HTML source_sha256 inventory differs from the manifest files"
                )
            canonical_input = self.output_dir / "input.epub"
            expected_input_hash = manifest.get("input_epub_sha256")
            if not isinstance(expected_input_hash, str) or not expected_input_hash:
                snapshot_errors.append("HTML manifest has no input EPUB snapshot")
            elif not canonical_input.is_file():
                snapshot_errors.append("canonical output/input.epub is missing")
            elif hashlib.sha256(canonical_input.read_bytes()).hexdigest() != expected_input_hash:
                snapshot_errors.append("input EPUB changed after html-prepare")

        source_snapshot = {
            "valid": not snapshot_errors,
            "manifest": str(manifest_path),
            "expected_files": sorted(snapshot_expected_hashes),
            "actual_files": sorted(mapping_names),
            "errors": snapshot_errors,
        }
        if file_name is None:
            all_sources = [
                self.compressed_units_dir / name
                for name in declared_names
                if (self.compressed_units_dir / name).is_file()
            ]
            missing.extend(
                name for name in declared_names
                if not (self.compressed_units_dir / name).is_file()
            )
        else:
            source_path = self.compressed_units_dir / requested_file
            all_sources = [source_path] if requested_file in declared_names and source_path.is_file() else []
            if requested_file not in declared_names or not all_sources:
                missing.append(requested_file)

        total = len(declared_names) if file_name is None else 1
        invalid = []
        safety_blocked = []
        valid = 0
        valid_files = []
        source_sha256 = {}
        target_sha256 = {}
        target_language_audits: Dict[str, Dict[str, Any]] = {}
        special_role_groups: Dict[str, Dict[str, Any]] = {}
        target_language = (
            (getattr(self, "config", {}) or {}).get("translation", {}) or {}
        ).get("target_language", "Chinese")

        for src_file in all_sources:
            tgt_file = self.translated_dir / src_file.name
            if not tgt_file.exists():
                missing.append(src_file.name)
                continue

            src_content = src_file.read_text(encoding="utf-8")
            tgt_content = tgt_file.read_text(encoding="utf-8")
            role = manifest_file_roles.get(src_file.name) or infer_translation_file_role(
                src_file.name, src_content
            )
            if role:
                logical_stem = re.sub(
                    r"\.part\d+$", "", src_file.stem, flags=re.IGNORECASE
                )
                group = special_role_groups.setdefault(
                    logical_stem,
                    {"role": role, "sources": [], "targets": [], "files": []},
                )
                group["role"] = role
                group["sources"].append(src_content)
                group["targets"].append(tgt_content)
                group["files"].append(src_file.name)
            source_sha256[src_file.name] = hashlib.sha256(src_file.read_bytes()).hexdigest()
            target_sha256[src_file.name] = hashlib.sha256(tgt_file.read_bytes()).hexdigest()

            expected_source_hash = snapshot_expected_hashes.get(src_file.name)
            if expected_source_hash and source_sha256[src_file.name] != expected_source_hash:
                invalid.append({
                    "file": src_file.name,
                    "reason": "source changed after html-prepare",
                })
                continue

            refusal = detect_refusal(src_content, tgt_content)
            if refusal:
                invalid.append({
                    "file": src_file.name,
                    "reason": f"refusal/disclaimer detected: {refusal}",
                })
                safety_blocked.append(src_file.name)
                continue

            src_lines = nonempty_lines(src_content)
            tgt_lines = nonempty_lines(tgt_content)
            if len(src_lines) != len(tgt_lines):
                invalid.append({
                    "file": src_file.name,
                    "reason": f"Line count mismatch: expected {len(src_lines)}, got {len(tgt_lines)}",
                })
                continue
            mismatches = tag_mismatch_count(src_lines, tgt_lines)
            if mismatches:
                invalid.append({
                    "file": src_file.name,
                    "reason": f"HTML tag structure mismatch in {mismatches} line(s)",
                })
                continue
            if not tgt_content.strip() and src_content.strip():
                invalid.append({"file": src_file.name, "reason": "target is empty"})
                continue
            if "```" in tgt_content:
                invalid.append({"file": src_file.name, "reason": "Markdown code fence is not allowed"})
                continue
            if is_density_exempt_role(role):
                language_audit = {
                    "applicable": False,
                    "blocked": False,
                    "exempt": True,
                    "role": role,
                    "reason": "special unit role is exempt from prose density gate",
                }
            else:
                language_audit = target_language_ratio_check(
                    src_content,
                    tgt_content,
                    target_language,
                )
            target_language_audits[src_file.name] = language_audit
            if language_audit.get("blocked"):
                invalid.append({
                    "file": src_file.name,
                    "reason": language_audit.get(
                        "reason", "target-language audit failed"
                    ),
                })
                continue
            else:
                valid += 1
                valid_files.append(src_file.name)

        for group in special_role_groups.values():
            role = str(group.get("role") or "").strip().lower()
            if not role:
                continue
            marker_errors = validate_special_role_markers(
                "\n".join(group["sources"]),
                "\n".join(group["targets"]),
                role,
            )
            if marker_errors:
                file_name = group["files"][0]
                invalid.extend(
                    {"file": file_name, "reason": error}
                    for error in marker_errors
                )
                for name in group["files"]:
                    if name in valid_files:
                        valid_files.remove(name)

        if file_name is None:
            metadata_report = self.validate_translated_metadata()
            context_report = validate_translation_context(
                self.output_dir,
                "translate-html_subagent_manifest.json",
                getattr(self, "config", {}),
            )
        else:
            metadata_report = {
                "valid": True,
                "skipped": True,
                "reason": "single-file validation scope",
            }
            context_report = {
                "valid": True,
                "skipped": True,
                "reason": "single-file validation scope",
            }

        all_passed = (
            len(missing) == 0
            and len(invalid) == 0
            and total > 0
            and source_snapshot["valid"]
            and metadata_report["valid"]
            and context_report["valid"]
        )

        return {
            "scope": scope,
            "file": requested_file,
            "total": total,
            "completed": total - len(missing),
            "valid": valid,
            "invalid": invalid,
            "safety_blocked": safety_blocked,
            "missing": missing,
            "valid_files": valid_files,
            "source_sha256": source_sha256,
            "target_sha256": target_sha256,
            "source_snapshot": source_snapshot,
            "target_language_audits": target_language_audits,
            "target_language_blocked": sorted(
                name
                for name, audit in target_language_audits.items()
                if audit.get("blocked")
            ),
            "declared_files": declared_names,
            "ignored_source_files": sorted(
                path.name for path in self.compressed_units_dir.glob("*.md")
                if path.name not in set(declared_names)
            ),
            "metadata": metadata_report,
            "translation_context": context_report,
            "all_passed": all_passed,
            "book_complete": all_passed if scope == "book" else False,
        }
    def persist_checkpoint(self, report: Dict[str, Any]) -> None:
        """Merge an HTML single-file result into the resume ledger."""
        path = self.output_dir / "translate-html_file_validation.json"
        records: Dict[str, Any] = {}
        if path.is_file():
            try:
                loaded = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict) and isinstance(loaded.get("files"), dict):
                    records = loaded["files"]
            except (OSError, json.JSONDecodeError, AttributeError, TypeError):
                records = {}
        for name in report.get("files_checked", [report.get("file")]):
            if not name:
                continue
            records[str(name)] = {
                "valid": str(name) in report.get("valid_files", []) and not report.get("missing"),
                "source_sha256": report.get("source_sha256", {}).get(str(name)),
                "target_sha256": report.get("target_sha256", {}).get(str(name)),
                "validated_at": datetime.now(timezone.utc).isoformat(),
                "invalid": [item for item in report.get("invalid", []) if item.get("file") == str(name)],
                "safety_blocked": str(name) in report.get("safety_blocked", []),
            }
        atomic_write_json(
            path,
            {"task": "translate-html", "scope": "file-checkpoints", "files": records},
        )
