"""Canonical identity and traversal helpers for modern nested TOC trees."""
from __future__ import annotations

from collections import Counter
import hashlib
import re
import unicodedata
from typing import Any, Iterator, Mapping, Sequence

TOCPath = tuple[int, ...]
TOC_TREE_SCHEMA_VERSION = 2


def _valid_explicit_anchor(value: str) -> bool:
    if not value:
        return False
    start_categories = {"Lu", "Ll", "Lt", "Lm", "Lo", "Nl"}
    rest_categories = start_categories | {"Mn", "Mc", "Nd", "Pc"}
    first = value[0]
    if first not in "_:" and unicodedata.category(first) not in start_categories:
        return False
    return all(
        char in "_.:-" or unicodedata.category(char) in rest_categories
        for char in value[1:]
    )


def iter_toc_nodes(
    chapters: Any, parent_path: TOCPath = ()
) -> Iterator[tuple[TOCPath, Mapping[str, Any]]]:
    """Yield each mapping node in preorder with its 1-based structural path."""
    if not isinstance(chapters, list):
        return
    for index, node in enumerate(chapters, 1):
        if not isinstance(node, Mapping):
            continue
        path = parent_path + (index,)
        yield path, node
        yield from iter_toc_nodes(node.get("children", []), path)


def _path_text(path: Sequence[int]) -> str:
    return ".".join(str(part) for part in path)


def toc_node_id(node: Mapping[str, Any], path: Sequence[int]) -> str:
    """Return the required explicit stable ID; paths are never node identity."""
    value = node.get("node_id")
    if isinstance(value, str) and value.strip():
        if value != value.strip():
            raise ValueError(f"TOC node at {_path_text(path)} has surrounding whitespace in node_id")
        return value
    raise ValueError(f"TOC node at {_path_text(path)} is missing required node_id")


def toc_node_anchor(node: Mapping[str, Any], path: Sequence[int]) -> str:
    """Return explicit anchor or a stable node_id-derived anchor."""
    toc_node_id(node, path)
    value = node.get("anchor")
    if isinstance(value, str) and value.strip():
        if value != value.strip() or not _valid_explicit_anchor(value):
            raise ValueError(f"TOC node at {_path_text(path)} has an unsafe explicit anchor")
        return value
    node_id = node.get("node_id")
    if isinstance(node_id, str) and node_id.strip():
        raw = node_id.strip()
        safe = re.sub(r"[^A-Za-z0-9_-]+", "-", raw).strip("-")
        if safe != raw or not safe:
            safe = (safe or "node") + "-" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]
        return f"toc-{safe}"
    raise ValueError(f"TOC node at {_path_text(path)} is missing required node_id")


def validate_toc_node_identities(chapters: Any) -> list[str]:
    """Report missing/repeated explicit IDs and anchors."""
    ids: list[str] = []
    anchors: list[str] = []
    errors = []
    for path, node in iter_toc_nodes(chapters):
        try:
            ids.append(toc_node_id(node, path))
            anchors.append(toc_node_anchor(node, path))
        except ValueError as exc:
            errors.append(str(exc))
    errors.extend(f"duplicate TOC node identity: {v}" for v, n in Counter(ids).items() if n > 1)
    errors.extend(f"duplicate TOC anchor: {v}" for v, n in Counter(anchors).items() if n > 1)
    for path, node in iter_toc_nodes(chapters):
        explicit = node.get("anchor")
        if explicit is not None and (
            not isinstance(explicit, str)
            or not explicit.strip()
            or explicit != explicit.strip()
            or not _valid_explicit_anchor(explicit)
        ):
            errors.append(f"invalid TOC anchor at {_path_text(path)}")
        binding_mode = node.get("binding_mode", "visible")
        if not isinstance(binding_mode, str) or binding_mode not in {"visible", "container_only"}:
            errors.append(f"invalid TOC binding_mode at {_path_text(path)}")
    return errors


def toc_nodes_by_id(tree: Mapping[str, Any]) -> dict[str, tuple[TOCPath, Mapping[str, Any]]]:
    """Validate and index a schema-v2 tree by canonical node_id."""
    if type(tree.get("schema_version")) is not int or tree.get("schema_version") != TOC_TREE_SCHEMA_VERSION:
        raise ValueError(f"unsupported TOC schema; expected {TOC_TREE_SCHEMA_VERSION}")
    chapters = tree.get("chapters")
    if not isinstance(chapters, list):
        raise ValueError("TOC tree must contain a chapters array")
    errors = validate_toc_node_identities(chapters)
    if errors:
        raise ValueError("invalid TOC identity contract: " + "; ".join(errors))
    return {toc_node_id(node, path): (path, node) for path, node in iter_toc_nodes(chapters)}


def validate_toc_tree_correspondence(
    source: Mapping[str, Any], translated: Mapping[str, Any]
) -> list[str]:
    """Require identical canonical node IDs at identical structural paths."""
    try:
        source_nodes = toc_nodes_by_id(source)
        translated_nodes = toc_nodes_by_id(translated)
    except ValueError as exc:
        return [str(exc)]
    errors = []
    missing = sorted(set(source_nodes) - set(translated_nodes))
    extra = sorted(set(translated_nodes) - set(source_nodes))
    if missing:
        errors.append(f"translated TOC is missing node IDs: {missing}")
    if extra:
        errors.append(f"translated TOC has unknown node IDs: {extra}")
    moved = sorted(identity for identity in set(source_nodes) & set(translated_nodes)
                   if source_nodes[identity][0] != translated_nodes[identity][0])
    if moved:
        errors.append(f"translated TOC node paths changed: {moved}")
    return errors


def _contains_title(text: str, title: str) -> bool:
    return _title_occurrences(text, title) > 0


def normalize_toc_source_label(value: str) -> str:
    """Normalize whole-block Markdown wrappers without changing inner text."""
    value = str(value).strip()
    # A footnote reference is a semantic marker attached to a heading label,
    # not part of its visible title. Preserve definitions such as [^1]: text.
    value = re.sub(r"\[\^[^\]]+\](?!:)", "", value)
    value = re.sub(r"\s+", " ", value)
    for wrapper in ("<strong>", "<em>", "<b>", "<i>", "**", "__", "*", "_", "`"):
        if value.startswith(wrapper) and len(value) > 2 * len(wrapper):
            closing = {"<strong>": "</strong>", "<em>": "</em>", "<b>": "</b>", "<i>": "</i>"}.get(wrapper, wrapper)
            if value.endswith(closing):
                value = value[len(wrapper):-len(closing)].strip()
    while len(value) >= 2 and (value[0], value[-1]) in {
        ("《", "》"), ("〈", "〉"), ("「", "」"), ("『", "』"),
        ("“", "”"), ("‘", "’"), ('"', '"'), ("'", "'"),
    }:
        value = value[1:-1].strip()
    return value


def _title_occurrences(text: str, title: str) -> int:
    expected = normalize_toc_source_label(title)
    if not expected:
        return False
    count = 0
    fenced = False
    paragraph: list[str] = []

    def count_block(lines: list[str]) -> int:
        if not lines:
            return 0
        joined = normalize_toc_source_label(" ".join(lines))
        if joined == expected and len(lines) > 1:
            wrappers = ("<strong>", "<em>", "<b>", "<i>", "**", "__", "*", "_")
            raw = " ".join(lines).strip()
            if any(raw.startswith(wrapper) and raw.endswith(
                {"<strong>": "</strong>", "<em>": "</em>", "<b>": "</b>", "<i>": "</i>"}.get(wrapper, wrapper)
            ) for wrapper in wrappers):
                return 1
        return sum(normalize_toc_source_label(line) == expected for line in lines)

    for line in str(text or "").splitlines():
        line = line.strip()
        if line.startswith(("```", "~~~")):
            count += count_block(paragraph)
            paragraph.clear()
            fenced = not fenced
            continue
        if fenced:
            continue
        if not line:
            count += count_block(paragraph)
            paragraph.clear()
            continue
        heading = re.match(r"^#{1,6}\s+(.*)$", line)
        if heading:
            count += count_block(paragraph)
            paragraph.clear()
            count += count_block([heading.group(1)])
            continue
        paragraph.append(line)
    count += count_block(paragraph)
    return count


def project_toc_ownership(
    chapters: Any,
    progress: Mapping[str, Any],
    source_texts: Mapping[str, str],
) -> dict[str, dict[str, Any]]:
    """Project every logical node onto exactly one physical Markdown file.

    Progress units own themselves and all descendants up to another unit.
    Unowned structural ancestors are assigned to their first source-order
    descendant unit. Visible labels must occur in source text; only reviewed
    ``binding_mode: container_only`` nodes may lack one.
    """
    identity_errors = validate_toc_node_identities(chapters)
    if identity_errors:
        raise ValueError("invalid TOC ownership identities: " + "; ".join(identity_errors))
    nodes = {path: node for path, node in iter_toc_nodes(chapters)}
    by_id = {toc_node_id(node, path): path for path, node in nodes.items()}
    if not nodes:
        raise ValueError("TOC ownership projection requires logical nodes")

    raw_units = progress.get("units")
    if not isinstance(raw_units, list) or not raw_units:
        raise ValueError("tree_progress.json must contain non-empty units")
    if any(not isinstance(unit, Mapping) for unit in raw_units):
        raise ValueError("tree_progress.json units must all be objects")

    units_by_path: dict[tuple[int, ...], Mapping[str, Any]] = {}
    names_by_path: dict[tuple[int, ...], list[str]] = {}
    texts_by_path: dict[tuple[int, ...], dict[str, str]] = {}
    claimed_files: set[str] = set()
    for unit in raw_units:
        identity = unit.get("node_id")
        path = by_id.get(identity) if isinstance(identity, str) else None
        if path is None:
            raise ValueError(f"progress unit has unknown or missing node_id: {identity!r}")
        index_path = unit.get("index_path")
        if index_path is not None and (
            not isinstance(index_path, list)
            or any(type(part) is not int or part < 1 for part in index_path)
            or tuple(index_path) != path
        ):
            raise ValueError(f"progress index_path disagrees with node_id {identity}")
        if path in units_by_path:
            raise ValueError(f"multiple progress units claim TOC node {identity}")
        part_files = unit.get("part_files")
        if part_files is not None and (
            not isinstance(part_files, list)
            or any(not isinstance(name, str) or not name.strip() for name in part_files)
        ):
            raise ValueError(f"progress part_files must be a list of non-empty filenames for {identity}")
        raw_names = part_files if part_files is not None else [unit.get("file")]
        if any(not isinstance(name, str) or not name.strip() for name in raw_names):
            raise ValueError(f"progress unit {identity} has invalid filenames")
        names = list(raw_names)
        if not names:
            raise ValueError(f"progress unit {identity} has no owned Markdown files")
        if len(names) != len(set(names)) or any(name in claimed_files for name in names):
            raise ValueError(f"duplicate physical file ownership in progress unit {identity}")
        claimed_files.update(names)
        missing = [name for name in names if name not in source_texts]
        if missing:
            raise ValueError(f"missing source texts for TOC ownership: {missing}")
        units_by_path[path] = unit
        names_by_path[path] = names
        if any(not isinstance(source_texts[name], str) for name in names):
            raise ValueError(f"source texts must be strings for progress unit {identity}")
        texts_by_path[path] = {name: source_texts[name] for name in names}

    ordered_unit_paths = sorted(units_by_path)
    owner_by_node: dict[tuple[int, ...], tuple[int, ...]] = {}
    for path in nodes:
        if path in units_by_path:
            owner_by_node[path] = path
            continue
        ancestors = [candidate for candidate in ordered_unit_paths
                     if len(candidate) < len(path) and path[:len(candidate)] == candidate]
        if ancestors:
            owner_by_node[path] = max(ancestors, key=len)
            continue
        descendants = [candidate for candidate in ordered_unit_paths
                       if len(candidate) > len(path) and candidate[:len(path)] == path]
        if descendants:
            owner_by_node[path] = min(descendants)
            continue
        raise ValueError(f"TOC node {toc_node_id(nodes[path], path)} has no progress-unit owner")

    projected: dict[str, dict[str, Any]] = {}
    rows_by_file: dict[str, list[dict[str, Any]]] = {
        name: [] for names in names_by_path.values() for name in names
    }
    used: dict[tuple[tuple[int, ...], str, str], int] = {}

    for path, node in iter_toc_nodes(chapters):
        unit_path = owner_by_node[path]
        names = names_by_path[unit_path]
        texts = texts_by_path[unit_path]
        node_id = toc_node_id(node, path)
        source_title = str(node.get("title") or "")
        normalized_title = normalize_toc_source_label(source_title)
        match_counts = {name: _title_occurrences(texts[name], source_title) for name in names}
        match = None
        occurrence = None
        for name in names:
            key = (unit_path, node_id, name)
            already_used = used.get((unit_path, "title:" + normalized_title, name), 0)
            if match_counts[name] > already_used:
                match = name
                occurrence = already_used + 1
                used[(unit_path, "title:" + normalized_title, name)] = occurrence
                break
        explicit_container = node.get("binding_mode") == "container_only"
        if match is None and not explicit_container:
            raise ValueError(f"visible TOC node {node_id} ({source_title!r}) is absent from its owning source unit")
        owner_file = match or names[0]
        row = {
            "node_id": node_id,
            "path": path,
            "source_title": source_title,
            "owner_file": owner_file,
            "binding_mode": "visible" if match else "container_only",
            "structural_ancestor": path != unit_path and not (len(path) > len(unit_path) and path[:len(unit_path)] == unit_path),
            "source_occurrence": occurrence or 0,
            "source_match_count": match_counts[owner_file],
        }
        rows_by_file[owner_file].append(row)

    for unit_path, names in names_by_path.items():
        unit_id = toc_node_id(nodes[unit_path], unit_path)
        for name in names:
            unit_row = next((row for row in rows_by_file[name]
                             if row["node_id"] == unit_id and row["path"] == unit_path), None)
            projected[name] = {
                "unit_node_id": unit_id,
                "unit_path": unit_path,
                "binding_mode": unit_row["binding_mode"] if unit_row else "container_only",
                "headings": rows_by_file[name],
            }
    return projected
