"""Resolve EPUB navigation targets once for HTML and NCX serialization."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Optional, Sequence
import html

from .toc_model import toc_node_id


@dataclass(frozen=True)
class ResolvedNavigationNode:
    node_id: str
    title: str
    target: str
    binding_status: str
    target_node_id: Optional[str]
    children: tuple["ResolvedNavigationNode", ...] = ()
    expected_title: Optional[str] = None
    expected_level: Optional[int] = None
    binding_mode: str = "visible"
    numberless_expected_title: Optional[str] = None
    numbering_label: Optional[str] = None
    navigation_visible: bool = True


def _file_target(entry: Mapping) -> Optional[str]:
    if entry.get("virtual_file_name"):
        return str(entry["virtual_file_name"])
    if not entry.get("file_path"):
        return None
    unit_id = entry.get("unit_id")
    if not unit_id:
        return None
    parts = entry.get("part_files") or [entry["file_path"]]
    suffix = "_part1" if len(parts) > 1 else ""
    return f"{unit_id}{suffix}.html"


def resolve_navigation(
    structure: Sequence[Mapping],
    anchor_locations: Optional[Mapping[str, str]] = None,
    *,
    generated_files: Optional[set[str]] = None,
    project: bool = True,
) -> tuple[ResolvedNavigationNode, ...]:
    """Bind logical TOC entries to rendered files/fragments.

    ``anchor_locations`` is the authoritative map produced while rendering.
    Container entries without a rendered anchor land on their first content
    descendant. Entries backed by a file fall back to that unit's first part.
    """
    anchor_locations = anchor_locations or {}
    hidden_roles = {"cover", "copyright", "metadata", "catalog", "ad", "toc"}

    for path, entry in _walk_entries(structure):
        visibility = entry.get("navigation_visibility", "auto")
        if not isinstance(visibility, str) or visibility not in {"auto", "include", "exclude"}:
            raise ValueError(f"Invalid TOC navigation_visibility at {path}: {visibility}")
    identifiers: set[str] = set()
    anchors: dict[str, str] = {}

    def resolve_list(
        entries: Sequence[Mapping], path: tuple[int, ...], owner_target: Optional[str] = None
    ) -> tuple[ResolvedNavigationNode, ...]:
        resolved = []
        for index, entry in enumerate(entries, 1):
            item_path = path + (index,)
            raw_node_id = entry.get("node_id")
            if not isinstance(raw_node_id, str) or not raw_node_id.strip():
                raise ValueError(f"TOC node is missing required node_id at {item_path}")
            node_id = toc_node_id(entry, item_path)
            if node_id in identifiers:
                raise ValueError(f"Duplicate TOC node identity: {node_id}")
            identifiers.add(node_id)
            anchor = str(entry.get("anchor") or "")
            raw_binding = entry.get("navigation_binding_mode")
            if raw_binding is None:
                raise ValueError(f"TOC node is missing ownership binding mode: {node_id}")
            binding = str(raw_binding)
            if binding not in {"visible", "container_only"}:
                raise ValueError(f"Unknown TOC binding mode for {node_id}: {binding}")
            if anchor:
                if anchor in anchors:
                    raise ValueError(f"TOC anchor collision: {anchor}")
                anchors[anchor] = node_id

            own_file = _file_target(entry)
            visibility = entry.get("navigation_visibility", "auto")
            role = str(entry.get("type") or "").lower()
            hidden_from_navigation = visibility == "exclude" or (
                visibility == "auto" and role in hidden_roles
            )
            child_owner = own_file or owner_target
            children = resolve_list(entry.get("children") or (), item_path, child_owner)
            target = None
            status = "container_fallback"
            located = anchor_locations.get(anchor) if anchor else None
            if binding == "visible" and not located:
                raise ValueError(
                    f"Required visible TOC heading is missing from rendered XHTML: {node_id} ({anchor})"
                )
            if located:
                target = f"{located}#{anchor}"
                status = "rendered_anchor"
                target_node_id = node_id
            else:
                target = _file_target(entry)
                if target:
                    status = "file_fallback"
                    target_node_id = node_id
                elif (first_descendant := _first_target_node(children)) is not None:
                    target = first_descendant.target
                    status = "first_content_descendant"
                    target_node_id = first_descendant.target_node_id
                elif owner_target:
                    target = owner_target
                    status = "owning_file_fallback"
                    target_node_id = None
                elif hidden_from_navigation:
                    target = ""
                    status = "hidden"
                    target_node_id = None
                else:
                    raise ValueError(f"TOC node has no rendered destination: {node_id}")

            if generated_files is not None and target:
                filename = target.split("#", 1)[0]
                if filename not in generated_files:
                    raise ValueError(f"TOC target file does not exist: {filename}")
                if "#" in target:
                    fragment = target.split("#", 1)[1]
                    if anchor_locations.get(fragment) != filename:
                        raise ValueError(f"TOC fragment is not in rendered anchor map: {target}")
            resolved.append(ResolvedNavigationNode(
                node_id=node_id,
                title=_navigation_title(entry),
                target=target,
                binding_status=status,
                target_node_id=target_node_id,
                expected_title=_navigation_title(entry),
                expected_level=entry.get("level") if type(entry.get("level")) is int else None,
                binding_mode=binding,
                numberless_expected_title=_numberless_title(entry),
                numbering_label=entry.get("numbering_label") if isinstance(entry.get("numbering_label"), str) else None,
                navigation_visible=not hidden_from_navigation,
                children=children,
            ))
        return tuple(resolved)

    resolved = resolve_list(structure, ())

    if not project:
        return resolved

    return project_navigation(resolved)


def project_navigation(nodes: Sequence[ResolvedNavigationNode]) -> tuple[ResolvedNavigationNode, ...]:
    def project_items(items):
        output = []
        for node in items:
            children = project_items(node.children)
            if not node.navigation_visible:
                output.extend(children)
            else:
                output.append(ResolvedNavigationNode(
                    node.node_id, node.title, node.target, node.binding_status,
                    node.target_node_id, children, node.expected_title,
                    node.expected_level,
                    node.binding_mode,
                    node.numberless_expected_title,
                    node.numbering_label,
                    node.navigation_visible,
                ))
        return tuple(output)

    return project_items(nodes)


def _first_target_node(nodes: Sequence[ResolvedNavigationNode]) -> Optional[ResolvedNavigationNode]:
    for node in nodes:
        if node.target:
            return node
        descendant = _first_target_node(node.children)
        if descendant is not None:
            return descendant
    return None


def _walk_entries(entries):
    for index, entry in enumerate(entries, 1):
        path = (index,)
        yield path, entry
        for child_path, child in _walk_entries(entry.get("children") or ()):
            yield path + child_path, child


def _navigation_title(entry: Mapping) -> str:
    title = str(entry.get("navigation_display_title") or entry.get("title") or "")
    from .toc_model import compose_numbered_title

    return compose_numbered_title(title, entry.get("numbering_label"))


def _numberless_title(entry: Mapping) -> str:
    title = str(entry.get("title") or "").strip()
    label = entry.get("numbering_label")
    if not isinstance(label, str) or not label.strip():
        return title
    from .toc_model import strip_numbering_label

    return strip_numbering_label(title, label) or title


def navigation_depth(nodes: Sequence[ResolvedNavigationNode]) -> int:
    return max((1 + navigation_depth(node.children) for node in nodes), default=0)


def present_navigation_headings(
    nodes: Sequence[ResolvedNavigationNode], generated_html: Mapping[str, str]
) -> dict[str, str]:
    """Promote exact anchored title-only blocks to their canonical h-level."""
    import re
    from .epub.converter import visible_heading_text
    from .toc_model import (
        normalize_toc_source_label, toc_title_match_key,
        _numbered_prefix, strip_numbering_label,
    )

    requests: dict[tuple[str, str], tuple[str, int, int, str, Optional[str]]] = {}

    def collect(items):
        for node in items:
            filename, marker, fragment = node.target.partition("#")
            if marker and node.binding_status == "rendered_anchor" and node.binding_mode == "visible":
                logical_level = node.expected_level or 1
                requests[(filename, fragment)] = (
                    node.expected_title or node.title,
                    min(logical_level, 6),
                    logical_level,
                    node.numberless_expected_title or node.expected_title or node.title,
                    node.numbering_label,
                )
            collect(node.children)

    collect(nodes)
    block = re.compile(r"<(h[1-6]|p)(\b[^>]*)>(.*?)</\1\s*>", re.I | re.S)
    updated = dict(generated_html)
    for (filename, fragment), (expected, level, logical_level, numberless_title, numbering_label) in requests.items():
        if filename not in updated:
            continue
        expected_key = toc_title_match_key(normalize_toc_source_label(expected, markdown_source=False))
        numberless_key = toc_title_match_key(normalize_toc_source_label(numberless_title, markdown_source=False))

        def numberless_is_unique() -> bool:
            matches = 0
            for document in updated.values():
                for candidate in block.finditer(document):
                    visible = normalize_toc_source_label(visible_heading_text(candidate.group(3)), markdown_source=False)
                    marker = _numbered_prefix(visible)
                    exact_numberless = strip_numbering_label(visible, numbering_label)
                    candidate_numberless_key = toc_title_match_key(
                        normalize_toc_source_label(
                            exact_numberless if exact_numberless is not None else visible[marker.end():] if marker else visible,
                            markdown_source=False,
                        )
                    )
                    if candidate_numberless_key == numberless_key:
                        matches += 1
            return matches == 1

        def replace(match):
            tag, attrs, inner = match.groups()
            if not re.search(rf"(?<![\w:-])id=[\"']{re.escape(fragment)}[\"']", attrs + inner, re.I):
                return match.group(0)
            visible = toc_title_match_key(normalize_toc_source_label(visible_heading_text(inner), markdown_source=False))
            if visible != expected_key:
                if not numbering_label or visible != numberless_key:
                    return match.group(0)
                if not numberless_is_unique():
                    raise ValueError(f"Reviewed numbering cannot be applied uniquely to {filename}#{fragment}")
                inner = f"{html.escape(numbering_label)} {inner}"
            canonical_tag = f"h{level}"
            if logical_level > 6:
                logical_class = f"logical-level-{logical_level}"
                class_match = re.search(r"\bclass=[\"']([^\"']*)[\"']", attrs, re.I)
                if class_match:
                    classes = class_match.group(1).split()
                    if logical_class not in classes:
                        classes.append(logical_class)
                        attrs = attrs[:class_match.start(1)] + " ".join(classes) + attrs[class_match.end(1):]
                else:
                    attrs += f' class="{logical_class}"'
            return f"<{canonical_tag}{attrs}>{inner}</{canonical_tag}>"

        updated[filename] = block.sub(replace, updated[filename])
    return updated


def validate_navigation_rendering(
    nodes: Sequence[ResolvedNavigationNode], generated_html: Mapping[str, str]
) -> None:
    """Check every resolved file/fragment against rendered XHTML before packaging."""
    import re
    import xml.etree.ElementTree as ET
    ids_by_file: dict[str, set[str]] = {}
    heading_blocks: dict[str, list[str]] = {}
    for filename, document in generated_html.items():
        try:
            root = ET.fromstring(document)
        except ET.ParseError as exc:
            raise ValueError(f"Rendered XHTML is not well-formed: {filename}: {exc}") from exc
        found = [element.attrib["id"] for element in root.iter() if "id" in element.attrib]
        if len(found) != len(set(found)):
            raise ValueError(f"Duplicate XHTML id in navigation target file: {filename}")
        ids_by_file[filename] = set(found)
        heading_blocks[filename] = [
            match.group(0)
            for match in re.finditer(r"<(h[1-6]|p)([^>]*)>(.*?)</\1>", document, re.I | re.S)
        ]

    nodes_by_id: dict[str, ResolvedNavigationNode] = {}

    def index_nodes(items):
        for item in items:
            nodes_by_id[item.node_id] = item
            index_nodes(item.children)
    index_nodes(nodes)

    def visit(items):
        for node in items:
            filename, marker, fragment = node.target.partition("#")
            if not node.navigation_visible and not node.target:
                visit(node.children)
                continue
            if filename not in generated_html:
                raise ValueError(f"TOC target file does not exist: {filename}")
            if marker and fragment not in ids_by_file.get(filename, set()):
                raise ValueError(f"TOC fragment is absent from rendered XHTML: {node.target}")
            if marker:
                expected_node = nodes_by_id.get(node.target_node_id or "")
                if expected_node and expected_node.binding_mode == "container_only":
                    visit(node.children)
                    continue
                from .epub.converter import ContentConverter
                from .epub.converter import visible_heading_text

                heading_pattern = re.compile(r"<(h[1-6]|p)([^>]*)>(.*?)</\1>", re.I | re.S)

                def visible(value: str) -> str:
                    return visible_heading_text(value)

                expected_title = (expected_node.expected_title or expected_node.title) if expected_node else (node.expected_title or node.title)
                matches = ContentConverter._subchapter_anchor_candidates(
                    "".join(heading_blocks[filename]), visible(expected_title), heading_pattern, visible
                )
                anchor_pattern = re.compile(rf'(?<![\w:-])id=["\']{re.escape(fragment)}["\']', re.I)
                landed = any(
                    rank[0] < 3 and anchor_pattern.search(match.group(2) + match.group(3))
                    for rank, match in matches
                )
                anchored_matches = [
                    match for rank, match in matches
                    if anchor_pattern.search(match.group(2) + match.group(3))
                ]
                if expected_node and expected_node.binding_mode == "visible":
                    canonical_level = expected_node.expected_level
                    required_level = min(canonical_level, 6) if canonical_level else None
                    valid_heading = any(
                        match.group(1).lower().startswith("h")
                        and (required_level is None or match.group(1).lower() == f"h{required_level}")
                        for match in anchored_matches
                    )
                    if not valid_heading:
                        raise ValueError(f"Visible TOC heading is not rendered at its canonical heading level: {node.target}")
                    if canonical_level and canonical_level > 6 and not any(
                        re.search(rf"\blogical-level-{canonical_level}\b", match.group(2), re.I)
                        for match in anchored_matches
                    ):
                        raise ValueError(f"Deep TOC heading lost its logical level class: {node.target}")
                anchored_numberless = any(
                    rank[0] == 2 and anchor_pattern.search(match.group(2) + match.group(3))
                    for rank, match in matches
                )
                if anchored_numberless:
                    all_candidates = [
                        candidate
                        for blocks in heading_blocks.values()
                        for candidate in ContentConverter._subchapter_anchor_candidates(
                            "".join(blocks), visible(expected_title), heading_pattern, visible
                        )
                    ]
                    eligible_count = sum(1 for rank, _match in all_candidates if rank[0] < 3)
                    if eligible_count != 1 or any(rank[0] == 3 for rank, _match in all_candidates):
                        raise ValueError(
                            f"Numberless TOC fallback is not globally unique or conflicts with a numbered heading: {node.target}"
                        )
                if not landed:
                    raise ValueError(
                        f"TOC fragment does not land on the expected heading: {node.target} ({expected_title!r})"
                    )
            visit(node.children)
    visit(nodes)


def serialize_html_navigation(nodes: Sequence[ResolvedNavigationNode]) -> str:
    def render(items, level=0):
        out = ""
        pad = "            " + "    " * level
        for node in items:
            out += f'{pad}<li><a href="{html.escape(node.target, quote=True)}">{html.escape(node.title)}</a>'
            if node.children:
                out += "\n" + pad + "    <ul>\n" + render(node.children, level + 1) + pad + "    </ul>"
            out += "</li>\n"
        return out
    return render(nodes)


def serialize_ncx_navigation(nodes: Sequence[ResolvedNavigationNode]) -> str:
    counter = 1
    play_orders = {}
    next_order = 1

    def render(items, level=2):
        nonlocal counter, next_order
        out = ""
        pad = "    " * level
        for node in items:
            target = "text/" + node.target
            if target not in play_orders:
                play_orders[target] = next_order
                next_order += 1
            out += (f'{pad}<navPoint id="navpoint-{counter}" playOrder="{play_orders[target]}">\n'
                    f'{pad}    <navLabel><text>{html.escape(node.title)}</text></navLabel>\n'
                    f'{pad}    <content src="{html.escape(target, quote=True)}"/>\n')
            counter += 1
            if node.children:
                out += render(node.children, level + 1)
            out += f"{pad}</navPoint>\n"
        return out
    return render(nodes)
