"""
Main workflow coordinator for refined breakdown.

Orchestrates the entire refinement process:
1. Analyze PDF structure
2. Verify boundaries
3. Handle failures (re-breakdown, discover subsections)
4. Generate work units
5. Merge pages and save
"""

import json
import shutil
from pathlib import Path
from typing import List, Dict
from loguru import logger
import tiktoken

from ..utils.unit_id import generate_unit_id
from .toc_tree import TOCNode, dict_list_to_toc_tree
from .refiner_state import RefinerState
from .page_merger import PageMerger
from .subagent_workflow import page_numbers, validate_toc_tree_data

# Initialize tokenizer
tokenizer = tiktoken.get_encoding("cl100k_base")


def _insert_toc_chapter(toc_tree: List[TOCNode], toc_info: Dict) -> TOCNode:
    """Insert an authoritative TOC range without letting body nodes start in it."""
    toc_chapter = TOCNode(
        title="Table of Contents",
        level=1,
        start_page=toc_info['start_page'],
        end_page=toc_info['end_page'],
        children=[],
    )
    for chapter in toc_tree:
        if (
            toc_chapter.start_page
            <= chapter.start_page
            <= toc_chapter.end_page
        ):
            chapter.start_page = toc_chapter.end_page + 1
            if chapter.end_page < chapter.start_page:
                chapter.end_page = chapter.start_page
    for index, chapter in enumerate(toc_tree):
        if chapter.start_page >= toc_chapter.start_page:
            toc_tree.insert(index, toc_chapter)
            break
    else:
        toc_tree.append(toc_chapter)
    return toc_chapter


class RefinedBreakdown:
    """
    Main class for refined breakdown process.

    Workflow:
    1. Analyze PDF structure (extract recursive TOC tree)
    2. Verify boundaries for each node
    3. Handle verification failures
    4. Generate work units based on token limits
    5. Merge pages with precise boundary cutting
    """

    def __init__(
        self,
        config: Dict,
        max_tokens: int = None,
        max_workers: int = None,
        local_only: bool = True,
    ):
        """
        Initialize the refined breakdown processor.

        Args:
            config: Configuration dict (from config.yaml)
            max_tokens: Maximum tokens per unit (LLM limit). If None, uses model limit from config.
            max_workers: Maximum parallel workers for verification
        """
        self.config = config
        self.max_workers = max_workers or config.get('general', {}).get('max_concurrent_workers', 8)

        self.max_tokens = max_tokens or config.get('refine', {}).get('max_tokens', 8000)

        self.page_merger = PageMerger()
        self.state = RefinerState()

    def process_from_toc(
        self,
        pdf_path: Path,
        output_dir: Path,
        book_title: str,
        resume: bool = False,
    ) -> List[Dict]:
        """Generate units from a subagent-produced ``toc_tree.json`` locally.

        This is the second half of the Subagent workflow.  It validates the
        hand-off, estimates tokens, merges OCR pages, and writes the
        ``ocr_markdown`` artifacts used by the local build pipeline.
        """
        output_dir.mkdir(parents=True, exist_ok=True)
        pages_dir = output_dir / "pages"
        available = page_numbers(pages_dir)
        if not available:
            raise ValueError(f"Pages not found in {pages_dir}. Run 'pdf2epub ocr-pages' first.")

        toc_tree_file = output_dir / "toc_tree.json"
        if not toc_tree_file.exists():
            raise ValueError(
                f"{toc_tree_file} not found. Run 'pdf2epub refine-prepare', "
                "then ask the Antigravity subagent to write it."
            )
        try:
            toc_data = json.loads(toc_tree_file.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ValueError(f"Invalid TOC JSON: {exc}") from exc

        errors = validate_toc_tree_data(toc_data, max(available), available)
        if errors:
            raise ValueError("Invalid subagent TOC: " + "; ".join(errors[:10]))

        toc_tree = dict_list_to_toc_tree(toc_data["chapters"])
        book_metadata = {key: value for key, value in toc_data.items() if key != "chapters"}
        return self._generate_units_from_tree(
            toc_tree,
            book_metadata,
            pages_dir,
            output_dir,
            resume=resume,
        )

    def _generate_units_from_tree(
        self,
        toc_tree: List[TOCNode],
        book_metadata: Dict,
        pages_dir: Path,
        output_dir: Path,
        resume: bool = False,
    ) -> List[Dict]:
        """Shared deterministic token estimation, splitting, and page merge."""
        ocr_markdown_dir = output_dir / "ocr_markdown"
        state_file = output_dir / "refiner_state.json"
        tree_progress_file = ocr_markdown_dir / "tree_progress.json"

        if resume and tree_progress_file.exists():
            progress_data = json.loads(tree_progress_file.read_text(encoding="utf-8"))
            logger.success(
                f"Refined breakdown already complete: {len(progress_data.get('units', []))} units"
            )
            return progress_data.get("units", [])

        if ocr_markdown_dir.exists() and not tree_progress_file.exists():
            logger.warning("Found incomplete ocr_markdown; clearing it before regeneration")
            shutil.rmtree(ocr_markdown_dir)
        ocr_markdown_dir.mkdir(parents=True, exist_ok=True)

        self._estimate_all_tokens(toc_tree, pages_dir)
        work_units: List[Dict] = []
        for chapter_idx, chapter in enumerate(toc_tree):
            work_units.extend(
                self._generate_units_recursive(chapter, pages_dir, [chapter_idx + 1])
            )

        logger.info(f"Saving {len(work_units)} work units...")
        unit_metadata = self._save_units(work_units, pages_dir, ocr_markdown_dir)
        tree_progress_file.write_text(
            json.dumps(
                {"units": unit_metadata, "book_metadata": book_metadata},
                indent=2,
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        # A local run has no verification agent state, but recording completion
        # makes --resume deterministic and keeps the existing state format.
        self.state.verification_complete = True
        self.state.save(state_file)
        logger.success(f"Refined breakdown complete: {len(work_units)} units")
        return unit_metadata

    def _estimate_all_tokens(self, toc_tree: List[TOCNode], pages_dir: Path):
        """Recursively estimate tokens for all nodes."""
        for node in toc_tree:
            self._estimate_node_tokens(node, pages_dir)

    def _estimate_node_tokens(self, node: TOCNode, pages_dir: Path):
        """Estimate tokens for a node and its children."""
        # Estimate this node's tokens
        total_tokens = 0
        for page_num in range(node.start_page, node.end_page + 1):
            page_file = pages_dir / f"page_{page_num:03d}.md"
            if page_file.exists():
                content = page_file.read_text(encoding='utf-8')
                total_tokens += len(tokenizer.encode(content))

        node.estimated_tokens = total_tokens

        # Recursively estimate children
        for child in node.children:
            self._estimate_node_tokens(child, pages_dir)

    def _generate_units_recursive(
        self,
        node: TOCNode,
        pages_dir: Path,
        index_path: List[int]
    ) -> List[Dict]:
        """
        Recursively generate work units from a node.

        Args:
            node: Current TOC node
            pages_dir: Directory containing page files
            index_path: Hierarchical index path like [7, 1] for first child of 7th top-level

        Logic:
        - If leaf and tokens <= max_tokens: create unit
        - If leaf and tokens > max_tokens: try discover subsections
        - If has children and total <= max_tokens: create unit for whole node
        - If has children and total > max_tokens: recurse into children
        """
        # Case 1: Leaf node
        if node.is_leaf():
            if node.estimated_tokens <= self.max_tokens:
                return [self._create_unit(node, index_path)]
            else:
                # Large leaf node - create unit and let NestedPartProcessor handle splitting
                logger.info(f"'{node.title}' ({node.estimated_tokens} tokens) exceeds max_tokens, will be split by processor")
                return [self._create_unit(node, index_path)]

        # Case 2: Has children. If the direct children cover the complete
        # parent range, use leaf units so every TOC leaf has a real file and
        # EPUB navigation does not point at a non-existent chapter_N.M file.
        total_children_tokens = sum(child.estimated_tokens for child in node.children)

        if total_children_tokens <= self.max_tokens:
            cursor = node.start_page
            children_cover_parent = True
            for child in sorted(node.children, key=lambda item: item.start_page):
                if child.start_page > cursor:
                    children_cover_parent = False
                    break
                cursor = max(cursor, child.end_page + 1)
            children_cover_parent = children_cover_parent and cursor > node.end_page
            if not children_cover_parent:
                # Preserve parent-only introductory material in one file.
                return [self._create_unit(node, index_path, include_children=True)]
            return [
                unit
                for child_idx, child in enumerate(node.children)
                for unit in self._generate_units_recursive(
                    child, pages_dir, index_path + [child_idx + 1]
                )
            ]
        else:
            # Recurse into children
            units = []
            for child_idx, child in enumerate(node.children):
                # Build child's index path by appending 1-based child index
                child_index_path = index_path + [child_idx + 1]
                child_units = self._generate_units_recursive(
                    child, pages_dir, child_index_path
                )
                units.extend(child_units)
            return units

    def _create_unit(
        self,
        node: TOCNode,
        index_path: List[int],
        include_children: bool = False
    ) -> Dict:
        """Create a work unit dictionary from a node."""
        # Generate unit ID using hierarchical index
        unit_id = generate_unit_id(index_path)

        return {
            'unit_id': unit_id,
            'node': node,
            'index_path': index_path,
            'title': node.title,
            'start_page': node.start_page,
            'end_page': node.end_page,
            'token_count': node.estimated_tokens,
            'include_children': include_children
        }

    def _save_units(
        self,
        work_units: List[Dict],
        pages_dir: Path,
        output_dir: Path
    ) -> List[Dict]:
        """Save all work units to files."""
        unit_metadata = []

        for i, unit in enumerate(work_units):
            node = unit['node']

            # Get next node for end boundary cutting
            next_node = None
            if i + 1 < len(work_units):
                next_node = work_units[i + 1]['node']

            # Merge content
            if unit.get('include_children'):
                # Get all nodes to merge
                all_nodes = [node] + node.get_all_leaves()
                content = self.page_merger.merge_nodes_content(all_nodes, pages_dir, next_node)
            else:
                content = self.page_merger.merge_node_content(node, pages_dir, next_node)

            # Save file
            output_file = output_dir / f"{unit['unit_id']}.md"
            output_file.write_text(content, encoding='utf-8')

            # Create metadata
            metadata = {
                'unit_id': unit['unit_id'],
                'index_path': unit['index_path'],
                'title': unit['title'],
                'page_range': [unit['start_page'], unit['end_page']],
                'token_count': unit['token_count'],
                'file': str(output_file.name)
            }
            if node.chapter_type:
                metadata['type'] = node.chapter_type
            unit_metadata.append(metadata)

            logger.debug(f"Saved {unit['unit_id']}: {unit['token_count']} tokens")

        return unit_metadata
