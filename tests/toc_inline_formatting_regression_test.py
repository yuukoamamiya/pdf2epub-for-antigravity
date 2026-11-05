from pdf2epub.toc_model import (
    normalize_toc_source_label,
    project_toc_ownership,
    toc_markdown_visible_title_candidates,
)
from pdf2epub.epub.converter import ContentConverter
import json


def test_source_toc_projection_uses_markdown_inline_visible_text():
    assert normalize_toc_source_label("# Die *Phänomenologie*", markdown_source=True) == "Die Phänomenologie"


def test_source_toc_projection_keeps_escaped_literal_emphasis_and_underscores():
    assert normalize_toc_source_label(r"# \*A\*", markdown_source=True) == "*A*"
    assert normalize_toc_source_label("foo_bar_baz", markdown_source=True) == "foo_bar_baz"


def test_rendered_html_labels_are_not_reparsed_as_markdown():
    assert normalize_toc_source_label("*A*", markdown_source=False) == "*A*"


def test_partial_nested_inline_formatting_projects_to_visible_heading():
    assert normalize_toc_source_label("Die **Phänomeno*logie***", markdown_source=True) == "Die Phänomenologie"
    assert normalize_toc_source_label("A `code` [link](https://example.test) &amp; B", markdown_source=True) == "A code link & B"


def test_toc_literal_markup_requires_markdown_source_evidence():
    assert toc_markdown_visible_title_candidates("# \\*A\\*", "*A*", "heading") == ["*A*"]
    assert toc_markdown_visible_title_candidates("# *A*", "*A*", "heading") == ["A"]
    assert toc_markdown_visible_title_candidates("# A", "*A*", "heading") == []
    assert toc_markdown_visible_title_candidates("# foo_bar_baz", "foobarbaz", "heading") == []


def test_source_ownership_accepts_partial_emphasis_but_keeps_identifier_punctuation():
    projection = project_toc_ownership(
        [
            {"node_id": "em", "title": "Die Phänomenologie"},
            {"node_id": "id", "title": "foo_bar_baz"},
        ],
        {"units": [
            {"node_id": "em", "index_path": [1], "file": "em.md"},
            {"node_id": "id", "index_path": [2], "file": "id.md"},
        ]},
        {"em.md": "# Die **Phänomenologie**", "id.md": "# foo_bar_baz"},
    )
    assert projection["em.md"]["headings"][0]["binding_mode"] == "visible"
    assert projection["id.md"]["headings"][0]["binding_mode"] == "visible"


def test_legacy_marked_toc_requires_exact_source_markdown_title_evidence():
    assert toc_markdown_visible_title_candidates("# *A*", "*A*", "heading") == ["A"]
    assert toc_markdown_visible_title_candidates("# **A**", "*A*", "heading") == []


def test_ownership_does_not_assign_one_source_heading_to_distinct_toc_labels():
    chapters = [{"node_id": "parent", "title": "A", "children": [
        {"node_id": "child", "title": "*A*"},
    ]}]
    progress = {"units": [{"node_id": "parent", "index_path": [1], "file": "u.md"}]}
    try:
        project_toc_ownership(chapters, progress, {"u.md": "# *A*"})
    except ValueError as exc:
        assert "child" in str(exc)
    else:
        raise AssertionError("one source candidate was assigned to two TOC nodes")

    projection = project_toc_ownership(
        [{"node_id": "a", "title": "A"}, {"node_id": "literal", "title": "*A*"}],
        {"units": [
            {"node_id": "a", "index_path": [1], "file": "a.md"},
            {"node_id": "literal", "index_path": [2], "file": "literal.md"},
        ]},
        {"a.md": "# A", "literal.md": "# \\*A\\*"},
    )
    assert projection["a.md"]["headings"][0]["binding_mode"] == "visible"
    assert projection["literal.md"]["headings"][0]["binding_mode"] == "visible"


def test_legacy_visible_occurrence_counts_all_same_visible_source_candidates():
    from pdf2epub.build_epub import add_subchapter_anchors_to_parts
    from pdf2epub.markdown_to_html import convert_markdown_to_html

    source = "# A\n\n## *A*"
    progress = {"units": [{"node_id": "root", "index_path": [1], "file": "u.md"}]}
    tree = [{"node_id": "root", "title": "A", "children": [
        {"node_id": "child", "title": "*A*"},
    ]}]
    projection = project_toc_ownership(tree, progress, {"u.md": source})
    rows = projection["u.md"]["headings"]
    assert [(row["source_occurrence"], row["source_match_count"]) for row in rows] == [(1, 2), (2, 2)]

    converter = ContentConverter.__new__(ContentConverter)
    rendered = convert_markdown_to_html(source, standalone=False)
    anchors = [
        {"title": row["source_title"], "anchor": f"toc-{row['node_id']}",
         "source_occurrence": row["source_occurrence"],
         "source_match_count": row["source_match_count"],
         "source_candidate_kind": row["source_candidate_kind"]}
        for row in rows
    ]
    parts, locations = add_subchapter_anchors_to_parts(
        converter, [("unit.html", rendered)], 1, anchors,
        source_markdown_by_file={"u.md": source},
    )
    assert set(locations) == {"toc-root", "toc-child"}
    assert parts[0][1].count('id="toc-') == 2


def test_marked_duplicate_headings_share_resolved_visible_occurrence_count():
    source = "# *A*\n\n## **A**"
    projection = project_toc_ownership(
        [{"node_id": "root", "title": "*A*", "children": [
            {"node_id": "child", "title": "**A**"},
        ]}],
        {"units": [{"node_id": "root", "index_path": [1], "file": "u.md"}]},
        {"u.md": source},
    )
    rows = projection["u.md"]["headings"]
    assert [(row["expected_visible_title"], row["source_occurrence"], row["source_match_count"])
            for row in rows] == [("A", 1, 2), ("A", 2, 2)]


def test_anchor_evidence_uniqueness_uses_toc_case_and_whitespace_key():
    from pdf2epub.build_epub import add_subchapter_anchors_to_parts
    from pdf2epub.markdown_to_html import convert_markdown_to_html

    source = "# *A*\n\n## a"
    rows = project_toc_ownership(
        [{"node_id": "root", "title": "*A*", "children": [
            {"node_id": "child", "title": "a"},
        ]}],
        {"units": [{"node_id": "root", "index_path": [1], "file": "u.md"}]},
        {"u.md": source},
    )["u.md"]["headings"]
    assert [(row["source_occurrence"], row["source_match_count"]) for row in rows] == [(1, 2), (2, 2)]
    converter = ContentConverter.__new__(ContentConverter)
    rendered = convert_markdown_to_html(source, standalone=False)
    parts, locations = add_subchapter_anchors_to_parts(
        converter,
        [("unit.html", rendered)],
        1,
        [{"title": row["source_title"], "anchor": f"toc-{row['node_id']}",
          "source_occurrence": row["source_occurrence"],
          "source_match_count": row["source_match_count"],
          "source_candidate_kind": row["source_candidate_kind"]} for row in rows],
        source_markdown_by_file={"u.md": source},
    )
    assert set(locations) == {"toc-root", "toc-child"}
    assert parts[0][1].count('id="toc-') == 2


def test_anchor_matching_does_not_strip_literal_asterisks_or_identifier_underscores():
    converter = ContentConverter.__new__(ContentConverter)
    updated, found = converter._add_subchapter_anchor("<h1>*A*</h1>", "*A*", "toc-literal")
    assert found and 'id="toc-literal"' in updated
    _unchanged, found = converter._add_subchapter_anchor("<h1>A</h1>", "*A*", "toc-false")
    assert not found
    _unchanged, found = converter._add_subchapter_anchor(
        "<h1>foo_bar_baz</h1>", "foobarbaz", "toc-identifier"
    )
    assert not found


def test_math_and_superscript_text_is_retained_in_source_projection():
    from pdf2epub.markdown_to_html import convert_markdown_to_html, markdown_visible_text
    from pdf2epub.epub.converter import visible_heading_text
    import re

    rendered = convert_markdown_to_html("# $R^*(t)$", standalone=False)
    actual = visible_heading_text(re.search(r"<h1[^>]*>(.*?)</h1>", rendered, re.S).group(1))
    assert normalize_toc_source_label("$R^*(t)$", markdown_source=True) == actual
    assert normalize_toc_source_label("x<sup>2</sup>", markdown_source=True) == "x2"
    for entity in ("A &amp; B", "A &amp;lt; B"):
        rendered = convert_markdown_to_html(f"# {entity}", standalone=False)
        actual = visible_heading_text(re.search(r"<h1[^>]*>(.*?)</h1>", rendered, re.S).group(1))
        assert normalize_toc_source_label(entity, markdown_source=True).lower() == actual


def test_inline_label_renderer_preserves_block_looking_titles_and_code_math():
    from pdf2epub.markdown_to_html import convert_markdown_to_html, markdown_visible_text
    import html
    import re

    for title in ("1. Introduction", "> Quotation", "- Heading"):
        expected = convert_markdown_to_html(f"# {title}", standalone=False)
        expected_heading = re.search(r"<h1[^>]*>(.*?)</h1>", expected, re.S).group(1)
        expected_heading = html.unescape(re.sub(r"<[^>]+>", "", expected_heading))
        assert markdown_visible_text(title) == expected_heading
    assert markdown_visible_text("`$x$`") == "$x$"


def test_renderer_helper_import_does_not_reset_loguru_handlers():
    from loguru import logger
    from pdf2epub.markdown_to_html import markdown_visible_text

    sink_id = logger.add(lambda _message: None)
    try:
        before = set(logger._core.handlers)
        markdown_visible_text("A *title*")
        assert set(logger._core.handlers) == before
    finally:
        logger.remove(sink_id)


def _validate_translated_titles(tmp_path, titles, markdown_source):
    from pdf2epub.toc_translation_workflow import validate_toc_heading_bindings
    from pdf2epub.toc_model import TOC_HEADING_CONTEXT_PROJECTION_VERSION

    manifest = {
        "toc_heading_contexts": {
            "unit.md": {
                "projection_version": TOC_HEADING_CONTEXT_PROJECTION_VERSION,
                "node_id": "root",
                "anchor": "toc-root",
                "toc_title": titles[0],
                "binding_mode": "visible",
                "children": [
                    {"title": title, "anchor": f"toc-{index}", "binding_mode": "visible"}
                    for index, title in enumerate(titles[1:], 1)
                ],
            }
        }
    }
    (tmp_path / "translate_subagent_manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    target = tmp_path / "translated" / "validated" / "unit.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(markdown_source, encoding="utf-8")
    return validate_toc_heading_bindings(tmp_path, only_files=["unit.md"])


def test_translated_marked_titles_and_literal_stars_require_distinct_sources(tmp_path):
    assert _validate_translated_titles(tmp_path, ["A", "*A*"], "# A\n# \\*A\\*")["valid"]
    report = _validate_translated_titles(tmp_path, ["A", "*A*"], "# *A*")
    assert not report["valid"]


def test_translated_title_number_fallback_does_not_conflict_with_exact_titles(tmp_path):
    report = _validate_translated_titles(
        tmp_path, ["1 Civil War", "Civil War"], "# 1 Civil War\n# Civil War"
    )
    assert report["valid"]


def test_synthetic_epub_uses_source_proven_visible_title_in_html_ncx_and_render(tmp_path):
    import pymupdf
    from pdf2epub.build_epub import BuildEpubConfig, build_epub
    from pdf2epub.toc_model import TOC_TREE_SCHEMA_VERSION

    output = tmp_path / "output"
    source_dir = output / "ocr_markdown"
    markdown_dir = output / "polished_markdown"
    source_dir.mkdir(parents=True)
    markdown_dir.mkdir()
    filename = "chapter_1.md"
    source = "*A*\n===\n\nA visible title with inline formatting.\n"
    (source_dir / filename).write_text(source, encoding="utf-8")
    (markdown_dir / filename).write_text(source, encoding="utf-8")
    (source_dir / "tree_progress.json").write_text(json.dumps({"units": [
        {"node_id": "root", "index_path": [1], "file": filename},
    ]}), encoding="utf-8")
    tree = {
        "schema_version": TOC_TREE_SCHEMA_VERSION,
        "book_title": "Formatting regression",
        "chapters": [{"node_id": "root", "title": "*A*", "level": 1}],
    }
    toc_path = output / "toc_tree.json"
    toc_path.write_text(json.dumps(tree), encoding="utf-8")
    epub_path = build_epub(BuildEpubConfig(
        book_title="Formatting regression",
        output_dir=output,
        markdown_dir=markdown_dir,
        toc_tree_path=toc_path,
        cover_image=output / "missing-cover.jpg",
        config={"footnotes": {"mode": "disabled"}},
    ))

    text_dir = output / "epub_build" / "text"
    chapter = (text_dir / "chapter_1.html").read_text(encoding="utf-8")
    html_toc = (text_dir / "toc.html").read_text(encoding="utf-8")
    ncx = (output / "epub_build" / "toc.ncx").read_text(encoding="utf-8")
    assert '<h1 id="a"><span id="toc-root"></span><em>A</em></h1>' in chapter
    assert "<a href=\"chapter_1.html#toc-root\">A</a>" in html_toc
    assert "<text>A</text>" in ncx
    with pymupdf.open(epub_path) as document:
        assert document.page_count >= 1
        rendered_text = " ".join(page.get_text("text") for page in document)
        assert "A visible title with inline formatting." in rendered_text
        assert "*A*" not in rendered_text


def test_translated_synthetic_epub_uses_exact_translated_markdown_evidence(tmp_path):
    import pymupdf
    from pdf2epub.build_epub import BuildEpubConfig, build_epub
    from pdf2epub.toc_model import TOC_TREE_SCHEMA_VERSION
    from pdf2epub.toc_translation_workflow import prepare_toc_translation_subagent, validate_toc_translation_subagent

    output = tmp_path / "output"
    source_dir = output / "ocr_markdown"
    translated_dir = output / "translated" / "validated"
    source_dir.mkdir(parents=True)
    translated_dir.mkdir(parents=True)
    filename = "chapter_1.md"
    source = "# Source title\n\nSource body.\n"
    translated = "# Das *Café*\n\nÜbersetzter Inhalt.\n"
    (source_dir / filename).write_text(source, encoding="utf-8")
    (translated_dir / filename).write_text(translated, encoding="utf-8")
    (source_dir / "tree_progress.json").write_text(json.dumps({"units": [
        {"node_id": "root", "index_path": [1], "file": filename},
    ]}), encoding="utf-8")
    source_tree = {
        "schema_version": TOC_TREE_SCHEMA_VERSION,
        "book_title": "Translated formatting regression",
        "chapters": [{"node_id": "root", "title": "Source title", "level": 1}],
    }
    toc_path = output / "toc_tree.json"
    toc_path.write_text(json.dumps(source_tree), encoding="utf-8")
    source_bytes_before = toc_path.read_bytes()
    prepare_toc_translation_subagent(output, "English", "German")
    result_path = output / "toc_translation_result.json"
    result = json.loads((output / "toc_translation_template.json").read_text(encoding="utf-8"))
    result["book_title"]["translated"] = "Übersetztes Buch"
    result["entries"][0]["translated"] = "Das *Café*"
    result_path.write_text(json.dumps(result), encoding="utf-8")
    assert validate_toc_translation_subagent(output)["valid"]
    epub_path = build_epub(BuildEpubConfig(
        book_title="Translated formatting regression",
        output_dir=output,
        markdown_dir=translated_dir,
        toc_tree_path=toc_path,
        translated=True,
        target_language="German",
        cover_image=output / "missing-cover.jpg",
        config={"footnotes": {"mode": "disabled"}},
    ))

    text_dir = output / "epub_build" / "text"
    chapter = (text_dir / "chapter_1.html").read_text(encoding="utf-8")
    html_toc = (text_dir / "toc.html").read_text(encoding="utf-8")
    ncx = (output / "epub_build" / "toc.ncx").read_text(encoding="utf-8")
    assert '<h1 id="das-cafe"><span id="toc-root"></span>Das <em>Café</em></h1>' in chapter
    assert "Das Café" in html_toc
    assert "Das Café" in ncx
    assert source_bytes_before == toc_path.read_bytes()
    with pymupdf.open(epub_path) as document:
        rendered_text = " ".join(page.get_text("text") for page in document)
        assert "Übersetzter Inhalt." in rendered_text
        assert "Das Café" in rendered_text
