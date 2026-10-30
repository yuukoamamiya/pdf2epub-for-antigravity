"""Deterministic EPUB content extraction and translation-unit preparation."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from pathlib import PurePosixPath
from typing import Any, Callable, Mapping, Optional
from loguru import logger

from .compressed_cache import CompressedExtractionCache
from .epub_parser import EPUBResourceNotFoundError
from .unit_files import HTMLUnitStore, HTMLUnitWorkspace
from .package_identity import MAPPING_SCHEMA_VERSION, unit_id_for_package_path, validate_package_relative_path
from pdf2epub.batching_service import batching_config
from pdf2epub.workflow_contracts import atomic_write_json
from .metadata_values import make_json_safe


@dataclass(frozen=True)
class ExtractionWorkspace:
    epub_path: Path
    output_dir: Path
    compressed_units_dir: Path
    parser: Any
    config: Mapping[str, Any]
    metadata: Mapping[str, Any]
    source_language: str
    book_title: str
    create_metadata_source: Callable[..., Any]


class HTMLExtractionPreparer:
    """Extract spine documents, compact their text, and write mapping/cache data."""
    def __init__(self, workspace: ExtractionWorkspace):
        self.epub_path = Path(workspace.epub_path)
        self.output_dir = Path(workspace.output_dir)
        self.compressed_units_dir = Path(workspace.compressed_units_dir)
        self.parser = workspace.parser
        self.config = workspace.config
        self.metadata = workspace.metadata
        self.source_language = workspace.source_language
        self.book_title = workspace.book_title
        self.create_metadata_source = workspace.create_metadata_source

    def _cache(self):
        return CompressedExtractionCache(self.output_dir, self.epub_path, self.compressed_units_dir)

    def _compressed_cache_is_current(self, *, max_nonempty_lines: int) -> bool:
        return self._cache().is_current(max_nonempty_lines=max_nonempty_lines)

    def _write_compressed_cache(self, *, max_nonempty_lines: int) -> None:
        self._cache().write(max_nonempty_lines=max_nonempty_lines)

    def create_metadata_translation_source(self, target_language=None, context_files=None):
        return self.create_metadata_source(
            target_language=target_language, context_files=context_files
        )

    def prepare(
        self,
        target_language: Optional[str] = None,
        translation_context: Optional[Mapping[str, Path]] = None,
    ) -> int:
        """
        Extract XHTML from EPUB and compress for translation.

        Uses HTMLCompressor to strip outer structure and save mapping.
        Only translatable content is output (no empty placeholders).

        Returns:
            Number of files extracted
        """
        import json
        from pdf2epub.batching_service import batching_config
        from .compressor import HTMLCompressor
        from .verified_compactor import VerifiedCompactor

        batching = batching_config(self.config)
        if self._compressed_cache_is_current(
            max_nonempty_lines=batching["max_nonempty_lines"]
        ):
            logger.info(
                "Reusing current compressed EPUB cache; skipping XHTML extraction "
                "and HTML compaction"
            )
            self.create_metadata_translation_source(
                target_language=target_language,
                context_files=translation_context,
            )
            return len(list(self.compressed_units_dir.glob("*.mapping.json")))

        # Get all CSS content for compactor
        css_content = ""
        for css_item in self.parser.resources.get('css', []):
            try:
                content = css_item.get('content', b'')
                if isinstance(content, bytes):
                    content = content.decode('utf-8')
                css_content += content + "\n"
            except Exception as e:
                logger.warning(f"Failed to read CSS: {e}")

        # Create compactor with CSS for DOM optimization (oracle-verified)
        compactor = VerifiedCompactor(css_content) if css_content else None
        compressor = HTMLCompressor(compactor=compactor)

        if compactor:
            logger.info(
                f"VerifiedCompactor: {compactor.oracle.selector_count} selectors, "
                f"{len(compactor.oracle.failed_selectors)} failed"
            )

        extracted = 0
        spine_items = list(self.parser.spine)

        # A prior pre-identity workspace may contain pathless mappings whose
        # translated files are user-authored. Reuse those unit names only when
        # each mapping resolves to exactly one current spine member. Resolve
        # the whole set before writing any files so ambiguous legacy state is
        # left intact and fails with an actionable message.
        existing_units_by_path = {}
        legacy_unit_ids = set()
        claimed_package_paths = set()
        spine_paths = {}
        for item in spine_items:
            href = str(item.get("href", ""))
            package_path = validate_package_relative_path(
                item.get("full_href") or href.split("#", 1)[0]
            )
            folded = package_path.casefold()
            if folded in spine_paths and spine_paths[folded] != package_path:
                raise ValueError(
                    f"EPUB contains package paths that collide on Windows: "
                    f"{spine_paths[folded]!r} and {package_path!r}"
                )
            spine_paths[folded] = package_path

        for mapping_path in sorted(self.compressed_units_dir.glob("*.mapping.json")):
            try:
                old_mapping = json.loads(mapping_path.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError) as exc:
                raise ValueError(f"Cannot safely resume invalid mapping {mapping_path.name}: {exc}") from exc
            if not isinstance(old_mapping, dict):
                raise ValueError(f"Cannot safely resume non-object mapping {mapping_path.name}")
            legacy_stem = mapping_path.name[:-len(".mapping.json")]
            mapped_path = old_mapping.get("package_relative_path")
            if mapped_path:
                package_path = validate_package_relative_path(mapped_path)
                if package_path.casefold() not in spine_paths:
                    raise ValueError(
                        f"Mapping {mapping_path.name} targets a document no longer in the EPUB: {package_path}; "
                        "preserve translations and resolve the stale mapping inventory before rerunning html-prepare."
                    )
                package_path = spine_paths[package_path.casefold()]
                if not old_mapping.get("legacy_unit_id") and legacy_stem != unit_id_for_package_path(package_path):
                    raise ValueError(f"Mapping identity does not match filename: {mapping_path.name}")
            else:
                old_extension = str(old_mapping.get("original_extension") or "").casefold()
                candidates = []
                for item in spine_items:
                    authored_path = PurePosixPath(str(item.get("href", "")).split("#", 1)[0])
                    package_path = validate_package_relative_path(
                        item.get("full_href") or str(item.get("href", "")).split("#", 1)[0]
                    )
                    member_path = PurePosixPath(package_path)
                    names = {authored_path.name.casefold(), member_path.name.casefold()}
                    expected_name = f"{legacy_stem}{old_extension}".casefold()
                    if expected_name in names:
                        candidates.append(package_path)
                candidates = list(dict.fromkeys(candidates))
                if len(candidates) != 1:
                    reason = "no current spine member" if not candidates else "multiple current spine members"
                    raise ValueError(
                        f"Legacy mapping {mapping_path.name} has no package path and matches {reason}; "
                        "preserve the translated file, then resolve or move the stale mapping manually before rerunning html-prepare."
                    )
                package_path = candidates[0]
            folded = package_path.casefold()
            if folded in claimed_package_paths:
                raise ValueError(
                    f"Multiple mappings claim package path {package_path}; "
                    "preserve translations and resolve the mapping inventory before rerunning html-prepare."
                )
            claimed_package_paths.add(folded)
            existing_units_by_path[package_path] = legacy_stem
            if old_mapping.get("legacy_unit_id") or not mapped_path:
                legacy_unit_ids.add(legacy_stem)

        for item in spine_items:
            href = item.get('href', '')
            if not href:
                continue

            try:
                # Get raw content directly from EPUB ZIP
                # (bypasses ebooklib which modifies HTML - removes body class, head content)
                content = self.parser.get_raw_content(href)
                if content is None:
                    raise EPUBResourceNotFoundError(
                        f"Spine document is missing or ambiguous in EPUB ZIP: {href}"
                    )

                # Decode if bytes
                if isinstance(content, bytes):
                    content = content.decode('utf-8')

                # Compress HTML
                compressed, mapping = compressor.compress(content, author_css=css_content)

                # Save compressed content (.md - one translation unit per line).
                # Very dense EPUB indexes/references are split into resumable
                # source parts so each worker receives a bounded number of
                # translation units.  The full mapping remains attached to the
                # logical base file and is merged again only at build time.
                # The decoded full ZIP member path is the document identity.
                # ``href`` fallback keeps lightweight parser test doubles and
                # pre-contract custom parsers usable; EPUBParser always supplies
                # full_href for local spine items.
                package_path = item.get("full_href") or str(href).split("#", 1)[0]
                package_path = validate_package_relative_path(package_path)
                file_stem = existing_units_by_path.get(package_path) or unit_id_for_package_path(package_path)
                legacy_file_stem = file_stem if file_stem in legacy_unit_ids else None
                original_extension = Path(package_path).suffix
                compressed_path = self.compressed_units_dir / f"{file_stem}.md"
                compressed_path.write_text(compressed, encoding='utf-8')

                # Save mapping for decompression (.mapping.json)
                # Include original extension for correct output filename
                mapping['original_extension'] = original_extension
                mapping['mapping_schema_version'] = MAPPING_SCHEMA_VERSION
                mapping['package_relative_path'] = package_path
                if legacy_file_stem:
                    mapping['legacy_unit_id'] = True
                mapping_path = self.compressed_units_dir / f"{file_stem}.mapping.json"
                # Serialize first, then write — prevents truncated files if
                # json.dumps fails mid-serialization (e.g. non-serializable lxml objects)
                nonempty_lines = [
                    line for line in compressed.splitlines() if line.strip()
                ]
                if len(nonempty_lines) > batching["max_nonempty_lines"]:
                    part_names = []
                    for part_index in range(
                        0, len(nonempty_lines), batching["max_nonempty_lines"]
                    ):
                        part_name = (
                            f"{file_stem}.part{part_index // batching['max_nonempty_lines'] + 1}.md"
                        )
                        (self.compressed_units_dir / part_name).write_text(
                            "\n".join(
                                nonempty_lines[
                                    part_index : part_index + batching["max_nonempty_lines"]
                                ]
                            )
                            + "\n",
                            encoding="utf-8",
                        )
                        part_names.append(part_name)
                    mapping["translation_parts"] = part_names
                    mapping["translation_split"] = {
                        "unit": "nonempty_lines",
                        "limit": batching["max_nonempty_lines"],
                    }
                    logger.info(
                        f"Split dense unit {file_stem} into {len(part_names)} source parts "
                        f"({batching['max_nonempty_lines']} non-empty lines/part)"
                    )
                atomic_write_json(
                    mapping_path,
                    make_json_safe(mapping),
                    indent=None,
                )

                extracted += 1
                lines = len(compressed.splitlines()) if compressed else 0
                logger.debug(f"Compressed: {file_stem} ({lines} translatable lines)")

            except EPUBResourceNotFoundError:
                raise
            except Exception as e:
                logger.warning(f"Failed to extract {href}: {e}")

        # Metadata is deliberately prepared as a separate, small work item.
        # The Antigravity subagent can translate it without touching OPF/XML,
        # while the builder remains responsible for applying only validated
        # values.  This also makes metadata translation resumable and auditable.
        self.create_metadata_translation_source(
            target_language=target_language,
            context_files=translation_context,
        )

        logger.info(f"Compressed {extracted} XHTML files to {self.compressed_units_dir}")
        self._write_compressed_cache(max_nonempty_lines=batching["max_nonempty_lines"])
        return extracted
