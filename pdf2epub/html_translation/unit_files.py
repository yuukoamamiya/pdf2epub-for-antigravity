"""Logical HTML translation unit inventory, split parts, and reconstructed records."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Dict, List, Optional
from loguru import logger
from .package_identity import unit_id_for_package_path, validate_package_relative_path

PART_FILE_RE = re.compile(r'^(.+)\.part(\d+)\.md$')


@dataclass(frozen=True)
class HTMLUnitWorkspace:
    compressed_units_dir: Path
    translated_dir: Path
    final_dir: Path


class HTMLUnitStore:
    """Resolve logical units from mapping files and merge ordered part files."""
    def __init__(self, workspace: HTMLUnitWorkspace):
        self.workspace = workspace

    def declared_translation_files(self) -> List[str]:
        names = []
        for mapping_path in sorted(self.workspace.compressed_units_dir.glob("*.mapping.json")):
            stem = mapping_path.name[:-len(".mapping.json")]
            try:
                mapping = json.loads(mapping_path.read_text(encoding="utf-8"))
                raw_parts = mapping.get("translation_parts", [])
                parts = [
                    str(name) for name in raw_parts
                    if isinstance(raw_parts, list)
                    and Path(str(name)).name == str(name)
                    and Path(str(name)).suffix.lower() == ".md"
                ]
            except (OSError, UnicodeError, json.JSONDecodeError, TypeError, AttributeError):
                parts = []
            if parts:
                names.extend(parts)
                continue
            source_path = self.workspace.compressed_units_dir / f"{stem}.md"
            if source_path.is_file():
                names.append(source_path.name)
        return names

    @staticmethod
    def group_part_files(directory: Path) -> Dict[str, List[tuple[int, Path]]]:
        from collections import defaultdict
        part_files: Dict[str, List[tuple[int, Path]]] = defaultdict(list)
        for file_path in Path(directory).glob("*.part*.md"):
            match = PART_FILE_RE.match(file_path.name)
            if match:
                part_files[match.group(1)].append((int(match.group(2)), file_path))
        for parts in part_files.values():
            parts.sort(key=lambda item: item[0])
        return dict(part_files)

    @classmethod
    def merge_part_files_in_dir(cls, directory: Path) -> Dict[str, str]:
        return {
            base_name: '\n'.join(part.read_text(encoding='utf-8') for _number, part in parts)
            for base_name, parts in cls.group_part_files(directory).items()
        }

    def merge_translated_part_files(self) -> Dict[str, str]:
        merged = self.merge_part_files_in_dir(self.workspace.translated_dir)
        for base_name, parts in self.group_part_files(self.workspace.translated_dir).items():
            logger.debug(f"Merged {len(parts)} parts for {base_name}")
        return merged

    def logical_unit_records(self, merged_translated: Optional[Dict[str, str]] = None) -> List[Dict[str, Any]]:
        merged_translated = merged_translated or {}
        merged_sources = self.merge_part_files_in_dir(self.workspace.compressed_units_dir)
        records = []
        package_paths = set()
        final_paths = set()
        for mapping_path in sorted(self.workspace.compressed_units_dir.glob("*.mapping.json")):
            base_stem = mapping_path.name[:-len(".mapping.json")]
            try:
                mapping = json.loads(mapping_path.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError) as exc:
                raise ValueError(f"Invalid compressed mapping {mapping_path.name}: {exc}") from exc
            if not isinstance(mapping, dict):
                raise ValueError(f"Compressed mapping must be an object: {mapping_path.name}")
            package_path = None
            if "package_relative_path" in mapping:
                package_path = validate_package_relative_path(
                    mapping.get("package_relative_path")
                )
                if not mapping.get("legacy_unit_id") and base_stem != unit_id_for_package_path(package_path):
                    raise ValueError(
                        f"Mapping identity does not match filename: {mapping_path.name}"
                    )
                folded_path = package_path.casefold()
                if folded_path in package_paths:
                    raise ValueError(f"Duplicate package-relative document path: {package_path}")
                package_paths.add(folded_path)
            source_path = self.workspace.compressed_units_dir / f"{base_stem}.md"
            translated_path = self.workspace.translated_dir / f"{base_stem}.md"
            source_content = ""
            has_translation_parts = isinstance(mapping.get("translation_parts"), list) and bool(mapping.get("translation_parts"))
            if not has_translation_parts and source_path.exists():
                source_content = source_path.read_text(encoding="utf-8")
            elif base_stem in merged_sources:
                source_content = merged_sources[base_stem]
            translated_content = ""
            translated_exists = False
            if has_translation_parts and base_stem in merged_translated:
                translated_content = merged_translated[base_stem]
                translated_exists = True
            elif translated_path.exists():
                translated_content = translated_path.read_text(encoding="utf-8")
                translated_exists = True
            original_ext = mapping.get("original_extension", ".xhtml")
            if not isinstance(original_ext, str) or original_ext.casefold() not in {
                ".xhtml", ".html", ".htm"
            }:
                raise ValueError(
                    f"Unsafe original_extension in mapping {mapping_path.name}: {original_ext!r}"
                )
            if package_path is not None:
                if str(original_ext).casefold() != PurePosixPath(package_path).suffix.casefold():
                    raise ValueError(
                        f"Mapping extension differs from package path: {mapping_path.name}"
                    )
                final_path = self.workspace.final_dir.joinpath(*PurePosixPath(package_path).parts)
            else:
                # Legacy mapping compatibility: packager will resolve this
                # basename only when it identifies one target in the EPUB.
                final_path = self.workspace.final_dir / f"{base_stem}{original_ext}"
            final_root = self.workspace.final_dir.resolve()
            try:
                final_path.resolve().relative_to(final_root)
            except ValueError as exc:
                raise ValueError(
                    f"Reconstructed XHTML path escapes final directory: {final_path}"
                ) from exc
            folded_final = str(final_path.relative_to(self.workspace.final_dir)).replace("\\", "/").casefold()
            if folded_final in final_paths:
                raise ValueError(f"Multiple translation units reconstruct to the same path: {final_path}")
            final_paths.add(folded_final)
            records.append({
                "base_stem": base_stem,
                "mapping_path": mapping_path,
                "mapping": mapping,
                "source_path": source_path,
                "source_content": source_content,
                "translated_path": translated_path,
                "translated_content": translated_content,
                "translated_exists": translated_exists,
                "final_path": final_path,
                "package_relative_path": package_path,
            })
        return records


__all__ = ["HTMLUnitStore", "HTMLUnitWorkspace", "PART_FILE_RE"]
