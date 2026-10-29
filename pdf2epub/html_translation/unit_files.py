"""Logical HTML translation unit inventory, split parts, and reconstructed records."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional
from loguru import logger

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
        for mapping_path in sorted(self.workspace.compressed_units_dir.glob("*.mapping.json")):
            base_stem = mapping_path.name[:-len(".mapping.json")]
            try:
                mapping = json.loads(mapping_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                mapping = {}
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
            records.append({
                "base_stem": base_stem,
                "mapping_path": mapping_path,
                "mapping": mapping,
                "source_path": source_path,
                "source_content": source_content,
                "translated_path": translated_path,
                "translated_content": translated_content,
                "translated_exists": translated_exists,
                "final_path": self.workspace.final_dir / f"{base_stem}{original_ext}",
            })
        return records


__all__ = ["HTMLUnitStore", "HTMLUnitWorkspace", "PART_FILE_RE"]
