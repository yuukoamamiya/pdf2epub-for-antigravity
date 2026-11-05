from pdf2epub.toc_model import (
    iter_toc_nodes,
    toc_node_anchor,
    toc_node_id,
    validate_toc_node_identities,
    project_toc_ownership,
    normalize_toc_source_label,
    toc_nodes_by_id,
    validate_toc_tree_correspondence,
)
import json
import pytest

from pdf2epub.toc_translation_workflow import build_toc_heading_contexts


def test_identity_is_title_independent_and_paths_are_not_identity():
    tree = [{"node_id": "root", "title": "Repeated", "children": [{"node_id": "child", "title": "Repeated"}]}]
    nodes = list(iter_toc_nodes(tree))
    assert [path for path, _ in nodes] == [(1,), (1, 1)]
    assert toc_node_id(nodes[0][1], nodes[0][0]) == "root"
    assert toc_node_id({"node_id": "root", "title": "Changed"}, (1,)) == "root"
    assert toc_node_anchor(nodes[1][1], nodes[1][0]).startswith("toc-child")


def test_explicit_node_identity_and_anchor_survive_tree_reindexing():
    node = {"node_id": "scan:p12:box4", "title": "Original"}
    assert toc_node_id(node, (8, 3)) == "scan:p12:box4"
    assert toc_node_anchor(node, (8, 3)).startswith("toc-scan-p12-box4-")
    assert toc_node_anchor({**node, "anchor": "stable-fragment"}, (8, 3)) == "stable-fragment"
    assert toc_node_anchor({"node_id": "节点一"}, (1,)) != toc_node_anchor({"node_id": "节点二"}, (1,))
    assert toc_node_anchor({"node_id": "a/b"}, (1,)) != toc_node_anchor({"node_id": "a:b"}, (1,))


def test_duplicate_explicit_id_or_anchor_is_reported():
    tree = [
        {"node_id": "same", "anchor": "dup", "children": []},
        {"node_id": "same", "anchor": "dup", "children": []},
    ]
    errors = validate_toc_node_identities(tree)
    assert "duplicate TOC node identity: same" in errors
    assert "duplicate TOC anchor: dup" in errors
    assert validate_toc_node_identities([{"title": "missing"}]) == [
        "TOC node at 1 is missing required node_id"
    ]
    for unsafe_anchor in ('bad"anchor', "bad<anchor", "bad&anchor"):
        assert any("invalid TOC anchor" in error for error in validate_toc_node_identities([
            {"node_id": "valid", "title": "A", "anchor": unsafe_anchor}
        ]))


def test_tree_correspondence_uses_node_ids_and_requires_same_path():
    source = {"schema_version": 2, "chapters": [{"node_id": "one", "title": "Source"}]}
    translated = {"schema_version": 2, "chapters": [{"node_id": "one", "title": "Translated"}]}
    assert list(toc_nodes_by_id(source)) == ["one"]
    assert validate_toc_tree_correspondence(source, translated) == []
    moved = {"schema_version": 2, "chapters": [{"node_id": "other", "title": "Translated"}]}
    assert validate_toc_tree_correspondence(source, moved)


def test_heading_projection_includes_owned_grandchildren_but_stops_at_nested_unit(tmp_path):
    output = tmp_path
    source = [
        {"node_id": "a", "title": "Root", "children": [
            {"node_id": "b", "title": "Middle", "children": [
                {"node_id": "c", "title": "Grandchild"},
                {"node_id": "d", "title": "Separate", "children": [
                    {"node_id": "e", "title": "Owned by Separate"}
                ]},
            ]}
        ]}
    ]
    translated = json.loads(json.dumps(source))
    for _, node in iter_toc_nodes(translated):
        node["title"] += " translated"
    (output / "toc_tree.json").write_text(json.dumps({"schema_version": 2, "chapters": source}), encoding="utf-8")
    (output / "toc_tree_translated.json").write_text(json.dumps({"schema_version": 2, "chapters": translated}), encoding="utf-8")
    progress = {"units": [
        {"index_path": [1], "node_id": "a", "file": "root.md"},
        {"index_path": [1, 1, 2], "node_id": "d", "file": "separate.md"},
    ]}
    (output / "ocr_markdown").mkdir()
    (output / "ocr_markdown" / "tree_progress.json").write_text(json.dumps(progress), encoding="utf-8")
    source_dir = output / "ocr_markdown"
    (source_dir / "root.md").write_text("Root\nMiddle\nGrandchild", encoding="utf-8")
    (source_dir / "separate.md").write_text("Separate\nOwned by Separate", encoding="utf-8")

    contexts = build_toc_heading_contexts(output, source_dir)
    root_context = contexts["root.md"]
    assert [item["title"] for item in root_context["children"]] == ["Middle translated", "Grandchild translated"]
    assert all(item["binding_mode"] == "visible" for item in root_context["children"])
    separate_context = contexts["separate.md"]
    assert [item["title"] for item in separate_context["children"]] == ["Owned by Separate translated"]


def test_shared_ownership_projection_assigns_split_headings_to_first_matching_part():
    chapters = [{"node_id": "root", "title": "Root", "children": [
        {"node_id": "child", "title": "Child", "children": [
            {"node_id": "grand", "title": "Grand"}
        ]}
    ]}]
    projection = project_toc_ownership(
        chapters,
        {"units": [{"node_id": "root", "part_files": ["part1.md", "part2.md"]}]},
        {"part1.md": "Root\nChild", "part2.md": "Grand"},
    )
    part1 = projection["part1.md"]["headings"]
    part2 = projection["part2.md"]["headings"]
    assert [item["node_id"] for item in part1] == ["root", "child"]
    assert [item["node_id"] for item in part2] == ["grand"]
    assert projection["part1.md"]["binding_mode"] == "visible"
    assert projection["part2.md"]["binding_mode"] == "container_only"


def test_repeated_visible_titles_use_distinct_source_occurrences():
    chapters = [{"node_id": "root", "title": "Root", "children": [
        {"node_id": "same-a", "title": "Same"},
        {"node_id": "same-b", "title": "Same"},
    ]}]
    projection = project_toc_ownership(
        chapters,
        {"units": [{"node_id": "root", "index_path": [1], "part_files": ["one.md", "two.md"]}]},
        {"one.md": "Root\nSame", "two.md": "Same"},
    )
    owners = {
        row["node_id"]: name
        for name, value in projection.items()
        for row in value["headings"]
    }
    assert owners["root"] == "one.md"
    assert owners["same-a"] == "one.md"
    assert owners["same-b"] == "two.md"


def test_unowned_ancestor_projects_once_to_first_descendant_unit():
    chapters = [{"node_id": "root", "title": "Root", "children": [
        {"node_id": "a", "title": "A"},
        {"node_id": "b", "title": "B"},
    ]}]
    projection = project_toc_ownership(
        chapters,
        {"units": [
            {"node_id": "a", "index_path": [1, 1], "file": "a.md"},
            {"node_id": "b", "index_path": [1, 2], "file": "b.md"},
        ]},
        {"a.md": "Root\nA", "b.md": "B"},
    )
    assert [row["node_id"] for row in projection["a.md"]["headings"]] == ["root", "a"]
    assert projection["a.md"]["headings"][0]["structural_ancestor"] is True
    assert [row["node_id"] for row in projection["b.md"]["headings"]] == ["b"]


def test_uncovered_visible_node_fails_and_container_ancestor_is_explicit():
    chapters = [{"node_id": "root", "title": "Root", "children": [
        {"node_id": "leaf", "title": "Leaf"},
    ]}]
    progress = {"units": [{"node_id": "leaf", "index_path": [1, 1], "file": "leaf.md"}]}
    try:
        project_toc_ownership(chapters, progress, {"leaf.md": "Leaf"})
    except ValueError as exc:
        assert "visible TOC node root" in str(exc)
    else:
        raise AssertionError("a visible uncovered ancestor must not silently disappear")

    chapters[0]["binding_mode"] = "container_only"
    projection = project_toc_ownership(chapters, progress, {"leaf.md": "Leaf"})
    assert projection["leaf.md"]["headings"][0]["node_id"] == "root"
    assert projection["leaf.md"]["headings"][0]["binding_mode"] == "container_only"


def test_same_title_parent_and_child_reserve_separate_source_occurrences():
    chapters = [{"node_id": "parent", "title": "Same", "children": [
        {"node_id": "child", "title": "Same"},
    ]}]
    projection = project_toc_ownership(
        chapters,
        {"units": [{"node_id": "parent", "index_path": [1], "file": "unit.md"}]},
        {"unit.md": "Same\nSame"},
    )
    rows = projection["unit.md"]["headings"]
    assert [(row["node_id"], row["source_occurrence"], row["source_match_count"]) for row in rows] == [
        ("parent", 1, 2),
        ("child", 2, 2),
    ]


def test_explicit_container_does_not_consume_same_title_candidate_across_parts():
    chapters = [{"node_id": "container", "title": "Same", "binding_mode": "container_only",
                 "children": [{"node_id": "visible", "title": "Same"}]}]
    projection = project_toc_ownership(
        chapters,
        {"units": [{"node_id": "container", "index_path": [1],
                    "part_files": ["part1.md", "part2.md"]}]},
        {"part1.md": "Container prose", "part2.md": "# Same\n\nChild body"},
    )
    by_id = {row["node_id"]: (name, row) for name, value in projection.items()
             for row in value["headings"]}
    assert by_id["container"][0] == "part1.md"
    assert by_id["container"][1]["binding_mode"] == "container_only"
    assert by_id["container"][1]["source_occurrence"] == 0
    assert by_id["visible"][0] == "part2.md"
    assert by_id["visible"][1]["binding_mode"] == "visible"
    assert by_id["visible"][1]["source_occurrence"] == 1


def test_explicit_container_does_not_consume_only_same_title_candidate_in_single_part():
    projection = project_toc_ownership(
        [{"node_id": "container", "title": "Same", "binding_mode": "container_only",
          "children": [{"node_id": "visible", "title": "Same"}]}],
        {"units": [{"node_id": "container", "index_path": [1], "file": "unit.md"}]},
        {"unit.md": "# Same\n\nBody"},
    )
    rows = {row["node_id"]: row for row in projection["unit.md"]["headings"]}
    assert rows["container"]["binding_mode"] == "container_only"
    assert rows["container"]["source_occurrence"] == 0
    assert rows["visible"]["binding_mode"] == "visible"
    assert rows["visible"]["source_occurrence"] == 1
    assert rows["visible"]["source_match_count"] == 1


def test_case_only_parent_child_titles_share_occurrence_key_but_sharp_s_is_distinct():
    chapters = [{"node_id": "parent", "title": "DIALEKTIK DER WERTFORM", "children": [
        {"node_id": "child", "title": "Dialektik der Wertform"},
    ]}]
    projection = project_toc_ownership(
        chapters,
        {"units": [{"node_id": "parent", "index_path": [1], "file": "unit.md"}]},
        {"unit.md": "# DIALEKTIK DER WERTFORM\n\n# Dialektik der Wertform"},
    )
    assert [(row["node_id"], row["source_occurrence"], row["source_match_count"])
            for row in projection["unit.md"]["headings"]] == [
        ("parent", 1, 2), ("child", 2, 2),
    ]

    with pytest.raises(ValueError, match="visible TOC node sharp"):
        project_toc_ownership(
            [{"node_id": "sharp", "title": "Straße"}],
            {"units": [{"node_id": "sharp", "index_path": [1], "file": "unit.md"}]},
            {"unit.md": "# STRASSE"},
        )


def test_sharp_s_and_ss_titles_bind_independently_in_source_and_html():
    from pdf2epub.epub.converter import visible_heading_text

    projection = project_toc_ownership(
        [{"node_id": "root", "title": "Container", "children": [
            {"node_id": "sharp", "title": "Straße"},
            {"node_id": "ss", "title": "STRASSE"},
        ]}],
        {"units": [{"node_id": "root", "index_path": [1], "file": "unit.md"}]},
        {"unit.md": "# Container\n\n# Straße\n\n# STRASSE"},
    )
    rows = projection["unit.md"]["headings"][1:]
    assert [(row["node_id"], row["source_occurrence"], row["source_match_count"])
            for row in rows] == [("sharp", 1, 1), ("ss", 1, 1)]
    assert visible_heading_text("Straße") != visible_heading_text("STRASSE")


def test_source_projection_preserves_identifier_underscores_and_ignores_fenced_code():
    chapters = [{"node_id": "root", "title": "A_B"}]
    result = project_toc_ownership(
        chapters,
        {"units": [{"node_id": "root", "index_path": [1], "file": "unit.md"}]},
        {"unit.md": "# A_B"},
    )
    assert result["unit.md"]["binding_mode"] == "visible"
    try:
        project_toc_ownership(
            chapters,
            {"units": [{"node_id": "root", "index_path": [1], "file": "unit.md"}]},
            {"unit.md": "```text\nA_B\n```"},
        )
    except ValueError as exc:
        assert "absent" in str(exc)
    else:
        raise AssertionError("a code example must not satisfy a TOC heading obligation")


def test_multiline_emphasized_soft_wrapped_title_is_one_source_label():
    projection = project_toc_ownership(
        [{"node_id": "root", "title": "A long title continued here"}],
        {"units": [{"node_id": "root", "index_path": [1], "file": "unit.md"}]},
        {"unit.md": "*A long title\ncontinued here*"},
    )
    assert projection["unit.md"]["binding_mode"] == "visible"


def test_source_heading_footnote_reference_is_not_part_of_visible_title():
    assert normalize_toc_source_label("Heading[^1]") == "Heading"
    assert normalize_toc_source_label("[^1]: Footnote definition") == "[^1]: Footnote definition"
    assert normalize_toc_source_label(r"Heading \*") == "Heading *"
    assert normalize_toc_source_label("Heading<sup>*</sup>") == "Heading*"
    result = project_toc_ownership(
        [{"node_id": "heading", "title": "Heading"}],
        {"units": [{"node_id": "heading", "index_path": [1], "file": "unit.md"}]},
        {"unit.md": "# Heading[^1]\n\n[^1]: Footnote text"},
    )
    row = result["unit.md"]["headings"][0]
    assert row["binding_mode"] == "visible"
    assert row["source_title"] == "Heading"


def test_occurrence_projection_prefers_headings_across_split_parts():
    projection = project_toc_ownership(
        [
            {"node_id": "root", "title": "Chapter", "children": [
                {"node_id": "child", "title": "Repeated"},
            ]},
        ],
        {"units": [{"node_id": "root", "index_path": [1], "part_files": ["a.md", "b.md"]}]},
        {"a.md": "# Chapter\n\nRepeated\n", "b.md": "# Repeated\n"},
    )
    rows = projection["b.md"]["headings"]
    child = next(row for row in rows if row["node_id"] == "child")
    assert child["source_candidate_kind"] == "heading"
    assert child["source_match_count"] == 1


def test_source_occurrences_keep_superscripts_and_trailing_symbols_literal():
    assert normalize_toc_source_label("Dimension x<sup>2</sup>") == "Dimension x2"
    assert normalize_toc_source_label("C++*") == "C++*"
    assert normalize_toc_source_label("Title†") == "Title†"
    assert normalize_toc_source_label("x < y > z", markdown_source=False) == "x < y > z"
    projection = project_toc_ownership(
        [{"node_id": "root", "title": "Dimension x<sup>2</sup>"}],
        {"units": [{"node_id": "root", "index_path": [1], "file": "unit.md"}]},
        {"unit.md": "# Dimension x<sup>2</sup>"},
    )
    assert projection["unit.md"]["binding_mode"] == "visible"


def test_literal_markdown_punctuation_and_raw_superscript_roundtrip():
    from pdf2epub.epub.converter import ContentConverter
    from pdf2epub.markdown_to_html import convert_markdown_to_html

    cases = [
        ("# C++\\*", "C++*", "heading"),
        (r"# \*A\*", "*A*", "heading"),
        ("# Dimension x<sup>2</sup>", "Dimension x2", "heading"),
        ("# Title†", "Title†", "heading"),
    ]
    converter = ContentConverter.__new__(ContentConverter)
    for index, (markdown, title, kind) in enumerate(cases):
        projection = project_toc_ownership(
            [{"node_id": "root", "title": title}],
            {"units": [{"node_id": "root", "index_path": [1], "file": "unit.md"}]},
            {"unit.md": markdown},
        )
        row = projection["unit.md"]["headings"][0]
        assert row["binding_mode"] == "visible"
        rendered = convert_markdown_to_html(markdown, standalone=False)
        updated, found = converter._add_subchapter_anchor(
            rendered, title, f"toc-{index}", source_occurrence=1,
            source_match_count=1, source_candidate_kind=kind,
        )
        assert found, (markdown, rendered)
        assert f'id="toc-{index}"' in updated

        stripped_title = title.rstrip("*") if title.endswith("*") else title + "*"
        if stripped_title != title:
            _unchanged, false_match = converter._add_subchapter_anchor(
                rendered, stripped_title, "toc-stripped", source_occurrence=1,
                source_match_count=1, source_candidate_kind=kind,
            )
            assert not false_match
