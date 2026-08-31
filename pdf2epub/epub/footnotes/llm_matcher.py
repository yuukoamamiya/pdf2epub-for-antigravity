"""
LLM-based section matching for Notes chapters.
"""

import json
import re
from pathlib import Path
from typing import Dict, List, Optional, Set
from loguru import logger

from .content_index import ContentAddressIndex
from .models import FootnoteDefinition, NotesSection
from ...utils.unit_id import generate_unit_id


class LLMSectionMatcher:
    """
    Uses LLM to match Notes chapter sections to TOC chapters.
    """

    def __init__(
        self,
        markdown_dir: Path,
        config=None,
        content_index: Optional[ContentAddressIndex] = None,
    ):
        """
        Initialize the LLM section matcher.

        Args:
            markdown_dir: Directory containing markdown files
            config: Configuration object for LLM calls
        """
        self.markdown_dir = markdown_dir
        self.config = config
        self.content_index = content_index
        self.notes_sections: List[NotesSection] = []
        self.chapter_to_section: Dict[str, NotesSection] = {}
        self.toc_chapters: List[Dict] = []

    def load_toc_tree(self) -> bool:
        """
        Load toc_tree.json from the output directory.

        Returns:
            True if loaded successfully, False otherwise
        """
        # toc_tree.json is in the output directory
        # For translated/validated, we need to go up two levels
        # For polished_markdown, we need to go up one level
        toc_path = self.markdown_dir.parent / "toc_tree.json"
        if not toc_path.exists():
            # Try one more level up (for translated/validated structure)
            toc_path = self.markdown_dir.parent.parent / "toc_tree.json"
        if not toc_path.exists():
            logger.warning(f"toc_tree.json not found at {toc_path}")
            return False

        try:
            with open(toc_path, 'r', encoding='utf-8') as f:
                toc_data = json.load(f)

            # Flatten the TOC tree to get all chapters with unit_ids
            def flatten_chapters(chapters, parent_path=None):
                result = []
                for i, ch in enumerate(chapters):
                    path = (parent_path or []) + [i + 1]
                    unit_id = generate_unit_id(path)
                    result.append({
                        "unit_id": unit_id,
                        "title": ch.get("title", ""),
                        "type": ch.get("type")
                    })
                    if "children" in ch and ch["children"]:
                        result.extend(flatten_chapters(ch["children"], path))
                return result

            self.toc_chapters = flatten_chapters(toc_data.get("chapters", []))
            logger.debug(f"Loaded {len(self.toc_chapters)} chapters from toc_tree.json")
            return True

        except Exception as e:
            logger.error(f"Error loading toc_tree.json: {e}")
            return False

    def get_notes_structure(self, primary_definition_chapters: Set[str]) -> str:
        """
        Get the Notes chapter structure with footnote definitions removed.

        Args:
            primary_definition_chapters: Set of primary definition chapter names

        Returns:
            String with only headers/titles (footnote content removed)
        """
        # Read all primary definition chapter files
        all_lines = []
        for chapter_name in self._ordered_chapters(primary_definition_chapters):
            file_path = self.markdown_dir / f"{chapter_name}.md"
            if file_path.exists():
                try:
                    with open(file_path, 'r', encoding='utf-8') as f:
                        content = f.read()
                    # Remove footnote definition lines
                    lines = content.split('\n')
                    filtered_lines = []
                    for line in lines:
                        # Skip footnote definitions [^key]: content
                        if re.match(r'^\[\^\w+\]:', line):
                            continue
                        # Keep non-empty lines that might be headers
                        stripped = line.strip()
                        if stripped:
                            filtered_lines.append(stripped)
                    all_lines.extend(filtered_lines)
                except Exception as e:
                    logger.error(f"Error reading {file_path}: {e}")

        return '\n'.join(all_lines)

    def _ordered_chapters(self, chapter_names: Set[str]) -> List[str]:
        """Return source stems in physical EPUB reading order."""
        if self.content_index:
            return sorted(chapter_names, key=self.content_index.order_key)
        return sorted(chapter_names)

    def match_sections(
        self,
        primary_definition_chapters: Set[str],
    ) -> bool:
        """
        Use LLM to match Notes section headers to TOC chapters.

        Args:
            primary_definition_chapters: Set of primary definition chapter names

        Returns:
            True if matching succeeded, False otherwise
        """
        if not self.toc_chapters:
            logger.warning("No TOC chapters loaded")
            return False

        # Try to load cached results first
        cache_path = self.markdown_dir.parent / "footnote_section_matches.json"
        if cache_path.exists():
            try:
                with open(cache_path, 'r', encoding='utf-8') as f:
                    matches = json.load(f)
                logger.info(f"Loaded {len(matches)} section matches from cache")
                return self._parse_sections_from_result(matches, primary_definition_chapters)
            except Exception as e:
                logger.warning(f"Failed to load cached matches: {e}")

        # Cache misses are intentionally offline.  Semantic matching must be
        # prepared by the workspace Subagent before the EPUB build.
        logger.warning("No cached Subagent section matches; using local mapping")
        return False

    def _parse_sections_from_result(
        self,
        matches: List[Dict],
        primary_definition_chapters: Set[str]
    ) -> bool:
        """
        Parse Notes sections based on LLM matching results.

        Args:
            matches: List of {"header": str, "unit_id": str} from LLM
            primary_definition_chapters: Set of primary definition chapter names

        Returns:
            True if parsing succeeded, False otherwise
        """
        if not matches:
            return False

        # Read all primary definition chapter content with line numbers
        all_content = []  # [(line_num, line_text, source_file, local_line_num), ...]
        global_line_num = 0

        for chapter_name in self._ordered_chapters(primary_definition_chapters):
            file_path = self.markdown_dir / f"{chapter_name}.md"
            if file_path.exists():
                try:
                    with open(file_path, 'r', encoding='utf-8') as f:
                        lines = f.readlines()
                    for local_line_num, line in enumerate(lines, 1):
                        all_content.append((global_line_num, line, chapter_name, local_line_num))
                        global_line_num += 1
                except Exception as e:
                    logger.error(f"Error reading {file_path}: {e}")

        if not all_content:
            return False

        # Find header positions in the content
        header_positions = []  # [(global_line_num, header_text, unit_id, source_file, local_line_num), ...]

        # Preserve repeated matched headers instead of collapsing them in a dict.
        header_to_unit_ids: Dict[str, List[str]] = {}
        valid_scope_ids = {
            chapter["unit_id"]
            for chapter in self.toc_chapters
            if chapter.get("type") != "notes"
        }
        for match in matches:
            header = match.get("header", "").strip()
            unit_id = match.get("unit_id", "")
            if unit_id and unit_id not in valid_scope_ids:
                logger.warning(
                    f"Ignoring Notes section match to unknown/non-body unit {unit_id}"
                )
                continue
            if header and unit_id:
                # Normalize header for matching
                header_normalized = re.sub(r'^#+\s*', '', header).strip()
                header_to_unit_ids.setdefault(header_normalized, []).append(unit_id)

        # Scan content to find headers in order
        for global_ln, line, source_file, local_ln in all_content:
            line_stripped = line.strip()
            # Remove markdown header markers
            line_clean = re.sub(r'^#+\s*', '', line_stripped)

            # Check if this line matches any header
            if line_clean in header_to_unit_ids and header_to_unit_ids[line_clean]:
                unit_id = header_to_unit_ids[line_clean].pop(0)
                header_positions.append((global_ln, line_clean, unit_id, source_file, local_ln))

        # Log any unmatched headers
        for header, remaining_unit_ids in header_to_unit_ids.items():
            if remaining_unit_ids:
                logger.warning(
                    f"Header '{header}' not found in Notes content "
                    f"({len(remaining_unit_ids)} unmatched mapping(s))"
                )

        if not header_positions:
            logger.warning("No headers found in Notes content")
            return False

        # Sort by position
        header_positions.sort(key=lambda x: x[0])

        # Create fragments, then merge fragments mapped to the same semantic TOC
        # scope. OCR can turn one definition into a heading and split a Notes
        # section; that must not replace the earlier fragment.
        self.notes_sections = []
        self.chapter_to_section = {}
        sections_by_unit_id: Dict[str, NotesSection] = {}

        for i, (global_ln, header, unit_id, source_file, local_ln) in enumerate(header_positions):
            # Determine end position
            if i + 1 < len(header_positions):
                end_global_ln = header_positions[i + 1][0]
            else:
                end_global_ln = len(all_content)

            # Collect definitions in this section
            section_definitions = []
            for g_ln, line, src_file, loc_ln in all_content:
                if global_ln <= g_ln < end_global_ln:
                    # Check for footnote definition
                    def_match = re.match(r'^\[\^(\w+)\](?::\s*|\s+)(.*)', line)
                    if def_match:
                        key = def_match.group(1)
                        content = def_match.group(2)
                        section_definitions.append(
                            FootnoteDefinition(key, content, src_file, loc_ln)
                        )

            section = NotesSection(
                header_text=header,
                start_line=local_ln,
                end_line=local_ln + (end_global_ln - global_ln),
                source_file=source_file,
                definitions=section_definitions,
                matched_unit_id=unit_id
            )
            existing = sections_by_unit_id.get(unit_id)
            if existing:
                existing.definitions.extend(section.definitions)
                existing.end_line = section.end_line
                logger.debug(
                    f"Merged Notes fragment '{header}' into {unit_id}: "
                    f"{len(section_definitions)} definitions"
                )
            else:
                sections_by_unit_id[unit_id] = section
                self.notes_sections.append(section)
                self.chapter_to_section[unit_id] = section
                logger.debug(
                    f"Section '{header}' -> {unit_id}: "
                    f"{len(section_definitions)} definitions"
                )

        for section in self.notes_sections:
            section.definitions.sort(
                key=lambda definition: (
                    self.content_index.order_key(definition.chapter)
                    if self.content_index
                    else definition.chapter,
                    definition.line_num,
                )
            )

        logger.info(
            f"Parsed {len(header_positions)} Notes fragments into "
            f"{len(self.notes_sections)} semantic sections"
        )
        return True
