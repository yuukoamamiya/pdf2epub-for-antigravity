"""Validated decompression and EPUB build orchestration from workspace data."""
from __future__ import annotations

import json
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, Mapping, Optional
from loguru import logger

from pdf2epub.utils.paths import sanitize_filename
from pdf2epub.workflow_contracts import atomic_write_json
from .build_contracts import NavigationUpdateError
from .package_identity import PACKAGE_PATH_MANIFEST


@dataclass(frozen=True)
class HTMLReconstructionContext:
    output_dir: Path
    compressed_units_dir: Path
    translated_dir: Path
    final_dir: Path
    source_epub: Path
    book_title: str
    config: Mapping[str, Any]
    navigation_report: Dict[str, Any]
    validate_units: Callable[[], Dict[str, Any]]
    merge_parts: Callable[[], Dict[str, str]]
    logical_records: Callable[[Optional[Dict[str, str]]], list[dict[str, Any]]]
    write_report: Callable[..., Path]
    build_epub: Callable[..., Path]


class HTMLReconstructor:
    """Build from current validated translations without rerunning extraction."""
    def __init__(self, context: HTMLReconstructionContext):
        self.context = context

    def build(
        self,
        output_epub: Optional[Path] = None,
        allow_partial: bool = False,
        allow_navigation_warnings: bool = False,
    ) -> Path:
        from .compressor import HTMLCompressor

        context = self.context
        validation = context.validate_units()
        if not validation["all_passed"] and not allow_partial:
            metadata_errors = validation.get("metadata", {}).get("errors", [])
            detail = list(metadata_errors)
            detail.extend(
                f"{item['file']}: {item['reason']}"
                for item in validation.get("invalid", [])
            )
            detail.extend(f"missing unit: {name}" for name in validation.get("missing", []))
            raise ValueError(
                "Translation validation failed; refusing to build EPUB. "
                "Run html-validate or use --allow-partial explicitly. "
                + "; ".join(detail[:10])
            )
        if not validation["all_passed"]:
            logger.warning("Building partial EPUB because allow_partial=True")

        compressor = HTMLCompressor()
        merged_parts = context.merge_parts()
        if merged_parts:
            logger.info(f"Merged {len(merged_parts)} split files")
        records = context.logical_records(merged_parts)

        if context.final_dir.exists():
            shutil.rmtree(context.final_dir)
        context.final_dir.mkdir(parents=True, exist_ok=True)

        package_path_bindings = []
        for record in records:
            base_stem = record["base_stem"]
            if not record["translated_exists"]:
                logger.warning(f"No translation found for {base_stem}")
                continue
            if base_stem in merged_parts:
                logger.debug(f"Using merged content for {base_stem}")
            restored = compressor.decompress(record["translated_content"], record["mapping"])
            record["final_path"].parent.mkdir(parents=True, exist_ok=True)
            record["final_path"].write_text(restored, encoding="utf-8")
            if record.get("package_relative_path"):
                package_path_bindings.append({
                    "translated_path": record["final_path"].relative_to(context.final_dir).as_posix(),
                    "package_relative_path": record["package_relative_path"],
                })
            logger.debug(f"Decompressed: {base_stem}")

        if package_path_bindings:
            atomic_write_json(
                context.final_dir / PACKAGE_PATH_MANIFEST,
                {"schema_version": 1, "documents": package_path_bindings},
                indent=2,
            )

        metadata_path = context.output_dir / "translated_metadata.json"
        translated_metadata = None
        if metadata_path.exists():
            with metadata_path.open("r", encoding="utf-8") as file:
                translated_metadata = json.load(file)
        if output_epub is None:
            if translated_metadata and translated_metadata.get("translated_title"):
                safe_title = sanitize_filename(translated_metadata["translated_title"])
                output_epub = context.output_dir / f"{safe_title}.epub"
            else:
                output_epub = context.output_dir / f"translated_{context.source_epub.name}"

        translation_config = context.config.get("html_translation", {})
        try:
            result_path = context.build_epub(
                original_epub=context.source_epub,
                translated_dir=context.final_dir,
                output_path=output_epub,
                book_title=context.book_title,
                translated_metadata=translated_metadata,
                navigation_report=context.navigation_report,
                epubcheck_mode=translation_config.get("epubcheck_mode", "warn"),
                epubcheck_path=translation_config.get("epubcheck_path"),
                allow_navigation_warnings=allow_navigation_warnings,
            )
        except NavigationUpdateError:
            context.write_report(output_epub=output_epub, phase="build_blocked")
            raise
        context.write_report(output_epub=result_path, phase="build")
        return result_path


__all__ = ["HTMLReconstructionContext", "HTMLReconstructor"]
