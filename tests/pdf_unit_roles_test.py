import json

import pytest

from pdf2epub.pdf_unit_roles import load_pdf_file_role_details, load_pdf_file_roles


def _save(tmp_path, chapters, units):
    (tmp_path / "ocr_markdown").mkdir(exist_ok=True)
    (tmp_path / "toc_tree.json").write_text(json.dumps({"chapters": chapters}), encoding="utf-8")
    (tmp_path / "ocr_markdown" / "tree_progress.json").write_text(json.dumps({"units": units}), encoding="utf-8")


def test_inheritable_roles_cross_generic_nodes_and_apply_exactly_to_parts(tmp_path):
    _save(tmp_path, [{"node_id": "refs", "type": "bibliography", "children": [
        {"node_id": "chapter", "type": "chapter", "children": [
            {"node_id": "part", "children": []},
        ]},
    ]}], [{"node_id": "part", "index_path": [1, 1, 1], "part_files": ["refs.part1.md", "refs.part2.md"]}])
    details = load_pdf_file_role_details(tmp_path)
    assert set(details) == {"refs.part1.md", "refs.part2.md"}
    assert all(item["role"] == "bibliography" for item in details.values())
    assert all(item["role_origin"]["node_id"] == "refs" for item in details.values())


def test_explicit_child_special_role_overrides_inherited_role(tmp_path):
    _save(tmp_path, [{"node_id": "refs", "type": "bibliography", "children": [
        {"node_id": "notes", "type": "notes", "children": [
            {"node_id": "child", "type": "chapter"},
        ]},
    ]}], [{"node_id": "child", "index_path": [1, 1, 1], "file": "child.md"}])
    assert load_pdf_file_roles(tmp_path) == {"child.md": "notes"}


def test_non_inheritable_frontmatter_role_does_not_flow_to_child(tmp_path):
    _save(tmp_path, [{"node_id": "front", "type": "frontmatter", "children": [
        {"node_id": "chapter", "type": "chapter"},
    ]}], [{"node_id": "chapter", "index_path": [1, 1], "file": "chapter.md"}])
    assert load_pdf_file_roles(tmp_path) == {}


def test_legacy_toc_position_projection_has_role_origin_for_command_helper(tmp_path):
    (tmp_path / "ocr_markdown").mkdir()
    (tmp_path / "ocr_markdown" / "tree_progress.json").write_text(
        json.dumps({"units": [{"file": "chapter_1.md"}]}), encoding="utf-8"
    )
    (tmp_path / "toc_tree.json").write_text(json.dumps({"chapters": [
        {"title": "References", "type": "bibliography"},
    ]}), encoding="utf-8")
    details = load_pdf_file_role_details(tmp_path)
    assert details["chapter_1.md"]["role_origin"] == {
        "source": "legacy_position", "node_id": None, "index_path": [1],
    }
    from pdf2epub.commands.markdown import _pdf_file_role_sources
    assert _pdf_file_role_sources(tmp_path) == {
        "chapter_1.md": details["chapter_1.md"]["role_origin"],
    }


def test_progress_special_role_overrides_generic_and_conflicts_with_toc_special(tmp_path):
    _save(tmp_path, [{"node_id": "unit", "type": "index"}], [
        {"node_id": "unit", "index_path": [1], "file": "unit.md", "type": "bibliography"},
    ])
    with pytest.raises(ValueError, match="Conflicting explicit roles"):
        load_pdf_file_roles(tmp_path)
    (tmp_path / "ocr_markdown" / "tree_progress.json").write_text(
        json.dumps({"units": [{"node_id": "unit", "index_path": [1], "file": "unit.md", "type": "Chapter"}]}),
        encoding="utf-8",
    )
    assert load_pdf_file_roles(tmp_path) == {"unit.md": "index"}


def test_identity_mismatch_does_not_fall_back_to_position_and_modern_toc_has_no_legacy_extras(tmp_path):
    _save(tmp_path, [{"node_id": "known", "type": "index"}], [
        {"node_id": "unknown", "index_path": [1], "file": "part.md"},
    ])
    with pytest.raises(ValueError, match="absent from toc_tree"):
        load_pdf_file_roles(tmp_path)
    _save(tmp_path, [{"node_id": "known", "type": "index"}], [
        {"node_id": "known", "index_path": [1], "part_files": ["part.md"]},
    ])
    assert load_pdf_file_roles(tmp_path) == {"part.md": "index"}
    assert "chapter_1.md" not in load_pdf_file_roles(tmp_path)


@pytest.mark.parametrize("unit,match", [
    ({"node_id": "known", "index_path": [] , "file": "u.md"}, "index_path"),
    ({"index_path": [2], "file": "u.md"}, "absent from toc_tree"),
    ({"node_id": "known", "index_path": [1], "file": "../u.md"}, "filenames"),
    ({"node_id": "known", "index_path": [1], "part_files": [], "file": "u.md"}, "part_files"),
    ({"node_id": "known", "index_path": [1], "part_files": ["u.md", "u.md"]}, "Duplicate physical"),
])
def test_malformed_progress_identity_path_and_file_contracts_are_clear(tmp_path, unit, match):
    _save(tmp_path, [{"node_id": "known", "type": "index"}], [unit])
    with pytest.raises(ValueError, match=match):
        load_pdf_file_roles(tmp_path)


@pytest.mark.parametrize("artifact,content", [
    ("progress", "{"),
    ("toc", "[]"),
])
def test_malformed_role_artifact_fails_with_value_error(tmp_path, artifact, content):
    (tmp_path / "ocr_markdown").mkdir(exist_ok=True)
    (tmp_path / "ocr_markdown" / "tree_progress.json").write_text(
        content if artifact == "progress" else json.dumps({"units": []}), encoding="utf-8"
    )
    if artifact == "toc":
        (tmp_path / "toc_tree.json").write_text(content, encoding="utf-8")
    with pytest.raises(ValueError, match="JSON"):
        load_pdf_file_roles(tmp_path)
