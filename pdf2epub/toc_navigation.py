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
) -> tuple[ResolvedNavigationNode, ...]:
    """Bind logical TOC entries to rendered files/fragments.

    ``anchor_locations`` is the authoritative map produced while rendering.
    Container entries without a rendered anchor land on their first content
    descendant. Entries backed by a file fall back to that unit's first part.
    """
    anchor_locations = anchor_locations or {}
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
                elif children:
                    target = children[0].target
                    status = "first_content_descendant"
                    target_node_id = children[0].target_node_id
                elif owner_target:
                    target = owner_target
                    status = "owning_file_fallback"
                    target_node_id = None
                else:
                    raise ValueError(f"TOC node has no rendered destination: {node_id}")

            if generated_files is not None:
                filename = target.split("#", 1)[0]
                if filename not in generated_files:
                    raise ValueError(f"TOC target file does not exist: {filename}")
                if "#" in target:
                    fragment = target.split("#", 1)[1]
                    if anchor_locations.get(fragment) != filename:
                        raise ValueError(f"TOC fragment is not in rendered anchor map: {target}")
            resolved.append(ResolvedNavigationNode(
                node_id=node_id,
                title=str(entry.get("title") or ""),
                target=target,
                binding_status=status,
                target_node_id=target_node_id,
                children=children,
            ))
        return tuple(resolved)

    return resolve_list(structure, ())


def navigation_depth(nodes: Sequence[ResolvedNavigationNode]) -> int:
    return max((1 + navigation_depth(node.children) for node in nodes), default=0)


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
            if filename not in generated_html:
                raise ValueError(f"TOC target file does not exist: {filename}")
            if marker and fragment not in ids_by_file.get(filename, set()):
                raise ValueError(f"TOC fragment is absent from rendered XHTML: {node.target}")
            if marker:
                from .epub.converter import ContentConverter
                from .epub.converter import visible_heading_text

                heading_pattern = re.compile(r"<(h[1-6]|p)([^>]*)>(.*?)</\1>", re.I | re.S)

                def visible(value: str) -> str:
                    return visible_heading_text(value)

                matches = ContentConverter._subchapter_anchor_candidates(
                    "".join(heading_blocks[filename]), visible(node.title), heading_pattern, visible
                )
                anchor_pattern = re.compile(rf'(?<![\w:-])id=["\']{re.escape(fragment)}["\']', re.I)
                landed = any(
                    rank[0] < 3 and anchor_pattern.search(match.group(2) + match.group(3))
                    for rank, match in matches
                )
                if not landed:
                    expected_node = nodes_by_id.get(node.target_node_id or "")
                    expected_title = expected_node.title if expected_node else node.title
                    if expected_node is not None and expected_node.node_id != node.node_id:
                        matches = ContentConverter._subchapter_anchor_candidates(
                            "".join(heading_blocks[filename]), visible(expected_title), heading_pattern, visible
                        )
                        landed = any(
                            rank[0] < 3 and anchor_pattern.search(match.group(2) + match.group(3))
                            for rank, match in matches
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
    counter = 2
    play_orders = {"text/toc.html": 1}
    next_order = 2

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
