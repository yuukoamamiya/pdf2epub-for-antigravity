"""Read and resolve explicit roles for refined PDF units."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping

from .utils.unit_id import generate_unit_id


PDF_SPECIAL_UNIT_ROLES = frozenset({
    "bibliography", "index", "notes", "author_notes", "catalog", "ad", "toc",
    "copyright", "frontmatter", "metadata", "cover",
})
PDF_INHERITABLE_UNIT_ROLES = frozenset({"bibliography", "index", "notes", "author_notes"})


def _load_json(path: Path, label: str) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"Cannot read {label} JSON at {path}: {exc}") from exc


def _role(node: Mapping[str, Any], label: str) -> str | None:
    value = node.get("type")
    if value is not None and not isinstance(value, str):
        raise ValueError(f"Malformed role type for {label}: expected text")
    if value is None or not value.strip():
        return None
    normalized = value.strip().lower()
    if normalized not in PDF_SPECIAL_UNIT_ROLES:
        return None
    return normalized


def _validate_path(value: Any, label: str) -> tuple[int, ...] | None:
    if value is None:
        return None
    if (not isinstance(value, list) or not value
            or any(type(part) is not int or part < 1 for part in value)):
        raise ValueError(f"Malformed {label}: expected a non-empty list of positive integers")
    return tuple(value)


def load_pdf_file_role_details(output_dir: Path) -> dict[str, dict[str, Any]]:
    """Return per-part roles with provenance; malformed present artifacts fail closed.

    Generic TOC nodes do not interrupt an inherited bibliography/index/notes
    role. Other special roles apply only to their own node/unit.
    """
    output_dir = Path(output_dir)
    progress_path = output_dir / "ocr_markdown" / "tree_progress.json"
    if not progress_path.is_file():
        return {}
    progress = _load_json(progress_path, "tree_progress")
    if not isinstance(progress, Mapping) or not isinstance(progress.get("units"), list):
        raise ValueError(f"Malformed tree_progress JSON at {progress_path}: expected object with units list")
    units = progress["units"]
    if any(not isinstance(unit, Mapping) for unit in units):
        raise ValueError(f"Malformed tree_progress JSON at {progress_path}: every unit must be an object")

    toc_path = output_dir / "toc_tree.json"
    toc_paths: dict[tuple[int, ...], dict[str, Any]] = {}
    toc_ids: dict[str, tuple[int, ...]] = {}
    toc_has_ids = False
    if toc_path.is_file():
        toc = _load_json(toc_path, "toc_tree")
        if not isinstance(toc, Mapping) or not isinstance(toc.get("chapters"), list):
            raise ValueError(f"Malformed toc_tree JSON at {toc_path}: expected object with chapters list")

        def visit(nodes: list[Any], prefix: tuple[int, ...], inherited: str | None) -> None:
            nonlocal toc_has_ids
            for index, node in enumerate(nodes, 1):
                if not isinstance(node, Mapping):
                    raise ValueError(f"Malformed TOC node at index_path {list(prefix + (index,))}: expected object")
                path = prefix + (index,)
                identity = node.get("node_id")
                if identity is not None:
                    if not isinstance(identity, str) or not identity.strip():
                        raise ValueError(f"Malformed TOC node at index_path {list(path)}: node_id must be non-empty text")
                    if identity in toc_ids:
                        raise ValueError(f"Duplicate TOC node_id {identity!r}")
                    toc_ids[identity] = path
                    toc_has_ids = True
                explicit = _role(node, f"TOC node {identity or path}")
                effective = explicit or inherited
                if explicit:
                    origin = {"source": "toc_tree.type", "node_id": identity, "index_path": list(path)}
                elif effective and prefix:
                    parent = toc_paths[prefix]
                    origin = parent.get("role_origin")
                elif effective:
                    origin = None
                else:
                    origin = None
                toc_paths[path] = {
                    "explicit_role": explicit,
                    "role": effective,
                    "source": "toc_tree.node_id" if explicit else ("toc_tree.inherited" if effective else None),
                    "node_id": identity,
                    "role_origin": origin,
                }
                if explicit and explicit not in PDF_INHERITABLE_UNIT_ROLES:
                    child_inherited = None
                else:
                    child_inherited = effective
                children = node.get("children", [])
                if not isinstance(children, list):
                    raise ValueError(f"Malformed children for TOC node {identity or path}: expected list")
                visit(children, path, child_inherited)

        visit(toc["chapters"], (), None)

    by_filename: dict[str, dict[str, Any]] = {}
    all_names: set[str] = set()
    seen_node_ids: set[str] = set()
    legacy_checkpoint = True
    for unit in units:
        identity = unit.get("node_id")
        if identity is not None and (not isinstance(identity, str) or not identity.strip()):
            raise ValueError(f"Malformed progress unit node_id: {identity!r}")
        if isinstance(identity, str):
            if identity in seen_node_ids:
                raise ValueError(f"Duplicate progress unit node_id {identity!r}")
            seen_node_ids.add(identity)
        index_path = _validate_path(unit.get("index_path"), f"progress index_path for {identity or '<legacy>'}")
        if identity is not None or index_path is not None:
            legacy_checkpoint = False
        toc_path_for_unit = toc_ids.get(identity) if isinstance(identity, str) else None
        if isinstance(identity, str) and toc_has_ids and toc_path_for_unit is None:
            raise ValueError(f"Progress unit node_id {identity!r} is absent from toc_tree.json")
        if index_path is not None and toc_paths and index_path not in toc_paths:
            raise ValueError(f"Progress index_path {list(index_path)} is absent from toc_tree.json")
        if toc_path_for_unit and index_path and toc_path_for_unit != index_path:
            raise ValueError(f"Progress index_path disagrees with node_id {identity!r}")
        resolved_path = toc_path_for_unit or index_path
        toc_record = toc_paths.get(resolved_path) if resolved_path else None
        progress_role = _role(unit, f"progress unit {identity or resolved_path}")
        toc_explicit = toc_record.get("explicit_role") if toc_record else None
        if progress_role and toc_explicit and progress_role != toc_explicit:
            raise ValueError(
                f"Conflicting explicit roles for progress unit {identity or resolved_path}: "
                f"tree_progress={progress_role}, toc_tree={toc_explicit}"
            )
        role = progress_role or (toc_record.get("role") if toc_record else None)
        if "part_files" in unit:
            raw_names = unit["part_files"]
            if not isinstance(raw_names, list) or not raw_names:
                raise ValueError(f"Malformed filenames for progress unit {identity or resolved_path}: part_files must be non-empty list")
        else:
            raw_names = [unit.get("file")]
        if (not isinstance(raw_names, list) or any(not isinstance(name, str) or not name.strip()
                or Path(name).name != name or Path(name).suffix.lower() != ".md" for name in raw_names)):
            raise ValueError(f"Malformed filenames for progress unit {identity or resolved_path}: expected file or part_files")
        if len(set(raw_names)) != len(raw_names) or all_names.intersection(raw_names):
            raise ValueError(f"Duplicate physical filename ownership for progress unit {identity or resolved_path}")
        all_names.update(raw_names)
        role_origin = (
            {"source": "tree_progress.type", "node_id": identity,
             "index_path": list(resolved_path) if resolved_path else None}
            if progress_role else toc_record.get("role_origin") if toc_record else None
        )
        if role:
            for filename in raw_names:
                by_filename[filename] = {
                    "role": role,
                    "source": role_origin.get("source") if role_origin else "legacy_position",
                    "role_origin": role_origin or {"source": "legacy_position"},
                    "node_id": identity,
                    "index_path": list(resolved_path) if resolved_path else None,
                }

    # Legacy checkpoints without identities/paths used sequential generated ids.
    if toc_paths and legacy_checkpoint:
        for path, record in toc_paths.items():
            if record.get("role") and generate_unit_id(list(path)) + ".md" not in by_filename:
                legacy_name = f"{generate_unit_id(list(path))}.md"
                by_filename.setdefault(legacy_name, {
                    "role": record["role"],
                    "source": "legacy_position",
                    "role_origin": {
                        "source": "legacy_position",
                        "node_id": record.get("node_id"),
                        "index_path": list(path),
                    },
                    "node_id": record.get("node_id"), "index_path": list(path),
                })
    return by_filename


def load_pdf_file_roles(output_dir: Path) -> dict[str, str]:
    """Compatibility projection returning only filename to special role."""
    return {name: record["role"] for name, record in load_pdf_file_role_details(output_dir).items()}


__all__ = [
    "PDF_INHERITABLE_UNIT_ROLES", "PDF_SPECIAL_UNIT_ROLES",
    "load_pdf_file_role_details", "load_pdf_file_roles",
]
