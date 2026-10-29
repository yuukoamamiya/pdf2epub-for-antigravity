"""
HTML EPUB Builder.

Rebuilds EPUB by replacing translated XHTML content while preserving
all other resources (CSS, fonts, images, metadata).
"""

import tempfile
import json
import shutil
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Set
from loguru import logger

from .epub_parser import EPUBParser, EPUBResourceNotFoundError
from .compressed_cache import CompressedExtractionCache
from .unit_files import PART_FILE_RE
from pdf2epub.document_roles import infer_translation_file_role


from .build_contracts import (
    BuildConfig,
    NavigationUpdateError,
)
from .package_paths import (
    UnsafePackagePath,
    _package_relative_href,
    _safe_existing_package_file,
    _safe_lxml_parser,
    _safe_package_path,
    _has_local_tag,
    _toc_entry_href,
)
from .metadata_values import (
    _make_json_safe,
    sort_author_for_library,
    sort_title_for_library,
)


class HTMLEpubBuilder:
    """
    Build translated EPUB by replacing XHTML content.

    Process:
    1. Extract original EPUB to temp directory
    2. Replace XHTML files with translated versions
    3. Repackage as new EPUB

    Preserves:
    - mimetype (uncompressed, first file)
    - META-INF/container.xml
    - content.opf (manifest, spine, metadata)
    - toc.ncx / nav.xhtml (navigation)
    - CSS, fonts, images
    """

    def __init__(self, config: BuildConfig):
        self.config = config
        self.original_epub = config.original_epub
        self.translated_dir = config.translated_dir
        self.output_path = config.output_path
        self.navigation_report = config.navigation_report if config.navigation_report is not None else {}
        self._reset_navigation_report()

    def _reset_navigation_report(self) -> None:
        """Reset NCX/nav diagnostics for one build attempt."""
        self.navigation_report.clear()
        self.navigation_report.update(
            {
                "ncx": {"status": "not_attempted", "path": None, "updated_entries": 0},
                "nav": {"status": "not_attempted", "path": None, "updated_entries": 0},
                "build_blocked": False,
                "navigation_warnings_acknowledged": False,
            }
        )

    def _navigation_warnings(self) -> List[str]:
        """Return navigation components that failed to update."""
        return [
            kind
            for kind, result in self.navigation_report.items()
            if kind in {"ncx", "nav"}
            and isinstance(result, dict)
            and result.get("status") == "warning"
        ]

    def _record_navigation(
        self,
        kind: str,
        status: str,
        path: Optional[Path] = None,
        extract_dir: Optional[Path] = None,
        updated_entries: int = 0,
        error: Optional[str] = None,
    ) -> None:
        """Delegate structured navigation diagnostics to their service."""
        from .package_navigation import record_navigation

        record_navigation(
            self.navigation_report,
            kind,
            status,
            path,
            extract_dir,
            updated_entries,
            error,
        )

    def build(self) -> Path:
        """
        Build the translated EPUB.

        Returns:
            Path to the output EPUB file
        """
        logger.info(f"Building translated EPUB from {self.original_epub}")
        self._reset_navigation_report()

        # Create temp directory for extraction
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            extract_dir = temp_path / "epub_content"

            # Step 1: Extract original EPUB
            self._extract_epub(extract_dir)

            # Step 2: Find and replace XHTML files
            replaced_count = self._replace_xhtml_files(extract_dir)
            logger.info(f"Replaced {replaced_count} XHTML files")

            # Step 3: Update metadata if provided
            if self.config.translated_metadata:
                self._update_content_opf(extract_dir, self.config.translated_metadata)
                self._update_toc_ncx(extract_dir, self.config.translated_metadata)
                self._update_nav_xhtml(extract_dir, self.config.translated_metadata)

                navigation_warnings = self._navigation_warnings()
                if navigation_warnings:
                    acknowledged = bool(self.config.allow_navigation_warnings)
                    self.navigation_report["build_blocked"] = not acknowledged
                    self.navigation_report[
                        "navigation_warnings_acknowledged"
                    ] = acknowledged
                    warning_text = ", ".join(navigation_warnings)
                    if not acknowledged:
                        raise NavigationUpdateError(
                            "Translated EPUB navigation update failed for "
                            f"{warning_text}; refusing to package. "
                            "Re-run with --allow-navigation-warnings only after "
                            "reviewing the navigation report."
                        )

            # Step 4: Normalize fragile source CSS before packaging
            self._normalize_css(extract_dir)

            # Step 5: Repackage as EPUB
            self._package_epub(extract_dir)

        self._validate_output_epub()
        logger.info(f"Built EPUB: {self.output_path}")
        return self.output_path

    def _validate_output_epub(self) -> None:
        from .package_resources import validate_output_epub
        return validate_output_epub(
            self.output_path,
            self.config.epubcheck_mode,
            self.config.epubcheck_path,
        )


    def _extract_epub(self, extract_dir: Path):
        from .package_resources import extract_epub
        return extract_epub(self.original_epub, extract_dir)


    def _replace_xhtml_files(self, extract_dir: Path) -> int:
        from .package_resources import EPUBFilePackager
        return EPUBFilePackager(self.output_path, self.translated_dir).replace_xhtml_files(extract_dir)


    def _replace_file(self, target: Path, source: Path):
        from .package_resources import EPUBFilePackager
        return EPUBFilePackager(self.output_path, self.translated_dir).replace_file(target, source)


    def _normalize_css(self, extract_dir: Path):
        from .package_resources import EPUBFilePackager
        return EPUBFilePackager(self.output_path, self.translated_dir).normalize_css(extract_dir)


    @staticmethod
    def _class_names(attrs: str) -> List[str]:
        from .package_resources import EPUBFilePackager
        return EPUBFilePackager._class_names(attrs)


    @classmethod
    def _classes_used_as_inline_block_wrappers(cls, extract_dir: Path) -> Set[str]:
        from .package_resources import EPUBFilePackager
        return EPUBFilePackager._classes_used_as_inline_block_wrappers(extract_dir)


    @staticmethod
    def _normalize_css_content(content: str, block_wrapper_classes: Set[str]) -> str:
        from .package_resources import EPUBFilePackager
        return EPUBFilePackager._normalize_css_content(content, block_wrapper_classes)


    def _package_epub(self, content_dir: Path):
        from .package_resources import EPUBFilePackager
        return EPUBFilePackager(self.output_path, self.translated_dir).package_epub(content_dir)


    def _find_opf_path(self, extract_dir):
        from .package_locator import find_opf_path
        return find_opf_path(extract_dir)


    def _find_toc_files(self, extract_dir, opf_path):
        from .package_locator import find_toc_files
        return find_toc_files(extract_dir, opf_path)


    def _update_content_opf(self, extract_dir: Path, metadata: Dict):
        from .package_metadata import EPUBMetadataUpdater, PackageMetadataContext
        return EPUBMetadataUpdater(
            PackageMetadataContext(
                config=self.config,
                find_opf_path=self._find_opf_path,
            )
        ).update_opf(extract_dir, metadata)


    def _update_toc_ncx(self, extract_dir: Path, metadata: Dict):
        from .package_navigation import EPUBNavigationUpdater, NavigationUpdateContext
        context = NavigationUpdateContext(
            navigation_report=self.navigation_report,
            find_opf_path=self._find_opf_path,
            find_toc_files=self._find_toc_files,
        )
        return EPUBNavigationUpdater(context).update_toc_ncx(extract_dir, metadata)


    def _update_nav_xhtml(self, extract_dir: Path, metadata: Dict):
        from .package_navigation import EPUBNavigationUpdater, NavigationUpdateContext
        context = NavigationUpdateContext(
            navigation_report=self.navigation_report,
            find_opf_path=self._find_opf_path,
            find_toc_files=self._find_toc_files,
        )
        return EPUBNavigationUpdater(context).update_nav_xhtml(extract_dir, metadata)



def build_html_epub(
    original_epub: Path,
    translated_dir: Path,
    output_path: Optional[Path] = None,
    book_title: Optional[str] = None,
    translated_metadata: Optional[Dict] = None,
    epubcheck_mode: str = "warn",
    epubcheck_path: Optional[str] = None,
    navigation_report: Optional[Dict[str, Any]] = None,
    allow_navigation_warnings: bool = False,
) -> Path:
    """
    Convenience function to build translated EPUB.

    Args:
        original_epub: Path to original EPUB file
        translated_dir: Directory containing translated XHTML files
        output_path: Optional output path (defaults to translated_{original}.epub)
        book_title: Optional book title
        translated_metadata: Optional dict with translated_title and toc entries
        epubcheck_mode: Final validation mode: off, warn, or strict
        epubcheck_path: Optional explicit path to the EPUBCheck executable
        allow_navigation_warnings: Explicitly acknowledge NCX/nav update warnings

    Returns:
        Path to the built EPUB file
    """
    if output_path is None:
        output_path = original_epub.parent / f"translated_{original_epub.name}"

    config = BuildConfig(
        original_epub=original_epub,
        translated_dir=translated_dir,
        output_path=output_path,
        book_title=book_title or original_epub.stem,
        translated_metadata=translated_metadata,
        navigation_report=navigation_report,
        epubcheck_mode=epubcheck_mode,
        epubcheck_path=epubcheck_path,
        allow_navigation_warnings=allow_navigation_warnings,
    )

    builder = HTMLEpubBuilder(config)
    return builder.build()


class HTMLEpubPipeline:
    """
    Complete HTML translation pipeline using HTMLCompressor.

    Orchestrates the full workflow:
    1. Extract XHTML from EPUB
    2. Compress HTML (strip outer structure, save mapping)
    3. Translate compressed content (one line per translation unit)
    4. Translate title and TOC
    5. Decompress (reconstruct full HTML from translated lines + mapping)
    6. Build new EPUB

    Directory structure:
    - compressed_units/  : .md (compressed content) + .mapping.json
    - translated_compressed/ : .md (translated lines)
    - final_xhtml/ : .xhtml (fully reconstructed HTML)
    """

    def __init__(
        self,
        epub_path: Path,
        output_dir: Path,
        config: Dict
    ):
        self.epub_path = epub_path
        self.output_dir = output_dir
        self.config = config

        # Parse EPUB
        self.parser = EPUBParser(str(epub_path))

        # Extract metadata
        self.metadata = self.parser.metadata
        self.book_title = self.metadata.get('title', epub_path.stem)
        self.source_language = self._detect_language()

        # Setup directories (using new compressor-based workflow)
        self.compressed_units_dir = output_dir / "compressed_units"  # .md + .mapping.json
        self.translated_dir = output_dir / "translated_compressed"   # .md
        self.final_dir = output_dir / "final_xhtml"                  # .xhtml
        self.navigation_report: Dict[str, Any] = {}

        for d in [self.compressed_units_dir, self.translated_dir, self.final_dir]:
            d.mkdir(parents=True, exist_ok=True)

    def _detect_language(self) -> str:
        from .metadata_preparation import detect_source_language
        return detect_source_language(self.metadata)


    def _unit_store(self):
        from .unit_files import HTMLUnitStore, HTMLUnitWorkspace
        translated_dir = Path(getattr(self, "translated_dir", self.output_dir / "translated_compressed"))
        final_dir = Path(getattr(self, "final_dir", self.output_dir / "final_xhtml"))
        return HTMLUnitStore(HTMLUnitWorkspace(
            compressed_units_dir=Path(self.compressed_units_dir),
            translated_dir=translated_dir,
            final_dir=final_dir,
        ))

    def declared_translation_files(self) -> List[str]:
        """Return the canonical translation inventory from mapping files."""
        return self._unit_store().declared_translation_files()


    def translation_file_roles(self, names: Optional[List[str]] = None) -> Dict[str, str]:
        """Return conservative special-unit roles for HTML hand-offs."""
        names = names if names is not None else self.declared_translation_files()
        roles = {}
        for name in names:
            role = infer_translation_file_role(name)
            if role:
                roles[str(name)] = role
        return roles

    def _get_language_code(self, language: str) -> str:
        from .metadata_preparation import get_language_code
        return get_language_code(language)


    def extract_and_preprocess(self, target_language=None, translation_context=None) -> int:
        from .extraction import ExtractionWorkspace, HTMLExtractionPreparer
        workspace = ExtractionWorkspace(
            epub_path=Path(self.epub_path), output_dir=Path(self.output_dir),
            compressed_units_dir=Path(self.compressed_units_dir), parser=self.parser,
            config=getattr(self, "config", {}), metadata=getattr(self, "metadata", {}),
            source_language=getattr(self, "source_language", "English"),
            book_title=getattr(self, "book_title", Path(self.epub_path).stem),
            create_metadata_source=self.create_metadata_translation_source,
        )
        return HTMLExtractionPreparer(workspace).prepare(target_language, translation_context)


    def _compressed_cache_service(self):
        return CompressedExtractionCache(
            self.output_dir, self.epub_path, self.compressed_units_dir
        )

    def _compressed_cache_path(self) -> Path:
        return self._compressed_cache_service().path

    def _compressed_cache_files(self) -> Dict[str, str]:
        """Compatibility wrapper for the compressed extraction cache service."""
        return self._compressed_cache_service().files()

    def _compressed_cache_is_current(self, *, max_nonempty_lines: int) -> bool:
        """Compatibility wrapper for extraction receipt freshness checks."""
        return self._compressed_cache_service().is_current(
            max_nonempty_lines=max_nonempty_lines
        )

    def _write_compressed_cache(self, *, max_nonempty_lines: int) -> None:
        """Compatibility wrapper to write the extraction receipt."""
        self._compressed_cache_service().write(
            max_nonempty_lines=max_nonempty_lines
        )

    def create_metadata_translation_source(self, target_language=None, context_files=None) -> Path:
        from .metadata_preparation import MetadataPreparationInput, MetadataTranslationPreparer
        inputs = MetadataPreparationInput(
            output_dir=Path(self.output_dir), parser=self.parser,
            config=getattr(self, "config", {}), metadata=self.metadata,
            source_language=self.source_language, book_title=self.book_title,
        )
        return MetadataTranslationPreparer(inputs).prepare(target_language, context_files)


    @staticmethod
    def _metadata_translation_prompt(source_filename: str, model: str = "gemini-3.1-pro-preview") -> str:
        from .metadata_preparation import MetadataTranslationPreparer
        return MetadataTranslationPreparer._metadata_translation_prompt(source_filename, model)


    def _merge_part_files(self) -> Dict[str, str]:
        """Compatibility wrapper for ordered translated-part merging."""
        return self._unit_store().merge_translated_part_files()


    @staticmethod
    def _group_part_files(directory: Path) -> Dict[str, List[tuple[int, Path]]]:
        from .unit_files import HTMLUnitStore
        return HTMLUnitStore.group_part_files(directory)


    @classmethod
    def _merge_part_files_in_dir(cls, directory: Path) -> Dict[str, str]:
        from .unit_files import HTMLUnitStore
        return HTMLUnitStore.merge_part_files_in_dir(directory)


    def _logical_unit_records(self, merged_translated: Optional[Dict[str, str]] = None) -> List[Dict[str, Any]]:
        """Compatibility wrapper around the logical unit store."""
        return self._unit_store().logical_unit_records(merged_translated)


    def _report_service(self, metadata_validation):
        from .reporting import HTMLReportContext, HTMLTranslationReportAggregator
        from .unit_files import HTMLUnitWorkspace
        return HTMLTranslationReportAggregator(HTMLReportContext(
            output_dir=Path(self.output_dir),
            source_epub=Path(self.epub_path),
            book_title=getattr(self, "book_title", Path(self.epub_path).stem),
            navigation_report=getattr(self, "navigation_report", {}),
            units=HTMLUnitWorkspace(
                Path(self.compressed_units_dir), Path(self.translated_dir), Path(getattr(self, "final_dir", self.output_dir / "final_xhtml"))
            ),
            metadata_validation=metadata_validation,
        ))

    @staticmethod
    def _read_jsonl(path: Path) -> List[Dict[str, Any]]:
        from .reporting import HTMLTranslationReportAggregator
        return HTMLTranslationReportAggregator._read_jsonl(path)


    def _trace_stats_for_file(self, trace_rows, file_stem):
        return self._report_service({})._trace_stats_for_file(trace_rows, file_stem)


    def _agent_stats_for_file(self, file_stem):
        return self._report_service({})._agent_stats_for_file(file_stem)


    def write_translation_report(self, output_epub: Optional[Path] = None, phase: str = "translation") -> Path:
        metadata_validation = self.validate_translated_metadata()
        return self._report_service(metadata_validation).write(output_epub, phase)


    def validate_translated_units(self, file_name: Optional[str] = None) -> Dict[str, Any]:
        from .translation_validation import validate_file_name
        validate_file_name(file_name)
        return self._validation_service().validate(file_name)


    def _validation_service(self):
        from .translation_validation import HTMLTranslationValidator, HTMLValidationWorkspace
        return HTMLTranslationValidator(HTMLValidationWorkspace(
            output_dir=Path(self.output_dir),
            compressed_units_dir=Path(self.compressed_units_dir),
            translated_dir=Path(self.translated_dir),
            config=getattr(self, "config", {}),
        ))

    def _declared_html_source_names(self) -> List[str]:
        return self._validation_service()._declared_html_source_names()


    def persist_file_validation_checkpoint(self, report: Dict[str, Any]) -> None:
        self._validation_service().persist_checkpoint(report)


    def validate_translated_metadata(self) -> Dict[str, Any]:
        from .metadata_validation import HTMLMetadataValidator, MetadataValidationWorkspace
        return HTMLMetadataValidator(
            MetadataValidationWorkspace(Path(self.output_dir))
        ).validate()


    def postprocess_and_build(self, output_epub=None, allow_partial=False, allow_navigation_warnings=False) -> Path:
        from .reconstruction import HTMLReconstructionContext, HTMLReconstructor
        context = HTMLReconstructionContext(
            output_dir=Path(self.output_dir),
            compressed_units_dir=Path(self.compressed_units_dir),
            translated_dir=Path(self.translated_dir),
            final_dir=Path(self.final_dir),
            source_epub=Path(self.epub_path),
            book_title=getattr(self, "book_title", Path(self.epub_path).stem),
            config=getattr(self, "config", {}),
            navigation_report=getattr(self, "navigation_report", {}),
            validate_units=self.validate_translated_units,
            merge_parts=self._merge_part_files,
            logical_records=self._logical_unit_records,
            write_report=self.write_translation_report,
            build_epub=build_html_epub,
        )
        return HTMLReconstructor(context).build(
            output_epub=output_epub,
            allow_partial=allow_partial,
            allow_navigation_warnings=allow_navigation_warnings,
        )
