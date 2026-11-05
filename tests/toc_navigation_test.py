import xml.etree.ElementTree as ET
import json

import pytest

from pdf2epub.build_epub import generate_hierarchical_toc_html, generate_hierarchical_toc_ncx
from pdf2epub.build_epub import BuildEpubConfig, _prepare_epub_structure, build_epub
from pdf2epub.toc_navigation import (
    navigation_depth,
    present_navigation_headings,
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
    assert [p.find("n:navLabel/n:text", ns).text for p in points] == ["Repeated", "Repeated", "Leaf"]
    html_root = ET.fromstring(html)
    links = html_root.findall(".//{http://www.w3.org/1999/xhtml}a")
    assert [link.text for link in links] == ["Repeated", "Repeated", "Leaf"]
    assert [link.attrib["href"] for link in links] == [
        point.find("n:content", ns).attrib["src"].removeprefix("text/")
        for point in points
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


def test_navigation_projection_hides_auto_excluded_roles_but_preserves_included_frontmatter():
    structure = [
        {"node_id": "copyright", "title": "Copyright", "type": "copyright", "navigation_binding_mode": "container_only", "file_path": "copyright.md", "unit_id": "copyright", "children": []},
        {"node_id": "preface", "title": "Preface", "type": "frontmatter", "navigation_binding_mode": "container_only", "file_path": "preface.md", "unit_id": "preface", "children": []},
        {"node_id": "dedication", "title": "Dedication", "type": "frontmatter", "navigation_visibility": "exclude", "navigation_binding_mode": "container_only", "file_path": "dedication.md", "unit_id": "dedication", "children": []},
    ]
    model = resolve_navigation(structure, generated_files={"copyright.html", "preface.html", "dedication.html"})
    assert [node.node_id for node in model] == ["preface"]


def test_hidden_parent_keeps_child_owner_and_invalid_visibility_fails_closed():
    structure = [{
        "node_id": "copyright", "title": "Copyright", "type": "copyright",
        "navigation_binding_mode": "container_only", "file_path": "copyright.md",
        "unit_id": "copyright", "children": [{
            "node_id": "logical", "title": "Group", "navigation_binding_mode": "container_only",
            "children": [],
        }],
    }]
    model = resolve_navigation(structure, generated_files={"copyright.html"})
    assert [node.node_id for node in model] == ["logical"]
    assert model[0].target == "copyright.html"
    structure[0]["navigation_visibility"] = "maybe"
    with pytest.raises(ValueError, match="Invalid TOC navigation_visibility"):
        resolve_navigation(structure, generated_files={"copyright.html"})
    assert resolve_navigation([{
        "node_id": "empty-cover", "title": "Cover", "type": "cover",
        "navigation_binding_mode": "container_only", "children": [],
    }], generated_files=set()) == ()


def test_container_alias_anchor_remains_non_heading_and_needs_only_valid_fragment():
    structure = [{"node_id": "container", "title": "Part Group", "level": 1,
                  "anchor": "container-alias", "navigation_binding_mode": "container_only"}]
    model = resolve_navigation(structure, {"container-alias": "chapter.html"})
    source = {"chapter.html": '<html><body><div id="container-alias">Group content</div></body></html>'}
    rendered = present_navigation_headings(model, source)
    assert rendered == source
    validate_navigation_rendering(model, source)


def test_deep_heading_is_capped_visually_and_keeps_logical_level_class():
    structure = [{"node_id": "deep", "title": "Deep", "level": 8,
                  "anchor": "deep-anchor", "navigation_binding_mode": "visible"}]
    model = resolve_navigation(structure, {"deep-anchor": "chapter.html"})
    rendered = present_navigation_headings(model, {
        "chapter.html": '<html><body><p id="deep-anchor">Deep</p></body></html>'
    })
    assert '<h6 id="deep-anchor" class="logical-level-8">Deep</h6>' in rendered["chapter.html"]
    validate_navigation_rendering(model, rendered)


def test_navigation_visibility_and_number_label_roundtrip_and_validation():
    from pdf2epub.toc_model import compose_numbered_title, validate_toc_node_identities

    node = {"node_id": "chapter", "title": "Civil War", "level": 1,
            "navigation_visibility": "include", "numbering_label": "II",
            "numbering_evidence": {"page": 1, "line": 1, "excerpt": "II Civil War"},
            "binding_mode": "container_only", "children": []}
    assert validate_toc_node_identities([node]) == []
    node["navigation_visibility"] = "sometimes"
    assert any("navigation_visibility" in error for error in validate_toc_node_identities([node]))
    assert compose_numbered_title("The Shape of Truth", "1") == "1 The Shape of Truth"
    assert compose_numbered_title("A Study in Blue", "1") == "1 A Study in Blue"
    assert compose_numbered_title("1. Shape", "1") == "1. Shape"
    assert compose_numbered_title("(1) Shape", "1.") == "(1) Shape"
    assert compose_numbered_title("I Introduction", "I") == "I Introduction"
    assert compose_numbered_title("A Study", "1") == "1 A Study"
    with pytest.raises(ValueError, match="conflicts"):
        compose_numbered_title("1.2 Scope", "1")
    with pytest.raises(ValueError, match="conflicts"):
        compose_numbered_title("2 Shape", "1")
    assert any("invalid TOC numbering_label" in error for error in validate_toc_node_identities([
        {"node_id": "bad", "title": "x", "numbering_label": "<img onerror=alert(1)>", "children": []}
    ]))
    for malformed in (".", "()", "( ).", ")", "(1", "1)"):
        assert any("invalid TOC numbering_label" in error for error in validate_toc_node_identities([
            {"node_id": "bad", "title": "x", "numbering_label": malformed,
             "numbering_evidence": {"page": 1, "line": 1, "excerpt": malformed}, "children": []}
        ]))
    from pdf2epub.refine.toc_tree import TOCNode
    model = TOCNode.from_dict({"title": "Title", "level": 1, "start_page": 1, "end_page": 1,
                               "node_id": "id", "navigation_visibility": "exclude",
                               "numbering_label": "II", "numbering_evidence": {"page": 1, "line": 1, "excerpt": "II Title"}})
    serialized = model.to_dict()
    assert serialized["navigation_visibility"] == "exclude"
    assert serialized["numbering_label"] == "II"
    assert serialized["numbering_evidence"]["excerpt"] == "II Title"


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
    with pytest.raises(ValueError, match="not rendered at its canonical heading level"):
        validate_navigation_rendering(model, {
            "chapter.html": '<html><body><p><em>1 Civil War</em><span id="heading-anchor"></span></p></body></html>'
        })
    with pytest.raises(ValueError, match="does not land on the expected heading"):
        validate_navigation_rendering(model, {
            "chapter.html": '<html><body><h2><span id="heading-anchor"></span>2 Civil War</h2></body></html>'
        })


def test_presentation_promotes_exact_title_paragraph_and_preserves_body_paragraph():
    structure = [{"node_id": "heading", "title": "Title", "level": 2,
                  "anchor": "a", "navigation_binding_mode": "visible"}]
    model = resolve_navigation(structure, {"a": "chapter.html"})
    rendered = present_navigation_headings(model, {
        "chapter.html": '<html><body><p><span id="a"></span><em>Title</em></p><p id="body">Title with following body.</p></body></html>'
    })["chapter.html"]
    assert '<h2><span id="a"></span><em>Title</em></h2>' in rendered
    assert '<p id="body">Title with following body.</p>' in rendered


def test_reviewed_numbering_is_added_once_only_when_bare_heading_is_unique():
    structure = [{
        "node_id": "heading", "title": "Civil War", "level": 2,
        "numbering_label": "II", "anchor": "a", "navigation_binding_mode": "visible",
    }]
    model = resolve_navigation(structure, {"a": "chapter.html"})
    rendered = present_navigation_headings(model, {
        "chapter.html": '<html><body><p id="a">Civil War</p></body></html>'
    })
    assert '<h2 id="a">II Civil War</h2>' in rendered["chapter.html"]
    structure[0]["title"] = "II Civil War"
    model = resolve_navigation(structure, {"a": "chapter.html"})
    rendered = present_navigation_headings(model, {
        "chapter.html": '<html><body><p id="a">II Civil War</p></body></html>'
    })
    assert '<h2 id="a">II Civil War</h2>' in rendered["chapter.html"]
    with pytest.raises(ValueError, match="cannot be applied uniquely"):
        present_navigation_headings(model, {
            "chapter.html": '<html><body><p id="a">Civil War</p><h2>III Civil War</h2></body></html>'
        })

    roman = [{
        "node_id": "roman", "title": "I Heading", "level": 2,
        "numbering_label": "I", "anchor": "roman", "navigation_binding_mode": "visible",
    }]
    roman_model = resolve_navigation(roman, {"roman": "chapter.html"})
    with pytest.raises(ValueError, match="cannot be applied uniquely"):
        present_navigation_headings(roman_model, {
            "chapter.html": '<html><body><p id="roman">Heading</p><h2>I Heading</h2></body></html>'
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


def test_container_with_same_title_does_not_steal_child_navigation_dom_anchor():
    from pdf2epub.build_epub import add_subchapter_anchors_to_parts
    from pdf2epub.epub.converter import ContentConverter
    from pdf2epub.markdown_to_html import convert_markdown_to_html
    from pdf2epub.toc_model import project_toc_ownership

    projection = project_toc_ownership(
        [{"node_id": "container", "title": "Same", "binding_mode": "container_only",
          "children": [{"node_id": "visible", "title": "Same"}]}],
        {"units": [{"node_id": "container", "index_path": [1],
                    "part_files": ["part1.md", "part2.md"]}]},
        {"part1.md": "Container prose", "part2.md": "# Same\n\nChild prose"},
    )
    child_row = next(row for part in projection.values() for row in part["headings"]
                     if row["node_id"] == "visible")
    structure = [{"node_id": "container", "title": "Same", "anchor": "container-anchor",
                  "navigation_binding_mode": "container_only", "unit_id": "chapter",
                  "file_path": "part1.md", "part_files": ["part1.md", "part2.md"],
                  "children": [{"node_id": "visible", "title": "Same", "anchor": "visible-anchor",
                                "navigation_binding_mode": child_row["binding_mode"],
                                "unit_id": "chapter", "file_path": child_row["owner_file"],
                                "part_files": ["part1.md", "part2.md"]}]}]
    converter = ContentConverter.__new__(ContentConverter)
    rendered_parts, locations = add_subchapter_anchors_to_parts(
        converter,
        [("chapter_part1.html", convert_markdown_to_html("Container prose", standalone=False)),
         ("chapter_part2.html", convert_markdown_to_html("# Same\n\nChild prose", standalone=False))],
        1,
        [{"title": "Same", "anchor": "visible-anchor", "source_occurrence": child_row["source_occurrence"],
          "source_match_count": child_row["source_match_count"],
          "source_candidate_kind": child_row["source_candidate_kind"]}],
    )
    assert locations == {"visible-anchor": "chapter_part2.html"}
    navigation = resolve_navigation(
        structure,
        locations,
        generated_files={name for name, _ in rendered_parts},
    )
    assert navigation[0].target == "chapter_part1.html"
    assert navigation[0].children[0].target == "chapter_part2.html#visible-anchor"
    generated = {name: f"<html><body>{html}</body></html>" for name, html in rendered_parts}
    validate_navigation_rendering(navigation, generated)


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
