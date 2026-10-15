"""Role lookup for refined PDF units.

This is a read-only projection of refinement output.  It is shared by PDF
translation and one-time repair commands without making either command depend
on the other.
"""

from __future__ import annotations

import json
from pathlib import Path

from .utils.unit_id import generate_unit_id


PDF_SPECIAL_UNIT_ROLES = frozenset(
    {"bibliography", "index", "notes", "author_notes", "catalog", "ad"}
)


def load_pdf_file_roles(output_dir: Path) -> dict[str, str]:
    """Load special PDF unit roles from current or legacy refinement output."""
    output_dir = Path(output_dir)
    progress = output_dir / "ocr_markdown" / "tree_progress.json"
    if not progress.is_file():
        return {}
    try:
        data = json.loads(progress.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}

    roles: dict[str, str] = {}
    for unit in data.get("units", []):
        role = str(unit.get("type") or "").strip().lower()
        if role not in PDF_SPECIAL_UNIT_ROLES:
            continue
        for file_name in unit.get("part_files") or [unit.get("file")]:
            if file_name:
                roles[str(file_name)] = role
    if roles:
        return roles

    # Older checkpoints predate the ``type`` field.  Fall back to the current
    # TOC so adding a type does not invalidate an otherwise resumable run.
    toc_path = output_dir / "toc_tree.json"
    try:
        toc = json.loads(toc_path.read_text(encoding="utf-8"))

        def visit(nodes, path: list[int]) -> None:
            for index, node in enumerate(nodes or [], 1):
                node_path = path + [index]
                role = str(node.get("type") or "").strip().lower()
                if role in PDF_SPECIAL_UNIT_ROLES:
                    roles[f"{generate_unit_id(node_path)}.md"] = role
                visit(node.get("children", []), node_path)

        visit(toc.get("chapters", []), [])
    except (OSError, json.JSONDecodeError, AttributeError, TypeError, ValueError):
        pass
    return roles


__all__ = ["PDF_SPECIAL_UNIT_ROLES", "load_pdf_file_roles"]
