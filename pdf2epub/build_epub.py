"""
Build EPUB from toc_tree.json structure.

This module generates EPUB files using toc_tree.json as the authoritative
source for chapter structure and titles. This provides:
- Accurate title hierarchy from PDF TOC
- Consistent chapter numbering
- Support for unlimited nesting levels
"""

import json
import difflib
import os
import re
import sys
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass
from loguru import logger

from .epub.builder import EpubBuilder, localized_toc_label
from .epub.converter import ContentConverter
from .utils.unit_id import generate_unit_id
from .utils.pdf_utils import extract_cover_image


@dataclass
class BuildEpubConfig:
    """Configuration for EPUB building."""
    book_title: str
    output_dir: Path
    markdown_dir: Path
    toc_tree_path: Path
    images_dir: Optional[Path] = None
    cover_image: Optional[Path] = None
    translated: bool = False
    target_language: str = "Chinese"
    config: Optional[Dict] = None
    output_epub: Optional[Path] = None
    combined_markdown_path: Optional[Path] = None


def _usable_metadata_value(value) -> str:
    """Return a clean metadata string, treating placeholders as missing."""
    if not isinstance(value, str):
        return ""
    value = value.strip()
    if value.lower() in {"unknown", "unknown author", "n/a", "na", "none"}:
        return ""
    return value


def resolve_book_metadata(
    toc_trees: List[Dict],
    config: Optional[Dict],
    output_dir: Path,
) -> Dict[str, str]:
    """Resolve bibliographic metadata without guessing from body text.

    Explicit config values win, followed by Agent-extracted TOC metadata and
    finally the PDF's embedded author field.  The original TOC is accepted
    alongside the translated TOC so translated builds retain original names.
    """
    config = config or {}
    configured_metadata = config.get("metadata", {})
    if not isinstance(configured_metadata, dict):
        configured_metadata = {}

    result: Dict[str, str] = {}
    for field in ("author", "publisher"):
        candidates = [config.get(field), configured_metadata.get(field)]
        for tree in toc_trees:
            if not isinstance(tree, dict):
                continue
            candidates.append(tree.get(field))
            tree_metadata = tree.get("metadata")
            if isinstance(tree_metadata, dict):
                candidates.append(tree_metadata.get(field))
        result[field] = next(
            (
                value
                for value in (_usable_metadata_value(item) for item in candidates)
                if value
            ),
            "",
        )

    if not result["author"]:
        try:
            import pymupdf as fitz

            for filename in ("input_original.pdf", "input.pdf"):
                pdf_path = Path(output_dir) / filename
                if not pdf_path.exists():
                    continue
                with fitz.open(pdf_path) as document:
                    embedded_author = _usable_metadata_value(
                        (document.metadata or {}).get("author")
                    )
                if embedded_author:
                    result["author"] = embedded_author
                    break
        except Exception as exc:
            logger.debug(f"Could not read embedded PDF metadata: {exc}")

    if not result["author"]:
        logger.warning(
            "No author metadata found; EPUB will use 'Unknown'. "
            "Add author to toc_tree.json or metadata in config.yaml."
        )
    return result


def load_toc_tree(path: Path) -> Dict:
    """Load and validate toc_tree.json."""
    if not path.exists():
        raise FileNotFoundError(f"toc_tree.json not found at {path}")

    with open(path, 'r', encoding='utf-8') as f:
        toc_tree = json.load(f)

    # Validate required fields
    if 'chapters' not in toc_tree:
        raise ValueError("toc_tree.json must contain 'chapters' array")

    return toc_tree


def calculate_tree_depth(entries: List[Dict], current_depth: int = 1) -> int:
    """
    Calculate maximum depth of hierarchical structure.

    Args:
        entries: List of chapter/section entries
        current_depth: Current depth level

    Returns:
        Maximum depth found in the tree
    """
    if not entries:
        return current_depth - 1 if current_depth > 1 else 1

    max_depth = current_depth
    for entry in entries:
        children = entry.get('children', [])
        if children:
            child_depth = calculate_tree_depth(children, current_depth + 1)
            max_depth = max(max_depth, child_depth)

    return max_depth


def find_chapter_file(unit_id: str, markdown_dir: Path) -> Optional[Path]:
    """
    Find markdown file for a unit ID.

    Args:
        unit_id: Unit ID like "chapter_12" or "chapter_7.1.1"
        markdown_dir: Directory containing markdown files

    Returns the base file (first part if split).
    Handles nested splits like chapter_25.part1.part1.md
    """
    # Try exact match first
    exact_file = markdown_dir / f"{unit_id}.md"
    if exact_file.exists():
        return exact_file

    # Try part1 file
    part1_file = markdown_dir / f"{unit_id}.part1.md"
    if part1_file.exists():
        return part1_file

    # Try nested part1.part1 file (when part1 was further split)
    nested_part1_file = markdown_dir / f"{unit_id}.part1.part1.md"
    if nested_part1_file.exists():
        return nested_part1_file

    return None


def find_part_files(
    base_file: Path,
    part_order: Optional[Dict[str, List[str]]] = None,
) -> List[Path]:
    """
    Find all part files for a chapter.

    Given chapter_10.38_intro.md or chapter_10.38_intro.part1.md,
    returns [part1, part2, part3, ...] in order.

    Handles nested splits like:
    - chapter_25.part1.part1.md
    - chapter_25.part1.part2.md
    - chapter_25.part2.md
    - chapter_25.part3.md
    """
    if base_file is None:
        return []

    if part_order and base_file.name in part_order:
        names = [str(name) for name in part_order[base_file.name]]
        if any(
            Path(name).name != name or Path(name).suffix.lower() != ".md"
            for name in names
        ):
            names = []
        ordered = [base_file.parent / name for name in names]
        if ordered and all(path.is_file() for path in ordered):
            return ordered

    # Get the base name without ALL .partN suffixes
    name = base_file.name
    stem = name.rsplit('.md', 1)[0]  # Remove .md

    # Strip all .partN suffixes to get true base
    # chapter_25.part1.part1 -> chapter_25
    base_pattern = stem
    while '.part' in base_pattern:
        base_pattern = base_pattern.rsplit('.part', 1)[0]

    # Find all part files (including nested)
    # This matches chapter_25.part1.md, chapter_25.part2.md, chapter_25.part1.part1.md, etc.
    part_files = list(base_file.parent.glob(f"{base_pattern}.part*.md"))

    if part_files:
        # Sort by extracting part numbers
        def sort_key(p: Path):
            # chapter_25.part1.part2.md -> [1, 2]
            # chapter_25.part2.md -> [2]
            stem = p.stem
            parts = []
            remaining = stem
            while '.part' in remaining:
                idx = remaining.index('.part')
                remaining = remaining[idx + 5:]  # Skip ".part"
                # Extract number
                num_str = ""
                for c in remaining:
                    if c.isdigit():
                        num_str += c
                    else:
                        break
                if num_str:
                    parts.append(int(num_str))
                    remaining = remaining[len(num_str):]
            return parts

        part_files = sorted(part_files, key=sort_key)
        return part_files
    else:
        # No part files, return the single file
        return [base_file]


def process_chapter_content(
    toc_title: str,
    toc_level: int,
    markdown_content: str,
    is_first_part: bool
) -> str:
    """
    Process chapter content with correct title and heading levels.

    Args:
        toc_title: Title from toc_tree.json
        toc_level: Level from toc_tree.json (2, 3, 4, ...)
        markdown_content: Raw markdown content
        is_first_part: True for base file or part1, False for part2+

    Returns:
        Processed markdown with correct title and heading levels
    """
    if not is_first_part:
        # part2, part3... don't get chapter title, just relevel
        return relevel_content(markdown_content, toc_level)

    lines = markdown_content.split('\n')

    # Generate heading with correct level
    heading_prefix = '#' * toc_level
    chapter_heading = f"{heading_prefix} {toc_title}"

    # A leaf unit may carry structural ancestor headings in front of its own
    # heading (for example ``# Part`` followed by ``## Section``). Keep those
    # headings in the first child's document so navigating to the structural
    # parent displays its title before the child section.
    first_heading_idx = _find_first_heading(lines)
    title_line_idx = _find_matching_heading(lines, toc_title)
    has_structural_prefix = (
        first_heading_idx is not None
        and title_line_idx is not None
        and title_line_idx != first_heading_idx
        and _is_structural_heading_prefix(lines[:title_line_idx])
    )

    # Check for the current heading at the beginning of the unit.  If it is
    # absent, the first heading is still the best authoritative replacement
    # for ordinary units whose OCR/source heading uses another language.
    if first_heading_idx is not None and title_line_idx is None:
        title_line_idx = first_heading_idx

    if title_line_idx is not None:
        # Replace existing heading
        lines[title_line_idx] = chapter_heading
    else:
        # Add heading at start
        lines.insert(0, chapter_heading)
        lines.insert(1, '')  # Blank line after heading
        title_line_idx = 0

    # Remove duplicate heading if present (OCR artifact)
    _remove_duplicate_heading(lines, title_line_idx, toc_title)

    # Relevel the current unit's descendants while leaving any structural
    # ancestor prefix at its original levels.
    if has_structural_prefix and title_line_idx is not None:
        prefix = '\n'.join(lines[:title_line_idx])
        unit_content = relevel_content(
            '\n'.join(lines[title_line_idx:]), toc_level, skip_first=True
        )
        return f"{prefix}\n{unit_content}" if prefix else unit_content

    # Relevel remaining headings
    return relevel_content('\n'.join(lines), toc_level, skip_first=True)


def add_subchapter_anchors_to_parts(
    converter: ContentConverter,
    rendered_parts: list[tuple[str, str]],
    chapter_index: int,
    subchapter_info: list[dict],
) -> tuple[list[tuple[str, str]], dict[str, str]]:
    """Place each child anchor in the part that actually contains its title.

    A logical PDF unit may be split into multiple HTML files after Markdown
    processing.  Searching only part 1 makes later child entries appear to
    have missing anchors.  Searching all parts is not enough by itself: the
    navigation href must also follow the part where the anchor was found.
    """
    locations: dict[str, str] = {}
    for child_index, info in enumerate(subchapter_info, 1):
        title = str(info.get("title") or "")
        anchor = str(info.get("anchor") or f"toc-{chapter_index}-{child_index}")
        found = False
        for part_index, (filename, html_content) in enumerate(rendered_parts):
            updated, found = converter._add_subchapter_anchor(
                html_content,
                title,
                anchor,
            )
            if not found:
                continue
            rendered_parts[part_index] = (filename, updated)
            locations[anchor.lstrip("#")] = filename
            break
        if not found:
            logger.warning(
                f"Could not locate heading for stable anchor #{anchor}: '{title}'"
            )
    return rendered_parts, locations


def _embedded_subchapter_entries(entries: list[dict]) -> list[dict]:
    """Collect descendants that share the current unit's rendered HTML."""
    result: list[dict] = []
    for entry in entries:
        if entry.get('file_path') or entry.get('virtual_file_name'):
            # A real or virtual child has its own navigation target; its
            # descendants must be resolved while processing that child.
            continue
        result.append(entry)
        result.extend(_embedded_subchapter_entries(entry.get('children', [])))
    return result


def _find_first_heading(lines: list[str]) -> Optional[int]:
    """Return the first Markdown heading line in a unit."""
    for index, line in enumerate(lines):
        if re.match(r"^\s*#+\s+", line):
            return index
    return None


def _normalize_heading_text(value: str) -> str:
    """Normalize heading text for conservative TOC-title matching."""
    value = re.sub(r"^#+\s*", "", str(value or ""))
    return re.sub(r"[^\w\u3400-\u9fff]+", "", value, flags=re.UNICODE).casefold()


def _is_structural_heading_prefix(lines: list[str]) -> bool:
    """Return whether a prefix contains only headings and whitespace."""
    return all(not line.strip() or re.match(r"^\s*#+\s+", line) for line in lines)


def _find_matching_heading(lines: list[str], toc_title: str) -> Optional[int]:
    """Find the first heading matching the current TOC title."""
    normalized_title = _normalize_heading_text(toc_title)
    if not normalized_title:
        return None
    for index, line in enumerate(lines):
        if not re.match(r"^\s*#+\s+", line):
            continue
        normalized_heading = _normalize_heading_text(line)
        if normalized_heading == normalized_title:
            return index
        if difflib.SequenceMatcher(None, normalized_title, normalized_heading).ratio() >= 0.9:
            return index
    return None


def find_replaced_heading_lines(lines: list[str], toc_title: str) -> list[int]:
    """Return 1-indexed line numbers of headings replaced or pruned by process_chapter_content."""
    first_heading_idx = _find_first_heading(lines)
    title_line_idx = _find_matching_heading(lines, toc_title)
    if (
        first_heading_idx is not None
        and title_line_idx is not None
        and title_line_idx != first_heading_idx
    ):
        prefix = lines[:title_line_idx]
        if _is_structural_heading_prefix(prefix):
            return [
                idx + 1
                for idx in range(title_line_idx + 1)
                if re.match(r"^\s*#+\s+", lines[idx])
            ]

    if title_line_idx is not None:
        target_idx = title_line_idx
    elif first_heading_idx is not None:
        target_idx = first_heading_idx
    else:
        target_idx = None

    if target_idx is not None:
        return [target_idx + 1]
    return []



def _remove_duplicate_heading(lines: list, title_line_idx: int, toc_title: str):
    """Remove duplicate heading that follows the chapter title.

    OCR often produces a heading like "## Title" which duplicates the TOC title.
    If we find a textually equivalent heading within the next few lines, remove it.

    Semantic similarity is intentionally not used here.  A chapter title and its
    first real subsection can be conceptually related (especially in translated
    Chinese text), but they are still distinct headings.  Removing a subsection
    at this stage also makes its EPUB TOC anchor impossible to create.
    """
    def normalize_heading(value: str) -> str:
        return _normalize_heading_text(value)

    normalized_title = normalize_heading(toc_title)
    # Look in lines after the title (skip blank lines)
    for i in range(title_line_idx + 1, min(title_line_idx + 5, len(lines))):
        line = lines[i].strip()
        if not line:
            continue
        if line.startswith('#'):
            # Extract heading text (remove # prefix)
            heading_text = re.sub(r'^#+\s*', '', line)
            normalized_heading = normalize_heading(heading_text)
            similarity = difflib.SequenceMatcher(
                None, normalized_title, normalized_heading
            ).ratio()
            if normalized_title and (
                normalized_title == normalized_heading
                or similarity >= 0.9
            ):
                # Remove this duplicate heading
                lines[i] = ''
                logger.debug(
                    f"Removed duplicate heading: '{heading_text}' "
                    f"(text similarity: {similarity:.2f})"
                )
            break  # Only check the first heading after title


# Lazy-loaded embedding model
_embedding_model = None
_HEADING_EMBEDDING_MODEL_NAME = "paraphrase-multilingual-MiniLM-L12-v2"
_HEADING_EMBEDDING_MIN_RAM_GB = 8.0


def _get_total_memory_bytes() -> Optional[int]:
    """Return total physical memory when the platform exposes it."""
    if hasattr(os, "sysconf"):
        try:
            pages = os.sysconf("SC_PHYS_PAGES")
            page_size = os.sysconf("SC_PAGE_SIZE")
            if pages > 0 and page_size > 0:
                return int(pages * page_size)
        except (OSError, ValueError, TypeError):
            pass

    if sys.platform == "win32":
        try:
            import ctypes

            class MEMORYSTATUSEX(ctypes.Structure):
                _fields_ = [
                    ("dwLength", ctypes.c_ulong),
                    ("dwMemoryLoad", ctypes.c_ulong),
                    ("ullTotalPhys", ctypes.c_ulonglong),
                    ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong),
                    ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong),
                    ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("sullAvailExtendedVirtual", ctypes.c_ulonglong),
                ]

            memory_status = MEMORYSTATUSEX()
            memory_status.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(memory_status)):
                return int(memory_status.ullTotalPhys)
        except Exception:
            pass

    return None


def _embedding_disabled_by_env() -> Optional[bool]:
    """Return an explicit env override for heading embeddings, if set."""
    value = os.getenv("PDF2EPUB_HEADING_EMBEDDINGS")
    if value is None:
        value = os.getenv("PDF2EPUB_DISABLE_HEADING_EMBEDDINGS")
        if value is None:
            return None
        return value.strip().lower() in {"1", "true", "yes", "on"}

    normalized = value.strip().lower()
    if normalized in {"0", "false", "no", "off", "difflib", "disabled"}:
        return True
    if normalized in {"1", "true", "yes", "on", "embedding", "enabled"}:
        return False
    logger.warning(
        "Ignoring invalid PDF2EPUB_HEADING_EMBEDDINGS value "
        f"{value!r}; expected enabled/disabled"
    )
    return None


def _heading_embedding_min_ram_gb() -> float:
    """Minimum total RAM required before loading sentence-transformer embeddings."""
    value = os.getenv("PDF2EPUB_HEADING_EMBEDDING_MIN_RAM_GB")
    if value is None:
        return _HEADING_EMBEDDING_MIN_RAM_GB
    try:
        threshold = float(value)
    except ValueError:
        logger.warning(
            "Ignoring invalid PDF2EPUB_HEADING_EMBEDDING_MIN_RAM_GB value "
            f"{value!r}; using {_HEADING_EMBEDDING_MIN_RAM_GB:g}GB"
        )
        return _HEADING_EMBEDDING_MIN_RAM_GB
    return max(0.0, threshold)


def _should_use_heading_embeddings() -> bool:
    """Decide whether heading deduplication should load the embedding model."""
    disabled = _embedding_disabled_by_env()
    if disabled is not None:
        return not disabled

    total_memory = _get_total_memory_bytes()
    min_ram_gb = _heading_embedding_min_ram_gb()
    if total_memory is None:
        logger.debug(
            "Could not detect total RAM; allowing heading embedding model load"
        )
        return True

    total_gb = total_memory / (1024 ** 3)
    if total_gb < min_ram_gb:
        logger.info(
            f"Total RAM is {total_gb:.1f}GB, below {min_ram_gb:.1f}GB threshold; "
            "using difflib for heading deduplication"
        )
        return False
    return True


def _get_embedding_model():
    """Lazy load the sentence transformer model."""
    global _embedding_model
    if _embedding_model is None:
        if not _should_use_heading_embeddings():
            _embedding_model = False
            return _embedding_model
        try:
            from sentence_transformers import SentenceTransformer
            # Small multilingual model (~120MB), but runtime imports may need much more RAM.
            _embedding_model = SentenceTransformer(_HEADING_EMBEDDING_MODEL_NAME)
            logger.debug("Loaded embedding model for heading deduplication")
        except ImportError:
            logger.warning("sentence-transformers not installed, falling back to text similarity")
            _embedding_model = False  # Mark as unavailable
    return _embedding_model


def _compute_semantic_similarity(text1: str, text2: str) -> float:
    """Compute semantic similarity between two texts using embeddings.

    Falls back to text-based similarity if embeddings unavailable.
    """
    import difflib

    model = _get_embedding_model()
    if model is False:
        # Fallback to text similarity
        return difflib.SequenceMatcher(None, text1.lower(), text2.lower()).ratio()

    try:
        # Compute embeddings
        embeddings = model.encode([text1, text2], convert_to_tensor=True)
        # Cosine similarity
        from sentence_transformers import util
        similarity = util.cos_sim(embeddings[0], embeddings[1]).item()
        return similarity
    except Exception as e:
        logger.warning(f"Embedding computation failed: {e}, falling back to text similarity")
        return difflib.SequenceMatcher(None, text1.lower(), text2.lower()).ratio()


def relevel_content(content: str, base_level: int, skip_first: bool = False) -> str:
    """
    Adjust heading levels so all content headings are below base_level.

    For base_level=3, markdown headings become:
    - # -> #### (base_level + 1)
    - ## -> ##### (base_level + 2)
    - etc.

    Args:
        content: Markdown content
        base_level: The level of the chapter heading
        skip_first: If True, don't relevel the first heading (it's the chapter title)

    Returns:
        Content with adjusted heading levels
    """
    lines = content.split('\n')
    first_skipped = False

    for i, line in enumerate(lines):
        match = re.match(r'^(#+)\s', line)
        if match:
            if skip_first and not first_skipped:
                first_skipped = True
                continue

            current_hashes = len(match.group(1))
            # Content headings start at base_level + 1
            new_level = base_level + current_hashes
            # Cap at 6 (maximum markdown heading level)
            new_level = min(new_level, 6)
            lines[i] = '#' * new_level + line[current_hashes:]

    return '\n'.join(lines)


def has_notes_chapter(chapters: List[Dict]) -> bool:
    """
    Check if any chapter in the TOC tree has type 'notes'.

    Args:
        chapters: List of chapter dictionaries from toc_tree

    Returns:
        True if a notes chapter is found
    """
    for chapter in chapters:
        if chapter.get('type') == 'notes':
            return True
        if 'children' in chapter and chapter['children']:
            if has_notes_chapter(chapter['children']):
                return True
    return False


def validate_centralized_endnotes_for_build(structure: List[Dict]) -> dict:
    """Fail closed when a typed Notes unit still contains raw endnotes."""
    from .refine.endnote_normalization import validate_normalized_endnotes

    source_texts: dict[str, str] = {}
    file_roles: dict[str, str] = {}

    def visit(entries: List[Dict]) -> None:
        for entry in entries:
            role = str(entry.get("type") or "").strip().lower()
            part_files = entry.get("part_files") or (
                [entry["file_path"]] if entry.get("file_path") else []
            )
            for part_file in part_files:
                path = Path(part_file)
                if not path.is_file():
                    continue
                name = path.name
                source_texts[name] = path.read_text(encoding="utf-8")
                if role:
                    file_roles[name] = role
            visit(entry.get("children") or [])

    visit(structure)
    return validate_normalized_endnotes(source_texts, file_roles)


def flatten_toc_tree(
    chapters: List[Dict],
    parent_index_path: List[int] = None,
    use_translated_titles: bool = False,
) -> List[Dict]:
    """
    Flatten nested toc_tree structure into a list with file assignments.

    Each entry in the result has:
    - title: Chapter title
    - level: Heading level
    - index_path: Hierarchical index like [7, 1, 1]
    - children: Flattened children (if any)
    """
    result = []

    for i, chapter in enumerate(chapters):
        # Build index_path: parent path + current 1-based index
        if parent_index_path is None:
            index_path = [i + 1]
        else:
            index_path = parent_index_path + [i + 1]

        title = chapter.get('title', '')
        if use_translated_titles:
            title = chapter.get('title', '')
        entry = {
            'title': title,
            'level': chapter.get('level', 1),
            'index_path': index_path,
            'anchor': 'toc-' + '-'.join(str(value) for value in index_path),
            'start_page': chapter.get('start_page'),
            'end_page': chapter.get('end_page'),
        }
        if 'type' in chapter:
            entry['type'] = chapter['type']

        if 'children' in chapter and chapter['children']:
            entry['children'] = flatten_toc_tree(
                chapter['children'],
                index_path,
                use_translated_titles=use_translated_titles,
            )

        result.append(entry)

    return result



def build_epub_structure(
    toc_structure: List[Dict],
    markdown_dir: Path,
    part_order: Optional[Dict[str, List[str]]] = None,
) -> Dict:
    """
    Build the final EPUB structure with file paths.

    Args:
        toc_structure: Processed toc structure (possibly translated)
        markdown_dir: Directory containing markdown files

    Returns:
        Structure dict for EPUB generation
    """
    def process_entry(entry: Dict, inherited_file: Optional[Path] = None) -> Dict:
        result = {
            'title': entry['title'],
            'level': entry['level'],
        }
        if 'type' in entry:
            result['type'] = entry['type']
        if 'anchor' in entry:
            result['anchor'] = entry['anchor']

        # Find markdown file using unit_id
        index_path = entry.get('index_path')

        if index_path is not None:
            # Generate unit_id from hierarchical index
            unit_id = generate_unit_id(index_path)
            result['unit_id'] = unit_id

            file_path = find_chapter_file(unit_id, markdown_dir)

            if file_path:
                result['file_path'] = file_path
                result['part_files'] = find_part_files(file_path, part_order)
            elif not entry.get('children') and inherited_file is None:
                # Only warn if this is a leaf node (no children)
                # Container nodes (with children) don't need their own markdown
                logger.warning(f"No markdown file found for {unit_id}: {entry['title']}")
            # Structural-only parents are represented by their first emitted
            # child unit. Refinement keeps a parent's heading/preamble with
            # that child, so the TOC generators resolve the parent to the
            # first real descendant file instead of creating a title-only page.

        # Process children recursively
        if 'children' in entry:
            child_inherited_file = result.get('file_path') or inherited_file
            result['children'] = [
                process_entry(c, child_inherited_file) for c in entry['children']
            ]

        return result

    return [process_entry(ch) for ch in toc_structure]


def _ordered_markdown_html_entries(structure: List[Dict]) -> List[Tuple[str, Dict, int, str]]:
    """Return Markdown-backed EPUB files in their reading order.

    Virtual structural pages are intentionally omitted: page-boundary
    carry-over belongs to the preceding prose unit, not to a generated
    parent-container page.
    """
    result: List[Tuple[str, Dict, int, str]] = []

    def visit(entries: List[Dict]) -> None:
        for entry in entries:
            if entry.get("file_path") and entry.get("unit_id"):
                part_files = entry.get("part_files") or [entry["file_path"]]
                for part_index, part_file in enumerate(part_files):
                    unit_id = str(entry["unit_id"])
                    if len(part_files) > 1:
                        html_name = f"{unit_id}_part{part_index + 1}.html"
                    else:
                        html_name = f"{unit_id}.html"
                    result.append(
                        (
                            html_name,
                            entry,
                            part_index,
                            Path(part_file).stem,
                        )
                    )
            if entry.get("children"):
                visit(entry["children"])

    visit(structure)
    return result


def relocate_leading_page_carryover(
    generated_html: Dict[str, str],
    structure: List[Dict],
) -> List[Dict[str, object]]:
    """Move page-boundary carry-over before the preceding unit's footnotes.

    The legacy PDF refinement only knows that a TOC unit starts on a page; it
    cannot identify the exact line.  A translated unit can therefore begin
    with the final paragraphs of the preceding unit.  Moving only the TOC
    link would leave those paragraphs after the preceding unit's footnotes.

    This pass operates on the generated XHTML, leaving validated Markdown
    immutable.  It moves leading non-heading blocks before the current unit's
    title to the preceding Markdown-backed HTML file, and moves any footnote
    definitions used exclusively by those blocks with them.  Footnote links
    are rewritten to the destination file so the link graph remains closed.
    """
    from collections import Counter

    from bs4 import BeautifulSoup
    from bs4.element import Tag

    ordered = _ordered_markdown_html_entries(structure)
    records: List[Dict[str, object]] = []
    heading_names = {f"h{level}" for level in range(1, 7)}

    def iter_tags(nodes):
        for node in nodes:
            if isinstance(node, Tag):
                yield node
                yield from node.find_all(True)

    def footnote_keys(nodes, aliases: set[str]) -> Counter[str]:
        counts: Counter[str] = Counter()
        for tag in iter_tags(nodes):
            if tag.name.lower() != "a":
                continue
            classes = tag.get("class") or []
            if "footnote-ref" not in classes:
                continue
            href = str(tag.get("href") or "")
            base, separator, fragment = href.partition("#")
            if not separator or not fragment.startswith("fn-"):
                continue
            if base and base not in aliases:
                continue
            counts[fragment[3:]] += 1
        return counts

    def rewrite_footnote_links(
        nodes,
        current_aliases: set[str],
        current_text_aliases: set[str],
        previous_filename: str,
        current_stems: set[str],
        previous_stem: str,
    ) -> None:
        for tag in iter_tags(nodes):
            tag_id = str(tag.get("id") or "")
            for current_stem in current_stems:
                prefix = f"fnref-{current_stem}-"
                if tag_id.startswith(prefix):
                    tag["id"] = f"fnref-{previous_stem}-{tag_id[len(prefix):]}"
                    break
            href = tag.get("href")
            if not isinstance(href, str):
                continue
            base, separator, fragment = href.partition("#")
            if not separator:
                if href.startswith("#fnref-"):
                    for current_stem in current_stems:
                        prefix = f"#fnref-{current_stem}-"
                        if href.startswith(prefix):
                            tag["href"] = f"#fnref-{previous_stem}-{href[len(prefix):]}"
                            break
                continue
            rewritten_fragment = fragment
            for current_stem in current_stems:
                prefix = f"fnref-{current_stem}-"
                if rewritten_fragment.startswith(prefix):
                    rewritten_fragment = (
                        f"fnref-{previous_stem}-"
                        f"{rewritten_fragment[len(prefix):]}"
                    )
                    break
            if base in current_aliases:
                tag["href"] = f"{previous_filename}#{rewritten_fragment}"
            elif base in {"text/" + alias for alias in current_text_aliases}:
                tag["href"] = f"text/{previous_filename}#{rewritten_fragment}"

    previous_content: Optional[Tuple[str, Path, str]] = None

    for html_name, entry, part_index, source_stem in ordered:
        if part_index != 0 or html_name not in generated_html:
            if html_name in generated_html:
                previous_content = (html_name, Path(html_name), source_stem)
            continue

        current_aliases = {html_name, f"{source_stem}.html"}
        current_text_aliases = set(current_aliases)
        soup = BeautifulSoup(generated_html[html_name], "html.parser")
        body = soup.body
        anchor = str(entry.get("anchor") or "").lstrip("#")
        target = body.find(id=anchor) if body is not None and anchor else None
        if body is None or target is None or previous_content is None:
            previous_content = (html_name, Path(html_name), source_stem)
            continue

        direct_nodes = list(body.children)
        try:
            target_index = direct_nodes.index(target)
        except ValueError:
            previous_content = (html_name, Path(html_name), source_stem)
            continue

        # Ancestor headings are structural context and must stay with the
        # current unit.  Only the initial non-heading blocks are carry-over.
        leading_nodes: List[Tag] = []
        for node in direct_nodes[:target_index]:
            if not isinstance(node, Tag):
                continue
            if node.name.lower() in heading_names:
                break
            leading_nodes.append(node)

        if not leading_nodes:
            previous_content = (html_name, Path(html_name), source_stem)
            continue

        previous_filename, _previous_path, previous_stem = previous_content
        previous_html = generated_html.get(previous_filename)
        if not previous_html:
            previous_content = (html_name, Path(html_name), source_stem)
            continue
        previous_soup = BeautifulSoup(previous_html, "html.parser")
        previous_body = previous_soup.body
        if previous_body is None:
            previous_content = (html_name, Path(html_name), source_stem)
            continue

        moved_refs = footnote_keys(leading_nodes, current_aliases)
        all_refs = footnote_keys([soup], current_aliases)
        movable_keys = {
            key
            for key, count in moved_refs.items()
            if count == all_refs.get(key, 0)
            and soup.find(id=f"fn-{key}") is not None
            and previous_soup.find(id=f"fn-{key}") is None
        }

        current_stems = {source_stem, Path(html_name).stem}
        for node in leading_nodes:
            rewrite_footnote_links(
                [node],
                current_aliases,
                current_text_aliases,
                previous_filename,
                current_stems,
                previous_stem,
            )
            node.extract()

        definitions = []
        for definition in soup.find_all(class_="footnote-def"):
            key = str(definition.get("id") or "")
            if not key.startswith("fn-") or key[3:] not in movable_keys:
                continue
            if definition is None:
                continue
            rewrite_footnote_links(
                [definition],
                current_aliases,
                current_text_aliases,
                previous_filename,
                current_stems,
                previous_stem,
            )
            definition.extract()
            definitions.append(definition)

        insertion_point = next(
            (
                index
                for index, node in enumerate(previous_body.contents)
                if isinstance(node, Tag)
                and "footnote-def" in (node.get("class") or [])
            ),
            len(previous_body.contents),
        )
        for node in [*leading_nodes, *definitions]:
            previous_body.insert(insertion_point, node)
            insertion_point += 1

        generated_html[previous_filename] = str(previous_soup)
        generated_html[html_name] = str(soup)
        records.append(
            {
                "source": html_name,
                "destination": previous_filename,
                "moved_blocks": len(leading_nodes),
                "moved_footnotes": sorted(movable_keys),
            }
        )
        logger.info(
            f"Moved {len(leading_nodes)} page-boundary block(s) from "
            f"{html_name} to {previous_filename}; "
            f"moved footnotes: {sorted(movable_keys) or 'none'}"
        )
        previous_content = (html_name, Path(html_name), source_stem)

    return records


def load_refinement_part_order(output_dir: Path) -> Dict[str, List[str]]:
    """Load authoritative continuation order from the refinement checkpoint."""
    progress_path = Path(output_dir) / "ocr_markdown" / "tree_progress.json"
    try:
        progress = json.loads(progress_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {}
    result: Dict[str, List[str]] = {}
    for unit in progress.get("units", []) or []:
        if not isinstance(unit, dict):
            continue
        names = [str(name) for name in (unit.get("part_files") or []) if name]
        if len(names) > 1 and all(
            Path(name).name == name and Path(name).suffix.lower() == ".md"
            for name in names
        ):
            result[names[0]] = names
    return result


def write_combined_markdown(
    structure: List[Dict],
    output_path: Path,
) -> Path:
    """Write Markdown in the same chapter/part order used for the EPUB."""
    parts: List[str] = []
    seen_files = set()

    def collect(entries: List[Dict]) -> None:
        for entry in entries:
            file_path = entry.get("file_path")
            if file_path:
                part_files = entry.get("part_files") or [file_path]
                for part_file in part_files:
                    part_path = Path(part_file)
                    if part_path in seen_files or not part_path.is_file():
                        continue
                    seen_files.add(part_path)
                    content = part_path.read_text(encoding="utf-8").strip()
                    if content:
                        parts.append(content)
            children = entry.get("children") or []
            if children:
                collect(children)

    collect(structure)
    if not parts:
        raise ValueError("No Markdown content found for combined output")

    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text("\n\n".join(parts) + "\n", encoding="utf-8")
    logger.success(f"Combined Markdown created: {output_path}")
    return output_path


def generate_hierarchical_toc_ncx(
    structure: List[Dict],
    book_title: str,
    output_path: Path,
    uid: Optional[str] = None,
    language: str = "en",
    anchor_locations: Optional[Dict[str, str]] = None,
) -> bool:
    """
    Generate NCX TOC with unlimited hierarchy levels.

    Args:
        structure: Hierarchical structure from build_epub_structure
        book_title: Book title
        output_path: Path to save NCX file

    Returns:
        True if successful
    """
    import html
    import uuid

    uid = uid or str(uuid.uuid4())
    toc_label = localized_toc_label(language)
    anchor_locations = anchor_locations or {}

    # Calculate actual depth from structure (+1 for TOC entry)
    toc_depth = calculate_tree_depth(structure) + 1

    ncx = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE ncx PUBLIC "-//NISO//DTD ncx 2005-1//EN" "http://www.daisy.org/z3986/2005/ncx-2005-1.dtd">
<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/" version="2005-1">
    <head>
        <meta name="dtb:uid" content="{uid}"/>
        <meta name="dtb:depth" content="{toc_depth}"/>
        <meta name="dtb:totalPageCount" content="0"/>
        <meta name="dtb:maxPageNumber" content="0"/>
    </head>
    <docTitle>
        <text>{html.escape(book_title)}</text>
    </docTitle>
    <navMap>
        <navPoint id="navpoint-toc" playOrder="1">
            <navLabel>
                <text>{html.escape(toc_label)}</text>
            </navLabel>
            <content src="text/toc.html"/>
        </navPoint>
"""

    nav_counter = [2]
    next_play_order = [2]
    href_play_orders = {"text/toc.html": 1}

    def render_entry(entry: Dict, indent: int = 2, parent_href: str = "text/toc.html") -> str:
        """Recursively render a nav entry and its children."""
        result = ""
        spaces = "    " * indent
        title = html.escape(entry['title'])

        # Determine the href
        if entry.get('virtual_file_name'):
            href = f"text/{entry['virtual_file_name']}"
        elif 'file_path' in entry:
            # Has content file
            unit_id = entry.get('unit_id')
            if unit_id:
                # Check if this chapter has multiple parts
                part_files = entry.get('part_files', [])
                if len(part_files) > 1:
                    href = f"text/{unit_id}_part1.html"
                else:
                    href = f"text/{unit_id}.html"
            else:
                href = parent_href  # Use parent's file
            # A PDF page is not a sufficiently precise TOC target when a
            # unit begins part-way through that page.  The builder adds the
            # stable unit anchor to the actual heading below; keep the
            # navigation link pointed at that heading rather than the file
            # start.  Anchors that were never placed keep the file-level
            # target so navigation never points at a missing fragment.
            anchor = str(entry.get('anchor') or '').lstrip('#')
            located_file = anchor_locations.get(anchor) if anchor else None
            if located_file:
                href = f"text/{located_file}#{anchor}"
        else:
            # No file, link to first child with file, or parent's file
            href = find_first_child_href(entry, parent_href)
            anchor = str(entry.get('anchor') or '').lstrip('#')
            if anchor:
                located_file = anchor_locations.get(anchor)
                if located_file:
                    href = f"text/{located_file}#{anchor}"

        nav_id = f"navpoint-{nav_counter[0]}"
        nav_counter[0] += 1
        if href not in href_play_orders:
            href_play_orders[href] = next_play_order[0]
            next_play_order[0] += 1
        play_order = href_play_orders[href]

        result += f"""{spaces}<navPoint id="{nav_id}" playOrder="{play_order}">
{spaces}    <navLabel>
{spaces}        <text>{title}</text>
{spaces}    </navLabel>
{spaces}    <content src="{href}"/>
"""

        # Render children, passing current href as parent
        if 'children' in entry:
            for child in entry['children']:
                result += render_entry(child, indent + 1, href)

        result += f"{spaces}</navPoint>\n"
        return result

    def find_first_child_href(entry: Dict, parent_href: str = "text/toc.html") -> str:
        """Find href of first descendant with a file, or fall back to parent."""
        if entry.get('virtual_file_name'):
            return f"text/{entry['virtual_file_name']}"
        if 'file_path' in entry:
            unit_id = entry.get('unit_id')
            if unit_id:
                # Check if this chapter has multiple parts
                part_files = entry.get('part_files', [])
                if len(part_files) > 1:
                    return f"text/{unit_id}_part1.html"
                else:
                    return f"text/{unit_id}.html"
            return parent_href
        if 'children' in entry:
            for child in entry['children']:
                href = find_first_child_href(child, parent_href)
                if href != "text/toc.html" and href != parent_href:
                    return href
        # No file found in this subtree, use parent's file
        return parent_href

    # Render all top-level entries
    for entry in structure:
        ncx += render_entry(entry)

    ncx += """    </navMap>
</ncx>"""

    try:
        with open(output_path, 'w', encoding='utf-8') as f:
            f.write(ncx)
        logger.success(f"Created hierarchical NCX TOC: {output_path}")
        return True
    except Exception as e:
        logger.error(f"Failed to create NCX: {e}")
        return False


def generate_hierarchical_toc_html(
    structure: List[Dict],
    book_title: str,
    output_path: Path,
    language: str = "en",
    anchor_locations: Optional[Dict[str, str]] = None,
) -> bool:
    """
    Generate HTML TOC with unlimited hierarchy levels.

    Args:
        structure: Hierarchical structure from build_epub_structure
        book_title: Book title
        output_path: Path to save HTML file
        language: Language code for HTML

    Returns:
        True if successful
    """
    import html

    lang_class = f"lang-{language}"
    toc_label = localized_toc_label(language)
    anchor_locations = anchor_locations or {}

    toc_html = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE html PUBLIC "-//W3C//DTD XHTML 1.1//EN" "http://www.w3.org/TR/xhtml11/DTD/xhtml11.dtd">
<html xmlns="http://www.w3.org/1999/xhtml" xml:lang="{language}" lang="{language}">
<head>
    <title>{html.escape(toc_label)}</title>
    <link rel="stylesheet" type="text/css" href="../stylesheet.css"/>
    <meta http-equiv="Content-Type" content="text/html; charset=utf-8"/>
</head>
<body class="{lang_class}">
    <div class="toc">
        <h1>{html.escape(toc_label)}</h1>
"""

    def render_entry(entry: Dict, level: int = 0, parent_href: str = "toc.html") -> str:
        """Recursively render a TOC entry and its children."""
        result = ""
        indent = "            " + "    " * level

        title = html.escape(entry['title'])

        # Determine the href
        if entry.get('virtual_file_name'):
            href = entry['virtual_file_name']
        elif 'file_path' in entry:
            unit_id = entry.get('unit_id')
            if unit_id:
                # Check if this chapter has multiple parts
                part_files = entry.get('part_files', [])
                if len(part_files) > 1:
                    href = f"{unit_id}_part1.html"
                else:
                    href = f"{unit_id}.html"
            else:
                href = parent_href  # Use parent's file
            # See the NCX generator above: file-level targets are refined to
            # the current unit's heading whenever a stable anchor exists.
            # Unknown anchors keep the file-level target instead of emitting
            # a fragment that the generated HTML does not contain.
            anchor = str(entry.get('anchor') or '').lstrip('#')
            located_file = anchor_locations.get(anchor) if anchor else None
            if located_file:
                href = f"{located_file}#{anchor}"
        else:
            # No file - link to first child with file, or parent's file
            href = find_first_child_href_html(entry, parent_href)
            anchor = str(entry.get('anchor') or '').lstrip('#')
            if anchor:
                located_file = anchor_locations.get(anchor)
                if located_file:
                    href = f"{located_file}#{anchor}"

        result += f"{indent}<li>\n"
        result += f"{indent}    <a href=\"{href}\">{title}</a>\n"

        # Render children, passing current href as parent
        if 'children' in entry and entry['children']:
            result += f"{indent}    <ul>\n"
            for child in entry['children']:
                result += render_entry(child, level + 1, href)
            result += f"{indent}    </ul>\n"

        result += f"{indent}</li>\n"
        return result

    def find_first_child_href_html(entry: Dict, parent_href: str = "toc.html") -> str:
        """Find href of first descendant with a file, or fall back to parent."""
        if entry.get('virtual_file_name'):
            return entry['virtual_file_name']
        if 'file_path' in entry:
            unit_id = entry.get('unit_id')
            if unit_id:
                # Check if this chapter has multiple parts
                part_files = entry.get('part_files', [])
                if len(part_files) > 1:
                    return f"{unit_id}_part1.html"
                else:
                    return f"{unit_id}.html"
            return parent_href
        if 'children' in entry:
            for child in entry['children']:
                href = find_first_child_href_html(child, parent_href)
                if href != "toc.html" and href != parent_href:
                    return href
        # No file found in this subtree, use parent's file
        return parent_href

    toc_html += "        <ul>\n"
    for entry in structure:
        toc_html += render_entry(entry)
    toc_html += "        </ul>\n"

    toc_html += """    </div>
</body>
</html>"""

    try:
        with open(output_path, 'w', encoding='utf-8') as f:
            f.write(toc_html)
        logger.success(f"Created hierarchical HTML TOC: {output_path}")
        return True
    except Exception as e:
        logger.error(f"Failed to create HTML TOC: {e}")
        return False


def build_epub(config: BuildEpubConfig) -> Path:
    """
    Main entry point for building EPUB from toc_tree.json.

    Args:
        config: Build configuration

    Returns:
        Path to generated EPUB file
    """
    import shutil
    from .epub.builder import EpubBuilder
    from .epub.converter import ContentConverter
    from .epub.footnotes import FootnoteManager, validate_footnote_graph

    logger.info(f"Building EPUB for: {config.book_title}")
    logger.info(f"Source: {'translated' if config.translated else 'polished_markdown'}")

    # Load toc_tree.json
    toc_tree = load_toc_tree(config.toc_tree_path)
    original_toc_tree = toc_tree
    logger.info(f"Loaded toc_tree.json with {len(toc_tree.get('chapters', []))} top-level entries")

    # Extract cover image from PDF if not provided
    if not config.cover_image:
        cover_page_info = toc_tree.get('cover_page') or {}
        cover_page_num = cover_page_info.get('page_number')

        # Fallback: use page 1 as cover if no cover page specified
        if not cover_page_num:
            cover_page_num = 1
            logger.info("No cover page in TOC, using page 1 as cover")

        if cover_page_num:
            # Look for original PDF (without page number patches) for clean cover
            pdf_path = config.output_dir / "input_original.pdf"
            if not pdf_path.exists():
                pdf_path = config.output_dir / "input.pdf"

            if pdf_path.exists():
                # Extract cover to images directory
                images_dir = config.images_dir or (config.output_dir / "images")
                images_dir.mkdir(parents=True, exist_ok=True)
                cover_output = images_dir / "cover.jpg"

                extracted = extract_cover_image(pdf_path, cover_output, cover_page_num)
                if extracted:
                    config.cover_image = extracted
                    logger.info(f"Extracted cover from PDF page {cover_page_num}")
            else:
                logger.warning("Cannot extract cover: PDF not found in output directory")

    # Load translated TOC if building translated EPUB
    if config.translated:
        from .toc_translation_workflow import validate_toc_translation_subagent

        toc_validation = validate_toc_translation_subagent(config.output_dir)
        if not toc_validation["valid"]:
            raise ValueError(
                "Translated TOC validation failed: "
                + "; ".join(toc_validation["errors"][:10])
            )
        translated_toc_path = config.output_dir / "toc_tree_translated.json"
        if not translated_toc_path.exists():
            raise FileNotFoundError(
                f"{translated_toc_path} is missing. Complete the TOC translation "
                "result and run translate-toc-validate before building."
            )
        with open(translated_toc_path, 'r', encoding='utf-8') as f:
            toc_tree = json.load(f)
        logger.info("Loaded locally merged, validated toc_tree_translated.json")

        translated_title = toc_tree.get('book_title')
        if translated_title:
            config.book_title = translated_title
            logger.info(f"Using translated title: {config.book_title}")

    # Flatten and process structure
    toc_structure = flatten_toc_tree(
        toc_tree['chapters'],
        use_translated_titles=config.translated,
    )

    # Build structure with file paths
    epub_structure = build_epub_structure(
        toc_structure,
        config.markdown_dir,
        part_order=load_refinement_part_order(config.output_dir),
    )
    from .refine.footnote_options import resolve_footnote_options

    footnote_mode = resolve_footnote_options(config.config).mode
    endnote_build_report = (
        validate_centralized_endnotes_for_build(epub_structure)
        if footnote_mode != "disabled"
        else {"valid": True, "definition_keys": [], "errors": [], "status": "disabled"}
    )
    if not endnote_build_report["valid"]:
        raise ValueError(
            "Centralized endnote normalization is incomplete: "
            + "; ".join(endnote_build_report["errors"][:10])
        )

    # Count chapters with files
    def count_with_files(entries):
        count = 0
        for e in entries:
            if 'file_path' in e or 'virtual_file_name' in e:
                count += 1
            if 'children' in e:
                count += count_with_files(e['children'])
        return count

    chapters_with_files = count_with_files(epub_structure)
    logger.info(f"Found markdown files for {chapters_with_files} chapters")

    # Create temporary EPUB directory
    epub_dir = config.output_dir / "epub_build"
    if epub_dir.exists():
        shutil.rmtree(epub_dir)
    epub_dir.mkdir(parents=True)

    # Create EPUB directory structure
    (epub_dir / "META-INF").mkdir()
    (epub_dir / "text").mkdir()
    (epub_dir / "images").mkdir()

    # Copy and compress images, get mapping
    image_mapping = {}
    if config.images_dir and config.images_dir.exists():
        images_dest = epub_dir / "images"
        for img_file in config.images_dir.iterdir():
            if img_file.is_file() and img_file.suffix.lower() in ['.jpg', '.jpeg', '.png', '.gif']:
                dest_path = images_dest / img_file.name
                shutil.copy(img_file, dest_path)
                # Map original name to itself (no renaming for now)
                image_mapping[img_file.name] = img_file.name
        logger.info(f"Copied {len(image_mapping)} images from {config.images_dir}")

    # Create a minimal config object for EpubBuilder and ContentConverter
    class MinimalConfig:
        def __init__(self, book_title, author, language, markdown_dir, publisher):
            self.book_title = book_title
            self.author = author
            self.publisher = publisher
            self.language = language
            self.markdown_dir = markdown_dir

    metadata = resolve_book_metadata(
        [original_toc_tree, toc_tree], config.config, config.output_dir
    )
    author = metadata["author"] or "Unknown"
    publisher = metadata["publisher"]
    language = 'zh' if config.translated else toc_tree.get('language', 'en')
    if language.lower() in ['english', 'japanese', 'chinese']:
        language = {'english': 'en', 'japanese': 'ja', 'chinese': 'zh'}.get(language.lower(), 'en')

    minimal_config = MinimalConfig(
        config.book_title, author, language, config.markdown_dir, publisher
    )
    builder = EpubBuilder(minimal_config)

    # Validated Markdown is an immutable staging contract.  Do not run
    # heuristic heading cleanup here: those helpers write back to
    # ``config.markdown_dir`` and would make the packaged EPUB differ from
    # the content that just passed translate/polish validation.  Structural
    # cleanup belongs before validation (or in a separate generated copy).
    converter = ContentConverter(minimal_config)

    if config.combined_markdown_path:
        write_combined_markdown(epub_structure, config.combined_markdown_path)

    # Initialize FootnoteManager after cleanup so scanned refs/defs match the
    # markdown that will actually be converted.
    auto_global = has_notes_chapter(toc_tree.get('chapters', []))
    if auto_global:
        logger.info("Detected notes chapter, enabling global footnote mode")
    footnote_manager = FootnoteManager(
        config.markdown_dir,
        auto_global=auto_global,
        epub_structure=epub_structure,
    )
    converter.footnote_manager = footnote_manager
    logger.debug(f"Initialized FootnoteManager in {footnote_manager.style.value} mode")

    # Create basic EPUB files
    builder.create_mimetype(epub_dir / "mimetype")
    builder.create_container_xml(epub_dir / "META-INF" / "container.xml")
    builder.create_stylesheet(epub_dir / "stylesheet.css")

    # Process and convert chapters
    all_html_files = []
    generated_html = {}
    anchor_locations: dict[str, str] = {}

    def process_chapters(entries: List[Dict]):
        """Recursively process all chapters with files."""
        for entry in entries:
            if entry.get('virtual_file_name'):
                # Structural-only parents get a small standalone page so the
                # parent navigation target cannot land on the first child's
                # prose.  The actual child Markdown remains immutable.
                heading = f"{'#' * max(1, int(entry.get('level') or 1))} {entry['title']}"
                html_content = markdown_to_html(
                    heading,
                    config.book_title,
                    language,
                    footnote_manager=footnote_manager,
                    image_mapping=image_mapping,
                    source_chapter=Path(entry['virtual_file_name']).stem,
                )
                html_filename = entry['virtual_file_name']
                html_path = epub_dir / "text" / html_filename
                with open(html_path, 'w', encoding='utf-8') as f:
                    f.write(html_content)
                all_html_files.append(html_filename)
                generated_html[html_filename] = html_content
                logger.debug(f"Created structural container {html_filename}")

            if 'file_path' in entry:
                unit_id = entry.get('unit_id')
                if unit_id:
                    # Process this chapter
                    part_files = entry.get('part_files', [entry['file_path']])
                    rendered_parts: list[tuple[str, str]] = []

                    for part_idx, part_file in enumerate(part_files):
                        is_first_part = (part_idx == 0)

                        # Read markdown content
                        with open(part_file, 'r', encoding='utf-8') as f:
                            content = f.read()

                        # Process content with correct title and levels
                        processed = process_chapter_content(
                            entry['title'],
                            entry['level'],
                            content,
                            is_first_part
                        )

                        # Convert to HTML with full footnote and image support
                        # Use actual part file name for correct footnote linking
                        part_file_stem = Path(part_file).stem
                        html_content = markdown_to_html(
                            processed,
                            config.book_title,
                            language,
                            footnote_manager=footnote_manager,
                            image_mapping=image_mapping,
                            source_chapter=part_file_stem
                        )

                        # Page-only TOC boundaries can leave the current
                        # unit's heading after a short carry-over from the
                        # preceding page/section.  Give the current entry a
                        # stable fragment at its actual heading so navigation
                        # lands on the semantic section start.
                        # Determine output filename using unit_id
                        if len(part_files) > 1:
                            html_filename = f"{unit_id}_part{part_idx + 1}.html"
                        else:
                            html_filename = f"{unit_id}.html"

                        # Page-only TOC boundaries can leave the current
                        # unit's heading after a short carry-over from the
                        # preceding page/section.  Give the current entry a
                        # stable fragment at its actual heading so navigation
                        # lands on the semantic section start.
                        if is_first_part and entry.get('anchor'):
                            html_content, anchor_added = converter._add_subchapter_anchor(
                                html_content,
                                entry['title'],
                                entry['anchor'],
                            )
                            if anchor_added:
                                anchor_locations[
                                    str(entry['anchor']).lstrip('#')
                                ] = html_filename
                            else:
                                logger.warning(
                                    f"Could not locate heading for stable anchor "
                                    f"#{entry['anchor']}: '{entry['title']}'"
                                )

                        rendered_parts.append((html_filename, html_content))

                    # A child without its own Markdown unit is rendered inside
                    # this logical unit.  Locate it across every generated
                    # part, then bind its TOC anchor to that exact HTML file.
                    child_entries = _embedded_subchapter_entries(
                        entry.get('children', [])
                    )
                    if child_entries:
                        try:
                            from .chapter_identity import ChapterIdentity

                            identity = ChapterIdentity.parse(unit_id)
                            chapter_index = (
                                identity.index_path[0]
                                if identity and identity.index_path
                                else 0
                            )
                            if chapter_index:
                                rendered_parts, child_locations = (
                                    add_subchapter_anchors_to_parts(
                                        converter,
                                        rendered_parts,
                                        chapter_index,
                                        [
                                            {
                                                'title': child['title'],
                                                'anchor': child.get('anchor'),
                                            }
                                            for child in child_entries
                                        ],
                                    )
                                )
                                anchor_locations.update(child_locations)
                        except Exception as exc:
                            logger.debug(
                                f"Could not add subchapter anchors for {unit_id}: {exc}"
                            )

                    for html_filename, html_content in rendered_parts:
                        html_path = epub_dir / "text" / html_filename
                        with open(html_path, 'w', encoding='utf-8') as f:
                            f.write(html_content)

                        all_html_files.append(html_filename)
                        generated_html[html_filename] = html_content
                        logger.debug(f"Created {html_filename}")

            # Process children
            if 'children' in entry:
                process_chapters(entry['children'])

    process_chapters(epub_structure)
    logger.info(f"Converted {len(all_html_files)} HTML files")
    relocation_records = relocate_leading_page_carryover(
        generated_html,
        epub_structure,
    )
    if relocation_records:
        changed_files = {
            filename
            for record in relocation_records
            for filename in (record["source"], record["destination"])
        }
        for filename in changed_files:
            (epub_dir / "text" / str(filename)).write_text(
                generated_html[str(filename)],
                encoding="utf-8",
            )
        logger.info(
            f"Applied {len(relocation_records)} page-boundary carry-over relocation(s)"
        )

    # Navigation is generated after conversion so child anchors can point to
    # the actual split part containing their heading rather than always part 1.
    generate_hierarchical_toc_ncx(
        epub_structure,
        config.book_title,
        epub_dir / "toc.ncx",
        uid=builder.uid,
        language=language,
        anchor_locations=anchor_locations,
    )
    generate_hierarchical_toc_html(
        epub_structure,
        config.book_title,
        epub_dir / "text" / "toc.html",
        language,
        anchor_locations=anchor_locations,
    )

    footnote_report = validate_footnote_graph(generated_html)
    if auto_global and footnote_manager.definitions and footnote_report["unlinked_sup_count"]:
        raise ValueError(
            "Global footnote graph contains unlinked references: "
            + "; ".join(
                f"{filename}#{sup_id}"
                for filename, sup_id, _text in footnote_report["unlinked_samples"][:10]
            )
        )
    if footnote_report["unlinked_sup_count"]:
        logger.warning(
            f"Kept {footnote_report['unlinked_sup_count']} ambiguous footnote "
            "reference(s) visible but unlinked"
        )
    logger.info(
        "Validated footnote graph: "
        f"{footnote_report['forward_hrefs']} forward link(s), "
        f"{footnote_report['backref_hrefs']} backlink(s)"
    )

    # Handle cover image
    cover_image = None
    if config.cover_image and config.cover_image.exists():
        cover_filename = config.cover_image.name
        shutil.copy(config.cover_image, epub_dir / "images" / cover_filename)
        cover_image = cover_filename
        # Create cover HTML
        builder.create_cover_html(cover_filename, epub_dir / "text" / "cover.html")

    # Create content.opf
    # Convert our structure to the format expected by create_content_opf
    flat_structure = {
        'book_title': config.book_title,
        'chapters': []  # We use all_html_files instead
    }
    builder.create_content_opf(
        flat_structure,
        epub_dir,
        epub_dir / "content.opf",
        cover_image,
        all_html_files
    )

    # Create final EPUB with sanitized filename
    from .utils.common import sanitize_filename
    safe_title = sanitize_filename(config.book_title)
    epub_path = config.output_epub or (config.output_dir / f"{safe_title}.epub")
    builder.create_epub(epub_dir, epub_path)

    # Keep build directory for debugging (don't clean up)
    # shutil.rmtree(epub_dir)

    logger.success(f"EPUB created: {epub_path}")
    logger.info(f"Build directory kept for debugging: {epub_dir}")
    return epub_path


def markdown_to_html(
    markdown_content: str,
    book_title: str,
    language: str = "en",
    footnote_manager=None,
    image_mapping=None,
    source_chapter=None
) -> str:
    """
    Convert markdown to XHTML for EPUB.

    Uses the full conversion pipeline with LaTeX/math support.
    """
    import html
    from .markdown_to_html import convert_markdown_to_html

    # Use full conversion with math/formula processing
    # standalone=False returns just the body content
    body = convert_markdown_to_html(
        markdown_content,
        title=book_title,
        include_css=False,
        standalone=False,
        image_mapping=image_mapping,
        footnote_manager=footnote_manager,
        source_chapter=source_chapter
    )

    lang_class = f"lang-{language}"

    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE html PUBLIC "-//W3C//DTD XHTML 1.1//EN" "http://www.w3.org/TR/xhtml11/DTD/xhtml11.dtd">
<html xmlns="http://www.w3.org/1999/xhtml" xml:lang="{language}" lang="{language}">
<head>
    <title>{html.escape(book_title)}</title>
    <link rel="stylesheet" type="text/css" href="../stylesheet.css"/>
    <meta http-equiv="Content-Type" content="text/html; charset=utf-8"/>
</head>
<body class="{lang_class}">
{body}
</body>
</html>"""


# CLI entry point will be added in cli.py
