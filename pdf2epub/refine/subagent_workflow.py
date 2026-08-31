"""File-based workflow helpers for Antigravity PDF refinement.

The subagent only performs the judgment-heavy TOC reading step.  This module
creates its hand-off instructions and validates the resulting JSON locally;
page merging and work-unit generation remain deterministic Python operations.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional

from pdf2epub.subagent_workflow import resolve_subagent_model
from .pagination import build_pagination_map


PAGE_RE = re.compile(r"^page_(\d+)\.md$")


def page_numbers(pages_dir: Path) -> List[int]:
    """Return the numeric page files available in an OCR directory."""
    numbers = []
    for path in pages_dir.glob("page_*.md"):
        match = PAGE_RE.match(path.name)
        if match:
            numbers.append(int(match.group(1)))
    return sorted(set(numbers))


def prepare_refine_subagent(
    output_dir: Path,
    book_title: str,
    max_tokens: int,
    config: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Path]:
    """Write the prompt and manifest consumed by the Antigravity subagent."""
    pages_dir = output_dir / "pages"
    available = page_numbers(pages_dir)
    if not available:
        raise ValueError(f"OCR pages not found in {pages_dir}; run ocr-pages first")

    model = resolve_subagent_model(config, "refine")
    manifest = {
        "schema_version": 1,
        "workflow": "antigravity-subagent",
        "book_title": book_title,
        "pages_dir": "pages",
        "page_count": max(available),
        "available_pages": available,
        "max_tokens_per_unit": max_tokens,
        "model": model,
        "output_file": "toc_tree.json",
        "pagination_map": "pagination_map.json",
    }
    manifest_path = output_dir / "refine_subagent_manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    pagination_path = output_dir / "pagination_map.json"
    build_pagination_map(pages_dir, pagination_path)

    prompt_path = output_dir / "refine_subagent_prompt.md"
    prompt_path.write_text(
        f"""# PDF structure refinement

Book: **{book_title}**
Recommended Antigravity model: `{model}`

Read all OCR Markdown files under `pages/` and write `toc_tree.json` in this
directory.  The file is consumed by a local deterministic step, so output
valid JSON only and do not add Markdown fences or commentary.

Before deciding chapter ranges, read `pagination_map.json`. It is a local
heuristic mapping between physical OCR pages and printed Roman/Arabic page
labels. Use it as supporting evidence for the table of contents; the physical
OCR page number remains authoritative in `start_page` and `end_page`. If the
map is uncertain, inspect the page text and record the uncertainty rather than
blindly applying an offset.

Use 1-based inclusive OCR page numbers.  Identify the book's real chapters and
sections from the page text, including nested sections.  Keep nodes ordered by
their first page.  Sibling nodes must not overlap; a section may contain its
children.  If two sections begin on the same page, include `boundary_info`
with a 1-based `start_line` where needed.

Required output shape:

```json
{{
  "schema_version": 1,
  "book_title": "{book_title}",
  "author": "Exact author name from the title or copyright page",
  "publisher": "Exact publisher name when visible",
  "metadata_source_pages": [1],
  "chapters": [
    {{
      "title": "Chapter title",
      "level": 1,
      "start_page": 1,
      "end_page": 10,
      "children": []
    }}
  ]
}}
```

Rules:

- `chapters` must not be empty.
- Every page range must exist in `pages/`, stay within the available page
  range, and use integer values.
- `level` starts at 1 and increases for nested children.
- Preserve meaningful title text from the OCR; do not invent page numbers.
- Inspect the title page, copyright page, and front matter for bibliographic metadata.
  Copy the author and publisher exactly as printed; do not translate, normalize,
  or guess them. Use an empty string only when the information is genuinely not
  visible, and record the relevant OCR page numbers in `metadata_source_pages`.
- You may include `type: "notes"`, `type: "bibliography"`, `type: "index"`,
  `boundary_info`, or other book metadata. Use `bibliography` for a references
  section and `index` for an index so the later translation prompt can apply
  the appropriate preservation rules.
  but do not change the required field names.
- The local step will estimate tokens using `{max_tokens}` as the unit limit;
  it will not call any model or provider.
""",
        encoding="utf-8",
    )

    return {"manifest": manifest_path, "prompt": prompt_path}


def validate_toc_tree_data(
    data: Any,
    total_pages: int,
    available_pages: Optional[Iterable[int]] = None,
) -> List[str]:
    """Validate a subagent TOC tree before it reaches the page merger."""
    errors: List[str] = []
    available = set(available_pages if available_pages is not None else range(1, total_pages + 1))

    if not isinstance(data, dict):
        return ["toc_tree.json must contain a JSON object"]
    chapters = data.get("chapters")
    if not isinstance(chapters, list) or not chapters:
        return ["toc_tree.json must contain a non-empty chapters array"]

    def visit(nodes: Any, parent: Optional[Dict[str, Any]], path: str) -> None:
        if not isinstance(nodes, list):
            errors.append(f"{path}.children must be an array")
            return

        previous: Optional[Dict[str, Any]] = None
        previous_path = ""
        for index, node in enumerate(nodes):
            node_path = f"{path}[{index}]"
            if not isinstance(node, dict):
                errors.append(f"{node_path} must be an object")
                continue

            title = node.get("title")
            level = node.get("level")
            start = node.get("start_page")
            end = node.get("end_page")
            if not isinstance(title, str) or not title.strip():
                errors.append(f"{node_path}.title must be a non-empty string")
            if not isinstance(level, int) or isinstance(level, bool) or level < 1:
                errors.append(f"{node_path}.level must be a positive integer")
            if not all(isinstance(value, int) and not isinstance(value, bool) for value in (start, end)):
                errors.append(f"{node_path} page range must use integer values")
                continue
            if start < 1 or end < start or end > total_pages:
                errors.append(f"{node_path} page range {start}-{end} is outside the OCR pages")
            missing = [page for page in range(max(1, start), min(total_pages, end) + 1) if page not in available]
            if missing:
                errors.append(f"{node_path} references missing OCR page(s): {missing[:5]}")
            if parent is not None:
                if start < parent["start_page"] or end > parent["end_page"]:
                    errors.append(f"{node_path} is outside its parent page range")
                if isinstance(level, int) and level <= parent.get("level", 0):
                    errors.append(f"{node_path}.level must be deeper than its parent")
            if previous is not None:
                if start < previous["start_page"]:
                    errors.append(f"{node_path} is out of page order after {previous_path}")
                elif start < previous["end_page"]:
                    current_boundary = node.get("boundary_info") or {}
                    previous_boundary = previous.get("boundary_info") or {}
                    same_page_split = (
                        start == previous.get("start_page") == previous.get("end_page")
                        and start == end
                        and isinstance(current_boundary.get("start_line"), int)
                        and isinstance(previous_boundary.get("start_line"), int)
                        and current_boundary["start_line"] > previous_boundary["start_line"]
                    )
                    if not same_page_split:
                        errors.append(f"{node_path} overlaps {previous_path}")
            previous = node
            previous_path = node_path

            boundary = node.get("boundary_info")
            if boundary is not None and not isinstance(boundary, dict):
                errors.append(f"{node_path}.boundary_info must be an object")
            elif isinstance(boundary, dict):
                for key in ("start_line", "end_line"):
                    if key in boundary and (
                        not isinstance(boundary[key], int) or boundary[key] < 1
                    ):
                        errors.append(f"{node_path}.boundary_info.{key} must be a positive integer")

            visit(node.get("children", []), node, f"{node_path}.children")

    visit(chapters, None, "chapters")
    return errors
