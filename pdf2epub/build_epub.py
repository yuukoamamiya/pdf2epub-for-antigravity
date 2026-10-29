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
import re
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass
from loguru import logger

from .epub.builder import EpubBuilder, localized_toc_label
from .epub.converter import ContentConverter
from .utils.unit_id import generate_unit_id
from .utils.pdf_utils import extract_cover_image
from .toc_navigation import (
    ResolvedNavigationNode,
    navigation_depth,
    resolve_navigation,
    serialize_html_navigation,
    serialize_ncx_navigation,
    validate_navigation_rendering,
)


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
        anchor_id = anchor.lstrip("#")
        existing = []
        for filename, html_content in rendered_parts:
            matches = list(re.finditer(
                rf'(?<![\w:-])id=["\']{re.escape(anchor_id)}["\']', html_content, re.I
            ))
            existing.extend([filename] * len(matches))
        if existing:
            if len(existing) == 1 and converter._has_subchapter_anchor(
                next(content for filename, content in rendered_parts if filename == existing[0]),
                title,
                anchor_id,
            ):
                locations[anchor_id] = existing[0]
            else:
                logger.warning(
                    "Stable anchor #{} already exists outside a unique matching heading",
                    anchor,
                )
            continue

        source_occurrence = info.get("source_occurrence")
        source_match_count = info.get("source_match_count")
        if source_occurrence is not None or source_match_count is not None:
            matches = []
            for part_index, (_filename, html_content) in enumerate(rendered_parts):
                updated, found = converter._add_subchapter_anchor(
                    html_content,
                    title,
                    anchor_id,
                    source_occurrence=source_occurrence,
                    source_match_count=source_match_count,
                )
                if found:
                    matches.append((part_index, updated))
            if len(matches) == 1:
                part_index, updated = matches[0]
                filename, _old_content = rendered_parts[part_index]
                rendered_parts[part_index] = (filename, updated)
                locations[anchor_id] = filename
                continue
            logger.warning(
                f"Could not uniquely bind source occurrence for stable anchor #{anchor}: '{title}'"
            )
            continue

        candidates = []
        for part_index, (_filename, html_content) in enumerate(rendered_parts):
            rank, count, has_number_conflict = converter._subchapter_anchor_match_rank(html_content, title)
            if count or has_number_conflict:
                candidates.append((rank, part_index, count, has_number_conflict))
        if candidates:
            best_rank = min(rank for rank, _, _, _ in candidates)
            best = [(index, count) for rank, index, count, _ in candidates if rank == best_rank]
            has_conflict = any(conflict for _, _, _, conflict in candidates)
        else:
            best = []
            has_conflict = False
        if len(best) == 1 and best[0][1] == 1 and not (best_rank[0] == 2 and has_conflict):
            part_index = best[0][0]
            filename, html_content = rendered_parts[part_index]
            updated, found = converter._add_subchapter_anchor(html_content, title, anchor_id)
            if found:
                rendered_parts[part_index] = (filename, updated)
                locations[anchor_id] = filename
                continue
        if not best or len(best) != 1 or best[0][1] != 1 or (best_rank[0] == 2 and has_conflict):
            logger.warning(
                f"Could not locate heading for stable anchor #{anchor}: '{title}'"
            )
    return rendered_parts, locations


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
        from .toc_model import toc_node_anchor, toc_node_id

        entry = {
            'title': title,
            'level': chapter.get('level', 1),
            'index_path': index_path,
            'node_id': toc_node_id(chapter, tuple(index_path)),
            'anchor': toc_node_anchor(chapter, tuple(index_path)),
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
        if 'node_id' in entry:
            result['node_id'] = entry['node_id']
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
    resolved_navigation: Optional[Tuple[ResolvedNavigationNode, ...]] = None,
) -> bool:
    """Write NCX using a resolved navigation tree (or resolve a legacy call)."""
    import html
    import uuid

    nodes = resolved_navigation if resolved_navigation is not None else resolve_navigation(structure, anchor_locations)
    toc_label = localized_toc_label(language)
    uid = uid or str(uuid.uuid4())
    depth = max(1, navigation_depth(nodes))
    content = f'''<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE ncx PUBLIC "-//NISO//DTD ncx 2005-1//EN" "http://www.daisy.org/z3986/2005/ncx-2005-1.dtd">
<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/" version="2005-1"><head>
<meta name="dtb:uid" content="{html.escape(uid, quote=True)}"/><meta name="dtb:depth" content="{depth}"/>
<meta name="dtb:totalPageCount" content="0"/><meta name="dtb:maxPageNumber" content="0"/></head>
<docTitle><text>{html.escape(book_title)}</text></docTitle><navMap>
<navPoint id="navpoint-toc" playOrder="1"><navLabel><text>{html.escape(toc_label)}</text></navLabel><content src="text/toc.html"/></navPoint>
{serialize_ncx_navigation(nodes)}</navMap></ncx>'''
    try:
        Path(output_path).write_text(content, encoding="utf-8")
        logger.success(f"Created hierarchical NCX TOC: {output_path}")
        return True
    except Exception as exc:
        logger.error(f"Failed to create NCX: {exc}")
        return False

def generate_hierarchical_toc_html(
    structure: List[Dict],
    book_title: str,
    output_path: Path,
    language: str = "en",
    anchor_locations: Optional[Dict[str, str]] = None,
    resolved_navigation: Optional[Tuple[ResolvedNavigationNode, ...]] = None,
) -> bool:
    """Write XHTML navigation from the same resolved model used for NCX."""
    import html

    nodes = resolved_navigation if resolved_navigation is not None else resolve_navigation(structure, anchor_locations)
    toc_label = localized_toc_label(language)
    lang_class = f"lang-{language}"
    content = f'''<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE html PUBLIC "-//W3C//DTD XHTML 1.1//EN" "http://www.w3.org/TR/xhtml11/DTD/xhtml11.dtd">
<html xmlns="http://www.w3.org/1999/xhtml" xml:lang="{language}" lang="{language}"><head>
<title>{html.escape(toc_label)}</title><link rel="stylesheet" type="text/css" href="../stylesheet.css"/>
<meta http-equiv="Content-Type" content="text/html; charset=utf-8"/></head><body class="{lang_class}">
<div class="toc"><h1>{html.escape(toc_label)}</h1><ul>
{serialize_html_navigation(nodes)}</ul></div></body></html>'''
    try:
        Path(output_path).write_text(content, encoding="utf-8")
        logger.success(f"Created hierarchical HTML TOC: {output_path}")
        return True
    except Exception as exc:
        logger.error(f"Failed to create HTML TOC: {exc}")
        return False


def _load_build_toc(config: BuildEpubConfig) -> Tuple[Dict, Dict]:
    """Load source and translated TOC data, including configured cover lookup."""
    toc_tree = load_toc_tree(config.toc_tree_path)
    original_toc_tree = toc_tree
    logger.info(
        f"Loaded toc_tree.json with {len(toc_tree.get('chapters', []))} top-level entries"
    )

    if not config.cover_image:
        cover_page_info = toc_tree.get('cover_page') or {}
        cover_page_num = cover_page_info.get('page_number')
        if not cover_page_num:
            cover_page_num = 1
            logger.info("No cover page in TOC, using page 1 as cover")

        if cover_page_num:
            pdf_path = config.output_dir / "input_original.pdf"
            if not pdf_path.exists():
                pdf_path = config.output_dir / "input.pdf"
            if pdf_path.exists():
                images_dir = config.images_dir or (config.output_dir / "images")
                images_dir.mkdir(parents=True, exist_ok=True)
                cover_output = images_dir / "cover.jpg"
                extracted = extract_cover_image(pdf_path, cover_output, cover_page_num)
                if extracted:
                    config.cover_image = extracted
                    logger.info(f"Extracted cover from PDF page {cover_page_num}")
            else:
                logger.warning("Cannot extract cover: PDF not found in output directory")

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

    return original_toc_tree, toc_tree


def _prepare_epub_structure(
    config: BuildEpubConfig, toc_tree: Dict, source_toc_tree: Optional[Dict] = None
) -> List[Dict]:
    """Resolve TOC units to Markdown files and enforce endnote prerequisites."""
    toc_structure = flatten_toc_tree(
        toc_tree['chapters'],
        use_translated_titles=config.translated,
    )
    epub_structure = build_epub_structure(
        toc_structure,
        config.markdown_dir,
        part_order=load_refinement_part_order(config.output_dir),
    )
    if source_toc_tree is None:
        raise ValueError("EPUB navigation requires the current source TOC ownership contract")
    if source_toc_tree is not None:
        from .toc_model import (
            TOC_TREE_SCHEMA_VERSION,
            project_toc_ownership,
            validate_toc_tree_correspondence,
        )

        progress_path = config.output_dir / "ocr_markdown" / "tree_progress.json"
        if not progress_path.is_file():
            raise ValueError("EPUB navigation requires current tree_progress.json ownership")
        if progress_path.is_file():
            progress = json.loads(progress_path.read_text(encoding="utf-8"))
            if source_toc_tree.get("schema_version") != TOC_TREE_SCHEMA_VERSION:
                raise ValueError("Unsupported source TOC schema; rerun refine-prepare and structure review")
            if toc_tree.get("schema_version") != TOC_TREE_SCHEMA_VERSION:
                raise ValueError("Unsupported translated TOC schema; rerun translate-toc")
            identity_errors = validate_toc_tree_correspondence(source_toc_tree, toc_tree)
            if identity_errors:
                raise ValueError("Invalid source/translated TOC correspondence: " + "; ".join(identity_errors))
            source_texts = {}
            missing_source_files = []
            for unit in progress.get("units", []):
                for name in unit.get("part_files") or [unit.get("file")]:
                    if name:
                        source_path = config.output_dir / "ocr_markdown" / str(name)
                        if source_path.is_file():
                            source_texts[str(name)] = source_path.read_text(encoding="utf-8")
                        else:
                            missing_source_files.append(str(name))
            if missing_source_files:
                raise ValueError(
                    "TOC ownership source Markdown is missing: "
                    + ", ".join(sorted(set(missing_source_files))[:10])
                )
            ownership = project_toc_ownership(
                source_toc_tree.get("chapters", []), progress, source_texts
            )
            translated_nodes = {}

            def index_translated(entries):
                for item in entries:
                    translated_nodes[item["node_id"]] = item
                    index_translated(item.get("children") or [])

            index_translated(epub_structure)
            binding_modes = {}
            anchors_by_source_file = {}
            for projection in ownership.values():
                for heading in projection.get("headings", []):
                    identity = heading["node_id"]
                    mode = heading.get("binding_mode")
                    if mode not in {"visible", "container_only"}:
                        raise ValueError(f"Invalid TOC ownership binding for {identity}: {mode}")
                    if mode == "visible" or identity not in binding_modes:
                        binding_modes[identity] = mode
                    target_node = translated_nodes.get(identity)
                    if target_node is None:
                        raise ValueError(f"TOC ownership refers to unknown translated node: {identity}")
                    owner = heading.get("owner_file")
                    if not owner:
                        raise ValueError(f"TOC ownership is missing physical file for {identity}")
                    anchors_by_source_file.setdefault(str(owner), []).append({
                        "node_id": identity,
                        "title": target_node["title"],
                        "anchor": target_node.get("anchor"),
                        "source_occurrence": heading.get("source_occurrence"),
                        "source_match_count": heading.get("source_match_count"),
                    })

            def apply_binding(entries):
                for entry in entries:
                    identity = entry.get("node_id")
                    if identity not in binding_modes:
                        raise ValueError(f"TOC node has no physical ownership projection: {identity}")
                    entry["navigation_binding_mode"] = binding_modes[identity]
                    entry["navigation_anchor_entries_by_part"] = {
                        Path(name).name: anchors_by_source_file.get(Path(name).name, [])
                        for name in (entry.get("part_files") or ([entry["file_path"]] if entry.get("file_path") else []))
                    }
                    apply_binding(entry.get("children") or [])

            apply_binding(epub_structure)
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

    def count_with_files(entries):
        count = 0
        for entry in entries:
            if 'file_path' in entry or 'virtual_file_name' in entry:
                count += 1
            if 'children' in entry:
                count += count_with_files(entry['children'])
        return count

    logger.info(f"Found markdown files for {count_with_files(epub_structure)} chapters")
    return epub_structure


def _prepare_epub_staging(config: BuildEpubConfig) -> Tuple[Path, Dict[str, str]]:
    """Recreate the build staging tree and copy supported images."""
    import shutil

    epub_dir = config.output_dir / "epub_build"
    if epub_dir.exists():
        shutil.rmtree(epub_dir)
    epub_dir.mkdir(parents=True)
    (epub_dir / "META-INF").mkdir()
    (epub_dir / "text").mkdir()
    (epub_dir / "images").mkdir()

    image_mapping = {}
    if config.images_dir and config.images_dir.exists():
        images_dest = epub_dir / "images"
        for img_file in config.images_dir.iterdir():
            if img_file.is_file() and img_file.suffix.lower() in ['.jpg', '.jpeg', '.png', '.gif']:
                shutil.copy(img_file, images_dest / img_file.name)
                image_mapping[img_file.name] = img_file.name
        logger.info(f"Copied {len(image_mapping)} images from {config.images_dir}")
    return epub_dir, image_mapping


def _render_epub_content(
    config: BuildEpubConfig,
    epub_dir: Path,
    epub_structure: List[Dict],
    language: str,
    footnote_manager,
    auto_global: bool,
    validate_footnote_graph,
    image_mapping: Dict[str, str],
    converter: ContentConverter,
    builder: EpubBuilder,
) -> List[str]:
    """Render chapter XHTML, relocate page carry-over, and write navigation."""
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
                        # Read markdown content
                        with open(part_file, 'r', encoding='utf-8') as f:
                            content = f.read()

                        # Process content with correct title and levels
                        # The validated Markdown and canonical ownership
                        # projection are authoritative. Replacing the first
                        # similar heading or pruning a duplicate by text can
                        # erase a distinct accepted TOC node.
                        processed = content

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

                        # Determine output filename using unit_id
                        if len(part_files) > 1:
                            html_filename = f"{unit_id}_part{part_idx + 1}.html"
                        else:
                            html_filename = f"{unit_id}.html"

                        rendered_parts.append((html_filename, html_content))

                    # Place every canonical ownership-projected heading in
                    # its source-assigned physical part, including visible
                    # structural ancestors embedded in descendant units.
                    for part_idx, part_file in enumerate(part_files):
                        filename, html_content = rendered_parts[part_idx]
                        anchors_for_part = entry.get("navigation_anchor_entries_by_part", {}).get(
                            Path(part_file).name, []
                        )
                        if anchors_for_part:
                            from .chapter_identity import ChapterIdentity

                            identity = ChapterIdentity.parse(unit_id)
                            chapter_index = identity.index_path[0] if identity and identity.index_path else 0
                            if not chapter_index:
                                raise ValueError(f"Cannot derive chapter index for {unit_id}")
                            anchored, locations = add_subchapter_anchors_to_parts(
                                converter,
                                [(filename, html_content)],
                                chapter_index,
                                anchors_for_part,
                            )
                            rendered_parts[part_idx] = anchored[0]
                            anchor_locations.update(locations)

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
    resolved_navigation = resolve_navigation(
        epub_structure,
        anchor_locations,
        generated_files=set(all_html_files),
    )
    validate_navigation_rendering(resolved_navigation, generated_html)
    ncx_written = generate_hierarchical_toc_ncx(
        epub_structure,
        config.book_title,
        epub_dir / "toc.ncx",
        uid=builder.uid,
        language=language,
        anchor_locations=anchor_locations,
        resolved_navigation=resolved_navigation,
    )
    html_toc_written = generate_hierarchical_toc_html(
        epub_structure,
        config.book_title,
        epub_dir / "text" / "toc.html",
        language,
        anchor_locations=anchor_locations,
        resolved_navigation=resolved_navigation,
    )
    if not ncx_written or not html_toc_written:
        raise ValueError("Failed to serialize the resolved TOC navigation")

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

    return all_html_files


def _package_epub(
    config: BuildEpubConfig,
    epub_dir: Path,
    builder: EpubBuilder,
    all_html_files: List[str],
) -> Path:
    """Write package metadata and create the final EPUB archive."""
    import shutil

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
    from .utils.paths import sanitize_filename
    safe_title = sanitize_filename(config.book_title)
    epub_path = config.output_epub or (config.output_dir / f"{safe_title}.epub")
    builder.create_epub(epub_dir, epub_path)

    # Keep build directory for debugging (don't clean up)
    # shutil.rmtree(epub_dir)

    logger.success(f"EPUB created: {epub_path}")
    logger.info(f"Build directory kept for debugging: {epub_dir}")
    return epub_path



def build_epub(config: BuildEpubConfig) -> Path:
    """
    Main entry point for building EPUB from toc_tree.json.

    Args:
        config: Build configuration

    Returns:
        Path to generated EPUB file
    """
    from .epub.builder import EpubBuilder
    from .epub.converter import ContentConverter
    from .epub.footnotes import FootnoteManager, validate_footnote_graph

    logger.info(f"Building EPUB for: {config.book_title}")
    logger.info(f"Source: {'translated' if config.translated else 'polished_markdown'}")

    original_toc_tree, toc_tree = _load_build_toc(config)

    epub_structure = _prepare_epub_structure(config, toc_tree, original_toc_tree)

    epub_dir, image_mapping = _prepare_epub_staging(config)

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

    all_html_files = _render_epub_content(
        config,
        epub_dir,
        epub_structure,
        language,
        footnote_manager,
        auto_global,
        validate_footnote_graph,
        image_mapping,
        converter,
        builder,
    )

    return _package_epub(config, epub_dir, builder, all_html_files)

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
