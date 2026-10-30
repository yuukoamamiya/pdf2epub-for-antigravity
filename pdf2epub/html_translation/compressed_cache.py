"""Freshness contract for reusable compressed EPUB extraction artifacts."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Dict

from pdf2epub.workflow_contracts import atomic_write_json
from .package_identity import (
    MAPPING_SCHEMA_VERSION,
    unit_id_for_package_path,
    validate_package_relative_path,
)


class CompressedExtractionCache:
    """Check and write receipts for an EPUB's compressed extraction snapshot."""

    def __init__(self, output_dir: Path, epub_path: Path, compressed_units_dir: Path):
        self.output_dir = Path(output_dir)
        self.epub_path = Path(epub_path)
        self.compressed_units_dir = Path(compressed_units_dir)

    @property
    def path(self) -> Path:
        return self.output_dir / "html_prepare_cache.json"

    def files(self) -> Dict[str, str]:
        """Hash mappings and active files, rejecting incomplete/unsafe mappings."""
        files: Dict[str, str] = {}
        package_paths: set[str] = set()
        units_root = self.compressed_units_dir.resolve()
        for mapping_path in sorted(self.compressed_units_dir.glob("*.mapping.json")):
            try:
                mapping_path.resolve().relative_to(units_root)
            except ValueError as exc:
                raise ValueError(f"mapping escapes compressed units directory: {mapping_path.name}") from exc
            files[mapping_path.name] = hashlib.sha256(mapping_path.read_bytes()).hexdigest()
            stem = mapping_path.name[:-len(".mapping.json")]
            try:
                mapping = json.loads(mapping_path.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError) as exc:
                raise ValueError(f"invalid compressed mapping: {mapping_path.name}") from exc
            if not isinstance(mapping, dict):
                raise ValueError(f"compressed mapping must be an object: {mapping_path.name}")
            if mapping.get("mapping_schema_version") != MAPPING_SCHEMA_VERSION:
                raise ValueError(f"mapping identity schema is stale: {mapping_path.name}")
            package_path = validate_package_relative_path(
                mapping.get("package_relative_path")
            )
            if not mapping.get("legacy_unit_id") and stem != unit_id_for_package_path(package_path):
                raise ValueError(f"mapping identity does not match filename: {mapping_path.name}")
            folded_path = package_path.casefold()
            if folded_path in package_paths:
                raise ValueError(f"duplicate package path identity: {package_path}")
            package_paths.add(folded_path)
            if "translation_parts" in mapping:
                active_names = mapping["translation_parts"]
                if not isinstance(active_names, list) or not active_names:
                    raise ValueError(f"invalid translation_parts in {mapping_path.name}")
            else:
                active_names = [f"{stem}.md"]
            for name in active_names:
                if not isinstance(name, str) or Path(name).name != name or Path(name).suffix.lower() != ".md":
                    raise ValueError(f"unsafe active compressed filename in {mapping_path.name}: {name!r}")
                path = self.compressed_units_dir / name
                try:
                    path.resolve().relative_to(units_root)
                except ValueError as exc:
                    raise ValueError(f"active compressed file escapes directory: {name}") from exc
                if not path.is_file():
                    raise ValueError(f"active compressed file is missing: {name}")
                files[path.name] = hashlib.sha256(path.read_bytes()).hexdigest()
        return files

    def is_current(self, *, max_nonempty_lines: int) -> bool:
        """Return whether the receipt matches input, split limit and artifacts."""
        if not self.path.is_file() or not self.epub_path.is_file():
            return False
        try:
            cache = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(cache, dict):
                return False
            if cache.get("schema_version") != 2:
                return False
            if cache.get("input_epub_sha256") != hashlib.sha256(self.epub_path.read_bytes()).hexdigest():
                return False
            if int(cache.get("max_nonempty_lines", 0)) != int(max_nonempty_lines):
                return False
            expected = cache.get("compressed_files")
            return isinstance(expected, dict) and bool(expected) and expected == self.files()
        except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError, AttributeError):
            return False

    def write(self, *, max_nonempty_lines: int) -> None:
        """Write a receipt after extraction completes."""
        if not self.epub_path.is_file():
            return
        compressed_files = self.files()
        if not compressed_files:
            raise ValueError("cannot write compressed cache receipt without mapped files")
        atomic_write_json(self.path, {
            "schema_version": 2,
            "input_epub_sha256": hashlib.sha256(self.epub_path.read_bytes()).hexdigest(),
            "max_nonempty_lines": int(max_nonempty_lines),
            "compressed_files": compressed_files,
        })
