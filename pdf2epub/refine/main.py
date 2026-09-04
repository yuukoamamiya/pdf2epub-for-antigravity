"""Deterministic local half of the PDF refinement workflow."""

import json
import hashlib
import shutil
from pathlib import Path
from typing import Any, List, Dict
from loguru import logger
import tiktoken

from ..utils.unit_id import generate_unit_id
from .toc_tree import TOCNode, dict_list_to_toc_tree
from .page_merger import PageMerger
from .subagent_workflow import page_numbers, validate_toc_tree_data
from .unit_splitter import split_markdown_unit

# Initialize tokenizer
tokenizer = tiktoken.get_encoding("cl100k_base")


REFINE_CHECKPOINT_SCHEMA = 3
DEFAULT_OVERSIZED_SPLIT_THRESHOLD = 15_000
DEFAULT_OVERSIZED_SPLIT_TARGET = 12_000
DEFAULT_OVERSIZED_SPLIT_TYPES = ("all",)


def _pages_fingerprint(pages_dir: Path) -> str:
    """Hash page names and contents so resume cannot reuse stale OCR input."""
    digest = hashlib.sha256()
    for page in sorted(pages_dir.glob("page_*.md")):
        digest.update(page.name.encode("utf-8"))
        digest.update(hashlib.sha256(page.read_bytes()).digest())
    return digest.hexdigest()


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
    """Validate a Subagent TOC and generate local Markdown work units."""

    def __init__(
        self,
        config: Dict,
        max_tokens: int = None,
    ):
        """
        Initialize the refined breakdown processor.

        Args:
            config: Configuration dict (from config.yaml)
            max_tokens: Maximum tokens per unit (LLM limit). If None, uses model limit from config.
        """
        self.config = config
        refine_config = config.get("refine", {})
        if not isinstance(refine_config, dict):
            refine_config = {}
        self.max_tokens = max_tokens or refine_config.get("max_tokens", 8000)

        self.page_merger = PageMerger()
        split_config = refine_config.get("oversized_unit_split", {})
        if not isinstance(split_config, dict):
            split_config = {}
        self.oversized_split_enabled = split_config.get("enabled", True) is not False
        self.oversized_split_threshold = self._positive_int(
            split_config.get("threshold_tokens"), DEFAULT_OVERSIZED_SPLIT_THRESHOLD
        )
        configured_target = split_config.get("target_tokens")
        if configured_target is None:
            subagent_config = config.get("subagent", {})
            batching = subagent_config.get("batching", {}) if isinstance(subagent_config, dict) else {}
            configured_target = batching.get("max_source_tokens") if isinstance(batching, dict) else None
        self.oversized_split_target = self._positive_int(
            configured_target, DEFAULT_OVERSIZED_SPLIT_TARGET
        )
        if self.oversized_split_target >= self.oversized_split_threshold:
            self.oversized_split_target = max(1, self.oversized_split_threshold - 1)
        # New configurations split every oversized refined unit by default.
        # Keep an explicit ``types`` list as an escape hatch for older books
        # that intentionally only split notes/bibliographies/indexes.
        configured_types = split_config.get("types", list(DEFAULT_OVERSIZED_SPLIT_TYPES))
        if not isinstance(configured_types, list):
            configured_types = list(DEFAULT_OVERSIZED_SPLIT_TYPES)
        self.oversized_split_types = tuple(
            sorted({str(value).strip().lower() for value in configured_types if str(value).strip()})
        ) or DEFAULT_OVERSIZED_SPLIT_TYPES

    @staticmethod
    def _positive_int(value: Any, default: int) -> int:
        try:
            value = int(value)
        except (TypeError, ValueError):
            return default
        return value if value > 0 else default

    @property
    def split_policy(self) -> Dict[str, Any]:
        """Return the policy that must participate in resume fingerprints."""
        return {
            "enabled": self.oversized_split_enabled,
            "threshold_tokens": self.oversized_split_threshold,
            "target_tokens": self.oversized_split_target,
            "types": list(self.oversized_split_types),
        }

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
        source_fingerprint = {
            "schema": REFINE_CHECKPOINT_SCHEMA,
            "toc_sha256": hashlib.sha256(toc_tree_file.read_bytes()).hexdigest(),
            "pages_sha256": _pages_fingerprint(pages_dir),
            "split_policy": self.split_policy,
        }
        return self._generate_units_from_tree(
            toc_tree,
            book_metadata,
            pages_dir,
            output_dir,
            resume=resume,
            source_fingerprint=source_fingerprint,
        )

    def _generate_units_from_tree(
        self,
        toc_tree: List[TOCNode],
        book_metadata: Dict,
        pages_dir: Path,
        output_dir: Path,
        resume: bool = False,
        source_fingerprint: Dict[str, str] = None,
    ) -> List[Dict]:
        """Shared deterministic token estimation, splitting, and page merge."""
        ocr_markdown_dir = output_dir / "ocr_markdown"
        tree_progress_file = ocr_markdown_dir / "tree_progress.json"

        if resume and tree_progress_file.exists():
            progress_data = json.loads(tree_progress_file.read_text(encoding="utf-8"))
            checkpoint = progress_data.get("fingerprint")
            if source_fingerprint and checkpoint == source_fingerprint:
                logger.success(
                    f"Refined breakdown already complete: {len(progress_data.get('units', []))} units"
                )
                return progress_data.get("units", [])
            logger.warning("Refinement inputs changed; regenerating OCR work units")
            shutil.rmtree(ocr_markdown_dir)

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
                {
                    "schema": REFINE_CHECKPOINT_SCHEMA,
                    "fingerprint": source_fingerprint or {},
                    "units": unit_metadata,
                    "book_metadata": book_metadata,
                },
                indent=2,
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
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
                # Keep the logical TOC unit intact here. Any enabled oversized
                # unit is split after page merge, where Markdown block and
                # page-separator boundaries are available.
                logger.info(
                    f"'{node.title}' ({node.estimated_tokens} tokens) exceeds max_tokens; "
                    "oversized-unit policy will be applied after merge"
                )
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

            role = str(getattr(node, "chapter_type", "") or "").strip().lower() or "body"
            actual_tokens = len(tokenizer.encode(content)) if content else 0
            split_all_types = "all" in self.oversized_split_types or "*" in self.oversized_split_types
            should_split = (
                self.oversized_split_enabled
                and (split_all_types or role in self.oversized_split_types)
                and actual_tokens > self.oversized_split_threshold
            )
            split_result = (
                split_markdown_unit(
                    content,
                    self.oversized_split_target,
                    role,
                    lambda value: len(tokenizer.encode(value)),
                )
                if should_split
                else None
            )
            part_contents = split_result.parts if split_result else [content]
            part_files = []
            for part_index, part_content in enumerate(part_contents, 1):
                suffix = f".part{part_index}" if len(part_contents) > 1 else ""
                output_file = output_dir / f"{unit['unit_id']}{suffix}.md"
                output_file.write_text(part_content, encoding='utf-8')
                part_files.append(output_file.name)

            # Create metadata
            metadata = {
                'unit_id': unit['unit_id'],
                'index_path': unit['index_path'],
                'title': unit['title'],
                'page_range': [unit['start_page'], unit['end_page']],
                'token_count': unit['token_count'],
                'file': part_files[0],
            }
            if len(part_files) > 1:
                metadata.update(
                    {
                        "part_files": part_files,
                        "part_token_counts": [
                            len(tokenizer.encode(part_content)) for part_content in part_contents
                        ],
                        "split_strategy": split_result.strategy,
                        "split_target_tokens": self.oversized_split_target,
                    }
                )
            if node.chapter_type:
                metadata['type'] = node.chapter_type
            unit_metadata.append(metadata)

            logger.debug(f"Saved {unit['unit_id']}: {unit['token_count']} tokens")

        return unit_metadata
