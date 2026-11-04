import xml.etree.ElementTree as ET
import json

import pytest

from pdf2epub.build_epub import generate_hierarchical_toc_html, generate_hierarchical_toc_ncx
from pdf2epub.build_epub import BuildEpubConfig, _prepare_epub_structure, build_epub
from pdf2epub.toc_navigation import (
    navigation_depth,
    resolve_navigation,
    validate_navigation_rendering,
)


def test_html_and_ncx_serialize_same_arbitrary_depth_navigation(tmp_path):
    structure = [{
        "node_id": "one", "title": "Repeated", "anchor": "a1", "navigation_binding_mode": "visible",
        "unit_id": "unit", "file_path": "unit.md",
        "part_files": ["unit.part1.md", "unit.part2.md"],
        "children": [{
            "node_id": "two", "title": "Repeated", "anchor": "a2", "navigation_binding_mode": "visible",
            "children": [{"node_id": "three", "title": "Leaf", "anchor": "a3", "navigation_binding_mode": "visible"}],
        }],
    }]
    model = resolve_navigation(structure, {"a1": "unit_part1.html", "a2": "unit_part2.html", "a3": "unit_part2.html"}, generated_files={"unit_part1.html", "unit_part2.html"})
    html_path, ncx_path = tmp_path / "toc.html", tmp_path / "toc.ncx"
    assert navigation_depth(model) == 3
    assert generate_hierarchical_toc_html(structure, "Book", html_path, resolved_navigation=model)
    assert generate_hierarchical_toc_ncx(structure, "Book", ncx_path, resolved_navigation=model)
    html = html_path.read_text()
    ncx = ncx_path.read_text()
    assert html.count('>Repeated</a>') == 2
    for target in ("unit_part1.html#a1", "unit_part2.html#a2", "unit_part2.html#a3"):
        assert target in html
        assert f"text/{target}" in ncx
    root = ET.fromstring(ncx)
    ns = {"n": "http://www.daisy.org/z3986/2005/ncx/"}
    assert root.find("n:head/n:meta[@name='dtb:depth']", ns).attrib["content"] == "3"
    points = root.findall(".//n:navPoint", ns)
    assert [p.find("n:navLabel/n:text", ns).text for p in points[1:]] == ["Repeated", "Repeated", "Leaf"]
    html_root = ET.fromstring(html)
    links = html_root.findall(".//{http://www.w3.org/1999/xhtml}a")
    assert [link.text for link in links] == ["Repeated", "Repeated", "Leaf"]
    assert [link.attrib["href"] for link in links] == [
        point.find("n:content", ns).attrib["src"].removeprefix("text/")
        for point in points[1:]
    ]
    assert len(html_root.findall(".//{http://www.w3.org/1999/xhtml}ul")) == 3


def test_container_uses_first_content_descendant_and_explicit_anchor(tmp_path):
    structure = [{"node_id": "container", "title": "Container", "navigation_binding_mode": "container_only", "children": [
        {"node_id": "visible", "title": "Visible", "anchor": "explicit-id", "navigation_binding_mode": "visible",
         "unit_id": "chapter", "file_path": "chapter.md", "part_files": ["chapter.md"]}
    ]}]
    model = resolve_navigation(structure, {"explicit-id": "chapter.html"}, generated_files={"chapter.html"})
    assert model[0].target == "chapter.html#explicit-id"
    assert model[0].binding_status == "first_content_descendant"
    assert model[0].children[0].binding_status == "rendered_anchor"


def test_navigation_rejects_duplicate_identity_anchor_and_missing_file():
    with pytest.raises(ValueError, match="Duplicate TOC node identity"):
        resolve_navigation([{"node_id": "same", "title": "A", "anchor": "a", "navigation_binding_mode": "visible", "unit_id": "a", "file_path": "a.md"},
                            {"node_id": "same", "title": "B", "anchor": "b", "navigation_binding_mode": "visible", "unit_id": "b", "file_path": "b.md"}], {"a": "a.html"})
    with pytest.raises(ValueError, match="anchor collision"):
        resolve_navigation([{"node_id": "a", "title": "A", "anchor": "x", "navigation_binding_mode": "visible", "unit_id": "a", "file_path": "a.md"},
                            {"node_id": "b", "title": "B", "anchor": "x", "navigation_binding_mode": "visible", "unit_id": "b", "file_path": "b.md"}], {"x": "a.html"})
    with pytest.raises(ValueError, match="target file does not exist"):
        resolve_navigation([{"node_id": "a", "title": "A", "navigation_binding_mode": "container_only", "unit_id": "a", "file_path": "a.md"}], generated_files=set())


def test_navigation_rejects_missing_or_colliding_rendered_ids():
    model = resolve_navigation([{"node_id": "a", "title": "A", "anchor": "a", "navigation_binding_mode": "visible",
                                 "unit_id": "a", "file_path": "a.md"}], {"a": "a.html"})
    with pytest.raises(ValueError, match="fragment is absent"):
        validate_navigation_rendering(model, {"a.html": '<html><body><h1 id="other">A</h1></body></html>'})
    with pytest.raises(ValueError, match="Duplicate XHTML id"):
        validate_navigation_rendering(model, {"a.html": '<html><body><h1 id="a">A</h1><p id="a">x</p></body></html>'})


def test_heading_validation_accepts_canonical_emphasis_paragraph_and_rejects_number_conflict():
    visible = [{"node_id": "heading", "title": "1 Civil War", "anchor": "heading-anchor",
                "navigation_binding_mode": "visible", "unit_id": "chapter",
                "file_path": "chapter.md", "part_files": ["chapter.md"]}]
    model = resolve_navigation(visible, {"heading-anchor": "chapter.html"})
    validate_navigation_rendering(model, {
        "chapter.html": '<html><body><p><em>1 Civil War</em><span id="heading-anchor"></span></p></body></html>'
    })
    with pytest.raises(ValueError, match="does not land on the expected heading"):
        validate_navigation_rendering(model, {
            "chapter.html": '<html><body><h2><span id="heading-anchor"></span>2 Civil War</h2></body></html>'
        })


def test_occurrence_anchor_rejects_numberless_match_when_conflicting_number_exists():
    from pdf2epub.epub.converter import ContentConverter

    converter = ContentConverter.__new__(ContentConverter)
    html = (
        "<h2>1 Civil War</h2>"
        "<h2>Civil War</h2>"
        "<h2>2 Civil War</h2>"
    )
    updated, found = converter._add_subchapter_anchor(
        html,
        "1 Civil War",
        "toc-anchor",
        source_occurrence=2,
        source_match_count=2,
    )
    assert not found
    assert updated == html

    updated, found = converter._add_subchapter_anchor(
        html,
        "1 Civil War",
        "toc-anchor",
        source_occurrence=1,
        source_match_count=2,
    )
    assert not found
    assert updated == html


def test_numberless_fallback_is_allowed_only_when_unique_and_unconflicted():
    from pdf2epub.epub.converter import ContentConverter

    converter = ContentConverter.__new__(ContentConverter)
    html = "<h2>Civil War</h2>"
    updated, found = converter._add_subchapter_anchor(
        html, "1 Civil War", "toc-anchor", source_occurrence=1,
        source_match_count=1, source_candidate_kind="heading",
    )
    assert found
    assert '<span id="toc-anchor"></span>Civil War' in updated

    conflict = "<h2>Civil War</h2><p>2 Civil War</p>"
    updated, found = converter._add_subchapter_anchor(
        conflict, "1 Civil War", "toc-anchor", source_occurrence=1,
        source_match_count=1, source_candidate_kind="heading",
    )
    assert not found
    assert updated == conflict


@pytest.mark.parametrize(
    "other_heading",
    ["<h2>Civil War</h2>", "<h2>2 Civil War</h2>", "<h2>1 Civil War</h2>"],
)
def test_final_navigation_validation_rejects_book_level_numberless_ambiguity(other_heading):
    from pdf2epub.toc_navigation import resolve_navigation, validate_navigation_rendering

    structure = [{
        "node_id": "chapter", "title": "1 Civil War", "anchor": "toc-civil-war",
        "navigation_binding_mode": "visible", "unit_id": "unit", "file_path": "unit.md",
    }]
    navigation = resolve_navigation(
        structure, {"toc-civil-war": "unit.html"}, generated_files={"unit.html", "other.html"}
    )
    generated = {
        "unit.html": '<html xmlns="http://www.w3.org/1999/xhtml"><body><h2><span id="toc-civil-war"></span>Civil War</h2></body></html>',
        "other.html": f'<html xmlns="http://www.w3.org/1999/xhtml"><body>{other_heading}</body></html>',
    }
    with pytest.raises(ValueError, match="Numberless TOC fallback"):
        validate_navigation_rendering(navigation, generated)


def test_final_navigation_validation_accepts_globally_unique_numberless_fallback():
    from pdf2epub.toc_navigation import resolve_navigation, validate_navigation_rendering

    structure = [{
        "node_id": "chapter", "title": "1 Civil War", "anchor": "toc-civil-war",
        "navigation_binding_mode": "visible", "unit_id": "unit", "file_path": "unit.md",
    }]
    navigation = resolve_navigation(
        structure, {"toc-civil-war": "unit.html"}, generated_files={"unit.html", "other.html"}
    )
    validate_navigation_rendering(navigation, {
        "unit.html": '<html xmlns="http://www.w3.org/1999/xhtml"><body><h2><span id="toc-civil-war"></span>Civil War</h2></body></html>',
        "other.html": '<html xmlns="http://www.w3.org/1999/xhtml"><body><p>Other title</p></body></html>',
    })


def test_container_fallback_global_check_uses_descendant_title():
    from pdf2epub.toc_navigation import resolve_navigation, validate_navigation_rendering

    structure = [{
        "node_id": "container", "title": "Front Matter", "anchor": "toc-container",
        "navigation_binding_mode": "container_only", "children": [{
            "node_id": "child", "title": "1 Civil War", "anchor": "toc-child",
            "navigation_binding_mode": "visible", "unit_id": "unit", "file_path": "unit.md",
        }],
    }]
    navigation = resolve_navigation(
        structure, {"toc-child": "unit.html"}, generated_files={"unit.html", "other.html"}
    )
    generated = {
        "unit.html": '<html xmlns="http://www.w3.org/1999/xhtml"><body><h2><span id="toc-child"></span>Civil War</h2></body></html>',
        "other.html": '<html xmlns="http://www.w3.org/1999/xhtml"><body><h2>1 Civil War</h2></body></html>',
    }
    with pytest.raises(ValueError, match="Numberless TOC fallback"):
        validate_navigation_rendering(navigation, generated)


def test_container_fallback_matches_descendant_title_before_global_uniqueness_check():
    from pdf2epub.toc_navigation import resolve_navigation, validate_navigation_rendering

    structure = [{
        "node_id": "container", "title": "1 Same", "anchor": "toc-container",
        "navigation_binding_mode": "container_only", "children": [{
            "node_id": "child", "title": "Same", "anchor": "toc-child",
            "navigation_binding_mode": "visible", "unit_id": "unit", "file_path": "unit.md",
        }],
    }]
    navigation = resolve_navigation(
        structure, {"toc-child": "unit.html"}, generated_files={"unit.html", "other.html"}
    )
    generated = {
        "unit.html": '<html xmlns="http://www.w3.org/1999/xhtml"><body><h2><span id="toc-child"></span>Same</h2></body></html>',
        "other.html": '<html xmlns="http://www.w3.org/1999/xhtml"><body><h2>Same</h2></body></html>',
    }
    validate_navigation_rendering(navigation, generated)


def test_occurrence_anchor_rejects_source_html_count_mismatch_and_bool_counts():
    from pdf2epub.epub.converter import ContentConverter

    converter = ContentConverter.__new__(ContentConverter)
    html = "<h2>Heading</h2><p>Heading</p>"
    for occurrence, count in ((1, 2), (True, 2), (1, True)):
        updated, found = converter._add_subchapter_anchor(
            html, "Heading", "toc-anchor", source_occurrence=occurrence,
            source_match_count=count, source_candidate_kind="heading",
        )
        assert not found
        assert updated == html


def test_occurrence_anchor_kind_prefers_heading_and_matches_curly_quote_rendering():
    from pdf2epub.epub.converter import ContentConverter, visible_heading_text

    converter = ContentConverter.__new__(ContentConverter)
    html = "<p>HEGEL’S Phenomenology</p><h1>HEGEL'S Phenomenology</h1>"
    assert visible_heading_text("HEGEL’S Phenomenology") == visible_heading_text("HEGEL'S Phenomenology")
    updated, found = converter._add_subchapter_anchor(
        html, "HEGEL’S Phenomenology", "toc-anchor", source_occurrence=1,
        source_match_count=1, source_candidate_kind="heading",
    )
    assert found
    assert '<h1><span id="toc-anchor"></span>HEGEL\'S Phenomenology</h1>' in updated


def test_build_part_anchor_uses_source_heading_kind_over_same_text_paragraph():
    from pdf2epub.build_epub import add_subchapter_anchors_to_parts
    from pdf2epub.epub.converter import ContentConverter
    from pdf2epub.markdown_to_html import convert_markdown_to_html
    from pdf2epub.toc_model import project_toc_ownership

    markdown = "# Copyright\n\nCopyright\n"
    ownership = project_toc_ownership(
        [{"node_id": "root", "title": "Chapter", "children": [
            {"node_id": "copyright", "title": "Copyright"},
        ]}],
        {"units": [{"node_id": "root", "index_path": [1], "file": "unit.md"}]},
        {"unit.md": "# Chapter\n\n" + markdown},
    )
    info = next(row for row in ownership["unit.md"]["headings"] if row["node_id"] == "copyright")
    assert info["source_candidate_kind"] == "heading"
    assert info["source_match_count"] == 1
    rendered = convert_markdown_to_html("# Chapter\n\n" + markdown, standalone=False)
    parts, locations = add_subchapter_anchors_to_parts(
        ContentConverter.__new__(ContentConverter),
        [("chapter.html", rendered)], 1,
        [{"title": "Copyright", "anchor": "toc-copyright",
          "source_occurrence": info["source_occurrence"],
          "source_match_count": info["source_match_count"],
          "source_candidate_kind": info["source_candidate_kind"]}],
    )
    assert locations == {"toc-copyright": "chapter.html"}
    assert '<span id="toc-copyright"></span>Copyright</h1>' in parts[0][1]
    assert "<p>Copyright</p>" in parts[0][1]


def test_build_part_occurrences_are_selected_across_all_rendered_parts():
    from pdf2epub.build_epub import add_subchapter_anchors_to_parts
    from pdf2epub.epub.converter import ContentConverter

    converter = ContentConverter.__new__(ContentConverter)
    repeated_parts = [("one.html", "<h2>Civil War</h2>"), ("two.html", "<h2>Civil War</h2>")]
    unchanged, locations = add_subchapter_anchors_to_parts(
        converter, repeated_parts, 1,
        [{"title": "1 Civil War", "anchor": "toc-anchor", "source_occurrence": 1,
          "source_match_count": 1, "source_candidate_kind": "heading"}],
    )
    assert not locations
    assert unchanged == repeated_parts

    exact_and_fallback = [("one.html", "<h2>1 Civil War</h2>"), ("two.html", "<h2>Civil War</h2>")]
    updated, locations = add_subchapter_anchors_to_parts(
        converter, exact_and_fallback, 1,
        [{"title": "1 Civil War", "anchor": "toc-anchor", "source_occurrence": 1,
          "source_match_count": 1, "source_candidate_kind": "heading"}],
    )
    assert locations == {"toc-anchor": "one.html"}
    assert '<span id="toc-anchor"></span>1 Civil War' in updated[0][1]
    assert updated[1][1] == "<h2>Civil War</h2>"


def test_occurrence_anchor_rejects_invalid_candidate_kind():
    from pdf2epub.epub.converter import ContentConverter

    html = "<h2>Heading</h2>"
    updated, found = ContentConverter.__new__(ContentConverter)._add_subchapter_anchor(
        html, "Heading", "toc-anchor", source_occurrence=1,
        source_match_count=1, source_candidate_kind="maybe-heading",
    )
    assert not found
    assert updated == html


def test_number_conflict_in_other_candidate_kind_still_blocks_numberless_fallback():
    from pdf2epub.epub.converter import ContentConverter

    converter = ContentConverter.__new__(ContentConverter)
    html = "<h2>Civil War</h2><p>2 Civil War</p>"
    updated, found = converter._add_subchapter_anchor(
        html, "1 Civil War", "toc-anchor", source_occurrence=1,
        source_match_count=1, source_candidate_kind="heading",
    )
    assert not found
    assert updated == html


def test_exact_occurrences_outrank_extra_numberless_heading_fallbacks():
    from pdf2epub.epub.converter import ContentConverter

    converter = ContentConverter.__new__(ContentConverter)
    html = "<h2>1 Civil War</h2><h2>1 Civil War</h2><h2>Civil War</h2>"
    updated, found = converter._add_subchapter_anchor(
        html, "1 Civil War", "toc-second", source_occurrence=2,
        source_match_count=2, source_candidate_kind="heading",
    )
    assert found
    assert '<h2><span id="toc-second"></span>1 Civil War</h2><h2>Civil War</h2>' in updated


def test_required_visible_binding_fails_closed_but_container_can_land_on_descendant():
    visible = [{"node_id": "visible", "title": "Visible", "anchor": "visible-anchor",
                "navigation_binding_mode": "visible", "unit_id": "chapter",
                "file_path": "chapter.md", "part_files": ["chapter.md"]}]
    with pytest.raises(ValueError, match="Required visible TOC heading"):
        resolve_navigation(visible, {})
    container = [{"node_id": "container", "title": "Container", "anchor": "container-anchor",
                  "navigation_binding_mode": "container_only", "children": [
                      {"node_id": "child", "title": "Child", "anchor": "child-anchor", "navigation_binding_mode": "visible",
                       "unit_id": "chapter", "file_path": "chapter.md", "part_files": ["chapter.md"]}
                  ]}]
    resolved = resolve_navigation(container, {"child-anchor": "chapter.html"})
    assert resolved[0].target == "chapter.html#child-anchor"
    assert resolved[0].binding_status == "first_content_descendant"


def test_builder_consumes_physical_ownership_for_split_heading(tmp_path):
    output = tmp_path / "output"
    source_dir = output / "ocr_markdown"
    markdown_dir = output / "translated"
    source_dir.mkdir(parents=True)
    markdown_dir.mkdir()
    source_parts = {
        "chapter_1.1.part1.md": "# Parent\n\n## Child\n",
        "chapter_1.1.part2.md": "### Grandchild\n\nBody\n",
    }
    for name, text in source_parts.items():
        (source_dir / name).write_text(text, encoding="utf-8")
        (markdown_dir / name).write_text(text, encoding="utf-8")
    (source_dir / "tree_progress.json").write_text(
        '{"units":[{"node_id":"child","index_path":[1,1],"part_files":'
        '["chapter_1.1.part1.md","chapter_1.1.part2.md"]}]}', encoding="utf-8"
    )
    tree = {"schema_version": 2, "chapters": [{"node_id": "parent", "title": "Parent", "level": 1, "binding_mode": "visible",
        "children": [{"node_id": "child", "title": "Child", "level": 2, "binding_mode": "visible",
            "children": [{"node_id": "grandchild", "title": "Grandchild", "level": 3, "binding_mode": "visible"}]}]}]}
    structure = _prepare_epub_structure(BuildEpubConfig(
        book_title="Book", output_dir=output, markdown_dir=markdown_dir,
        toc_tree_path=output / "toc.json", config={"footnotes": {"mode": "disabled"}},
    ), tree, tree)
    parent, child = structure[0], structure[0]["children"][0]
    assert "file_path" not in parent
    assert parent["navigation_binding_mode"] == "visible"
    assert child["navigation_binding_mode"] == "visible"
    assert child["children"][0]["navigation_binding_mode"] == "visible"
    assert any(row["node_id"] == "parent" for row in child["navigation_anchor_entries_by_part"]["chapter_1.1.part1.md"])
    anchors = {parent["anchor"]: "chapter_1.1_part1.html",
               child["anchor"]: "chapter_1.1_part1.html",
               child["children"][0]["anchor"]: "chapter_1.1_part2.html"}
    generated_files = {"chapter_1.1_part1.html", "chapter_1.1_part2.html"}
    model = resolve_navigation(structure, anchors, generated_files=generated_files)
    assert model[0].target.endswith("chapter_1.1_part1.html#" + parent["anchor"])
    assert model[0].children[0].children[0].target.endswith(
        "chapter_1.1_part2.html#" + child["children"][0]["anchor"]
    )
    generated = {
        "chapter_1.1_part1.html": f'<html><body><h1 id="{parent["anchor"]}">Parent</h1><h2 id="{child["anchor"]}">Child</h2></body></html>',
        "chapter_1.1_part2.html": f'<html><body><h3 id="{child["children"][0]["anchor"]}">Grandchild</h3></body></html>',
    }
    validate_navigation_rendering(model, generated)
    anchors.pop(child["children"][0]["anchor"])
    with pytest.raises(ValueError, match="Required visible TOC heading"):
        resolve_navigation(structure, anchors, generated_files=generated_files)


def test_real_epub_renderer_places_ancestor_and_split_heading_anchors(tmp_path):
    from pdf2epub.toc_model import TOC_TREE_SCHEMA_VERSION

    output = tmp_path / "output"
    source_dir = output / "ocr_markdown"
    markdown_dir = output / "polished_markdown"
    source_dir.mkdir(parents=True)
    markdown_dir.mkdir()
    source_parts = {
        "chapter_1.1.part1.md": "# DIALEKTIK DER WERTFORM\n\n## Dialektik der Wertform\n\nChild body.\n",
        "chapter_1.1.part2.md": "### Grandchild\n\nGrandchild body.\n",
    }
    for name, text in source_parts.items():
        (source_dir / name).write_text(text, encoding="utf-8")
        (markdown_dir / name).write_text(text, encoding="utf-8")
    (source_dir / "tree_progress.json").write_text(
        json.dumps({"units": [{"node_id": "child", "index_path": [1, 1],
                               "part_files": list(source_parts)}]}), encoding="utf-8"
    )
    tree = {"schema_version": TOC_TREE_SCHEMA_VERSION, "book_title": "Book", "chapters": [
        {"node_id": "parent", "title": "DIALEKTIK DER WERTFORM", "level": 1, "binding_mode": "visible", "children": [
            {"node_id": "child", "title": "Dialektik der Wertform", "level": 2, "binding_mode": "visible", "children": [
                {"node_id": "grandchild", "title": "Grandchild", "level": 3, "binding_mode": "visible"}
            ]}
        ]}
    ]}
    toc_path = output / "toc_tree.json"
    toc_path.write_text(json.dumps(tree), encoding="utf-8")
    config = BuildEpubConfig(
        book_title="Book", output_dir=output, markdown_dir=markdown_dir,
        toc_tree_path=toc_path, cover_image=output / "no-cover.jpg",
        config={"footnotes": {"mode": "disabled"}},
    )
    epub_path = build_epub(config)
    assert epub_path.is_file()
    rendered_dir = output / "epub_build" / "text"
    parent_file = (rendered_dir / "chapter_1.1_part1.html").read_text(encoding="utf-8")
    grandchild_file = (rendered_dir / "chapter_1.1_part2.html").read_text(encoding="utf-8")
    parent_anchor = "toc-parent"
    child_anchor = "toc-child"
    grandchild_anchor = "toc-grandchild"
    assert f'id="{parent_anchor}"' in parent_file
    assert f'id="{child_anchor}"' in parent_file
    assert f'id="{grandchild_anchor}"' in grandchild_file
    nav_html = (rendered_dir / "toc.html").read_text(encoding="utf-8")
    nav_ncx = (output / "epub_build" / "toc.ncx").read_text(encoding="utf-8")
    for target in (
        f"chapter_1.1_part1.html#{parent_anchor}",
        f"chapter_1.1_part1.html#{child_anchor}",
        f"chapter_1.1_part2.html#{grandchild_anchor}",
    ):
        assert target in nav_html
        assert f"text/{target}" in nav_ncx

    (markdown_dir / "chapter_1.1.part2.md").write_text(
        "No expected heading here.\n", encoding="utf-8"
    )
    with pytest.raises(ValueError, match="Required visible TOC heading is missing"):
        build_epub(config)


def test_real_epub_build_preserves_container_heading_footnote_reference(tmp_path):
    from pdf2epub.epub.footnotes import validate_footnote_graph
    from pdf2epub.toc_model import TOC_TREE_SCHEMA_VERSION

    output = tmp_path / "output"
    source_dir = output / "ocr_markdown"
    markdown_dir = output / "polished_markdown"
    source_dir.mkdir(parents=True)
    markdown_dir.mkdir()
    source = "# Parent[^1]\n\n## Child\n\nBody.\n\n[^1]: Parent note.\n"
    for directory in (source_dir, markdown_dir):
        (directory / "chapter_1.1.md").write_text(source, encoding="utf-8")
    (source_dir / "tree_progress.json").write_text(json.dumps({"units": [{
        "node_id": "child", "index_path": [1, 1], "file": "chapter_1.1.md"
    }]}), encoding="utf-8")
    tree = {"schema_version": TOC_TREE_SCHEMA_VERSION, "chapters": [{
        "node_id": "parent", "title": "Parent", "level": 1, "binding_mode": "container_only",
        "children": [{"node_id": "child", "title": "Child", "level": 2,
                      "binding_mode": "visible"}],
    }]}
    toc_path = output / "toc_tree.json"
    toc_path.write_text(json.dumps(tree), encoding="utf-8")
    epub_path = build_epub(BuildEpubConfig(
        book_title="Book", output_dir=output, markdown_dir=markdown_dir,
        toc_tree_path=toc_path, cover_image=output / "no-cover.jpg",
        config={"footnotes": {"mode": "auto"}},
    ))

    assert epub_path.is_file()
    rendered = (output / "epub_build" / "text" / "chapter_1.1.html").read_text(encoding="utf-8")
    assert 'Parent<sup id="fnref-' in rendered
    assert 'class="footnote-ref"' in rendered
    assert validate_footnote_graph({"chapter_1.1.html": rendered})["forward_hrefs"] == 1


def test_real_epub_renderer_places_repeated_title_anchors_by_source_occurrence(tmp_path):
    from pdf2epub.toc_model import TOC_TREE_SCHEMA_VERSION

    output = tmp_path / "output"
    source_dir = output / "ocr_markdown"
    markdown_dir = output / "polished_markdown"
    source_dir.mkdir(parents=True)
    markdown_dir.mkdir()
    source_text = "# Repeated\n\n## Repeated\n\nBody.\n"
    (source_dir / "chapter_1.1.md").write_text(source_text, encoding="utf-8")
    (markdown_dir / "chapter_1.1.md").write_text(source_text, encoding="utf-8")
    (source_dir / "tree_progress.json").write_text(json.dumps({"units": [{
        "node_id": "child", "index_path": [1, 1], "file": "chapter_1.1.md"
    }]}), encoding="utf-8")
    tree = {"schema_version": TOC_TREE_SCHEMA_VERSION, "chapters": [{
        "node_id": "parent", "title": "Repeated", "level": 1, "binding_mode": "visible",
        "children": [{"node_id": "child", "title": "Repeated", "level": 2,
                      "binding_mode": "visible"}],
    }]}
    toc_path = output / "toc_tree.json"
    toc_path.write_text(json.dumps(tree), encoding="utf-8")
    epub_path = build_epub(BuildEpubConfig(
        book_title="Book", output_dir=output, markdown_dir=markdown_dir,
        toc_tree_path=toc_path, cover_image=output / "no-cover.jpg",
        config={"footnotes": {"mode": "disabled"}},
    ))
    assert epub_path.is_file()
    rendered = (output / "epub_build" / "text" / "chapter_1.1.html").read_text(encoding="utf-8")
    assert rendered.count('id="toc-parent"') == 1
    assert rendered.count('id="toc-child"') == 1
    assert '<h1 id="repeated"><span id="toc-parent"></span>Repeated</h1>' in rendered
    assert '<h2 id="repeated_1"><span id="toc-child"></span>Repeated</h2>' in rendered
