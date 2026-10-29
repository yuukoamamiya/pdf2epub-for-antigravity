import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from xml.etree import ElementTree as ET

from pdf2epub.commands.pdf import _resolve_pdf_markdown_source
from pdf2epub.build_epub import (
    add_subchapter_anchors_to_parts,
    flatten_toc_tree,
    build_epub_structure,
    load_refinement_part_order,
    generate_hierarchical_toc_html,
    generate_hierarchical_toc_ncx,
    resolve_book_metadata,
    relocate_leading_page_carryover,
    write_combined_markdown,
)
from pdf2epub.epub.builder import EpubBuilder
from pdf2epub.epub.converter import ContentConverter
from pdf2epub.workflow_contracts import MARKDOWN_VALIDATION_SCHEMA_VERSION


def _set_navigation_binding(entries, mode):
    for entry in entries:
        entry["navigation_binding_mode"] = mode
        _set_navigation_binding(entry.get("children") or [], mode)


def test_refinement_checkpoint_controls_continuation_order(tmp_path: Path) -> None:
    markdown_dir = tmp_path / "translated"
    markdown_dir.mkdir()
    for name in ("chapter_1.part1.md", "chapter_1.part2.md", "chapter_1.part3.md"):
        (markdown_dir / name).write_text(name, encoding="utf-8")
    progress_dir = tmp_path / "ocr_markdown"
    progress_dir.mkdir()
    (progress_dir / "tree_progress.json").write_text(
        json.dumps(
            {
                "units": [
                    {
                        "part_files": [
                            "chapter_1.part1.md",
                            "chapter_1.part3.md",
                            "chapter_1.part2.md",
                        ]
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    order = load_refinement_part_order(tmp_path)
    assert order["chapter_1.part1.md"] == [
        "chapter_1.part1.md",
        "chapter_1.part3.md",
        "chapter_1.part2.md",
    ]
    structure = build_epub_structure(
        [{"title": "Chapter", "level": 1, "index_path": [1]}],
        markdown_dir,
        part_order=order,
    )
    assert [path.name for path in structure[0]["part_files"]] == [
        "chapter_1.part1.md",
        "chapter_1.part3.md",
        "chapter_1.part2.md",
    ]


def test_ncx_and_opf_share_publication_identifier(tmp_path: Path) -> None:
    config = SimpleNamespace(
        book_title="Test Book",
        author="Test Author",
        language="en",
    )
    builder = EpubBuilder(config)
    ncx_path = tmp_path / "toc.ncx"
    opf_path = tmp_path / "content.opf"

    assert generate_hierarchical_toc_ncx(
        [], config.book_title, ncx_path, uid=builder.uid, language=config.language
    )
    assert builder.create_content_opf(
        {"chapters": []},
        tmp_path,
        opf_path,
        all_html_files=[],
    )

    ncx = ET.parse(ncx_path)
    opf = ET.parse(opf_path)
    ncx_uid = ncx.find(
        ".//{http://www.daisy.org/z3986/2005/ncx/}meta[@name='dtb:uid']"
    ).attrib["content"]
    opf_uid = opf.find(
        ".//{http://purl.org/dc/elements/1.1/}identifier"
    ).text
    assert ncx_uid == opf_uid == builder.uid


def test_hierarchical_toc_uses_reader_language_label(tmp_path: Path) -> None:
    ncx_path = tmp_path / "toc.ncx"
    html_path = tmp_path / "toc.html"

    assert generate_hierarchical_toc_ncx(
        [],
        "测试书名",
        ncx_path,
        language="zh",
    )
    assert generate_hierarchical_toc_html(
        [],
        "测试书名",
        html_path,
        language="zh",
    )

    assert "<text>目录</text>" in ncx_path.read_text(encoding="utf-8")
    html = html_path.read_text(encoding="utf-8")
    assert "<title>目录</title>" in html
    assert "<h1>目录</h1>" in html
    assert "Table of Contents" not in html


def test_epub_builder_uses_reader_language_label(tmp_path: Path) -> None:
    builder = EpubBuilder(
        SimpleNamespace(
            book_title="测试书名",
            author="测试作者",
            language="zh",
        )
    )
    ncx_path = tmp_path / "toc.ncx"
    html_path = tmp_path / "toc.html"

    assert generate_hierarchical_toc_ncx(
        [], builder.config.book_title, ncx_path, uid=builder.uid,
        language=builder.config.language,
    )
    assert generate_hierarchical_toc_html(
        [], builder.config.book_title, html_path, language=builder.config.language
    )
    assert "<text>目录</text>" in ncx_path.read_text(encoding="utf-8")
    assert "<title>目录</title>" in html_path.read_text(encoding="utf-8")


def test_epub_builder_writes_author_and_optional_publisher(tmp_path: Path) -> None:
    builder = EpubBuilder(
        SimpleNamespace(
            book_title="Test Book",
            author="Elena Ficara",
            publisher="Walter de Gruyter GmbH",
            language="en",
        )
    )
    opf_path = tmp_path / "content.opf"

    assert builder.create_content_opf(
        {"chapters": []},
        tmp_path,
        opf_path,
        all_html_files=[],
    )

    opf = opf_path.read_text(encoding="utf-8")
    assert "<dc:creator opf:role=\"aut\">Elena Ficara</dc:creator>" in opf
    assert "<dc:publisher>Walter de Gruyter GmbH</dc:publisher>" in opf


def test_hierarchical_ncx_reuses_play_order_for_same_target(
    tmp_path: Path,
) -> None:
    output_path = tmp_path / "toc.ncx"
    structure = [
        {
            "node_id": "parent",
            "title": "Parent",
            "children": [
                {
                    "node_id": "child",
                    "title": "First child",
                    "unit_id": "chapter_1",
                    "file_path": tmp_path / "chapter_1.md",
                    "part_files": [],
                }
            ],
        }
    ]
    _set_navigation_binding(structure, "container_only")

    assert generate_hierarchical_toc_ncx(
        structure,
        "Test Book",
        output_path,
        uid="test-publication-id",
    )

    root = ET.parse(output_path)
    ns = {"n": "http://www.daisy.org/z3986/2005/ncx/"}
    points = root.findall(".//n:navPoint", ns)
    targets = [
        point.find("n:content", ns).attrib["src"]
        for point in points
    ]
    play_orders = [point.attrib["playOrder"] for point in points]
    same_target_orders = [
        order
        for target, order in zip(targets, play_orders)
        if target == "text/chapter_1.html"
    ]

    assert same_target_orders == ["2", "2"]
    assert len({point.attrib["id"] for point in points}) == len(points)


def test_epub_stylesheet_uses_a_valid_quote_string() -> None:
    stylesheet = (
        Path(__file__).parents[1]
        / "pdf2epub"
        / "epub"
        / "resources"
        / "stylesheet.css"
    ).read_text(encoding="utf-8")

    assert 'content: "“";' in stylesheet
    assert 'content: """;' not in stylesheet


def test_flatten_toc_tree_uses_in_place_translated_title_fields() -> None:
    structure = flatten_toc_tree(
        [{
            "node_id": "translated-root",
            "title": "译名",
            "level": 1,
            "start_page": 1,
            "end_page": 2,
            "children": [{
                "node_id": "translated-child",
                "title": "子标题",
                "level": 2,
                "start_page": 1,
                "end_page": 2,
            }],
        }],
        use_translated_titles=True,
    )
    assert structure[0]["title"] == "译名"
    assert structure[0]["children"][0]["title"] == "子标题"


def test_toc_falls_back_to_parent_file_for_unresolved_child_anchor(
    tmp_path: Path,
) -> None:
    parent_file = tmp_path / "chapter_1.md"
    parent_file.write_text("# Parent\n\n## Child\n", encoding="utf-8")
    structure = build_epub_structure(
        flatten_toc_tree([{
            "node_id": "parent",
            "title": "Parent",
            "level": 1,
            "start_page": 1,
            "end_page": 2,
            "children": [{
                "node_id": "child",
                "title": "Child",
                "level": 2,
                "start_page": 1,
                "end_page": 2,
            }],
        }]),
        tmp_path,
    )
    _set_navigation_binding(structure, "container_only")
    from pdf2epub.build_epub import generate_hierarchical_toc_html
    toc_path = tmp_path / "toc.html"
    assert generate_hierarchical_toc_html(structure, "Book", toc_path)
    toc = toc_path.read_text(encoding="utf-8")
    assert 'href="chapter_1.html"' in toc
    assert 'href="chapter_1.html#toc-parent-1"' not in toc


def test_subchapter_anchor_supports_deep_headings_and_nested_markup() -> None:
    converter = object.__new__(ContentConverter)
    html = '<h5 id="old"><strong>Child title</strong></h5>'
    result = converter._add_subchapter_anchors(
        html,
        1,
        [{"title": "Child title", "anchor": "toc-1-1"}],
    )
    assert 'id="toc-1-1"' in result
    assert '<strong>Child title</strong>' in result


def test_subchapter_anchor_searches_later_rendered_part_and_updates_navigation(
    tmp_path: Path,
) -> None:
    converter = object.__new__(ContentConverter)
    rendered = [
        ("chapter_1_part1.html", "<h1>Chapter</h1><p>Early body</p>"),
        ("chapter_1_part2.html", "<h2>Late child</h2><p>Later body</p>"),
    ]

    rendered, locations = add_subchapter_anchors_to_parts(
        converter,
        rendered,
        chapter_index=1,
        subchapter_info=[{"title": "Late child", "anchor": "toc-1-2"}],
    )

    assert 'id="toc-1-2"' in rendered[1][1]
    assert 'id="toc-1-2"' not in rendered[0][1]
    assert locations == {"toc-1-2": "chapter_1_part2.html"}

    structure = [{
        "node_id": "chapter",
        "title": "Chapter",
        "level": 1,
        "unit_id": "chapter_1",
        "file_path": tmp_path / "chapter_1.md",
        "part_files": [
            tmp_path / "chapter_1.part1.md",
            tmp_path / "chapter_1.part2.md",
        ],
        "anchor": "toc-1",
        "children": [{
            "node_id": "missing-child",
            "title": "Late child",
            "level": 2,
            "anchor": "toc-1-2",
            "children": [{
                    "node_id": "nested-child",
                "title": "Nested late child",
                "level": 3,
                "anchor": "toc-1-2-1",
            }],
        }],
    }]
    _set_navigation_binding(structure, "container_only")
    toc_path = tmp_path / "toc.html"
    ncx_path = tmp_path / "toc.ncx"
    assert generate_hierarchical_toc_html(
        structure,
        "Book",
        toc_path,
        anchor_locations=locations,
    )
    assert generate_hierarchical_toc_ncx(
        structure,
        "Book",
        ncx_path,
        anchor_locations=locations,
    )
    assert 'href="chapter_1_part2.html#toc-1-2"' in toc_path.read_text(
        encoding="utf-8"
    )
    assert 'src="text/chapter_1_part2.html#toc-1-2"' in ncx_path.read_text(
        encoding="utf-8"
    )


def test_navigation_falls_back_to_parent_file_for_missing_child_anchor(
    tmp_path: Path,
) -> None:
    structure = [{
        "node_id": "container",
        "title": "Chapter",
        "level": 1,
        "unit_id": "chapter_1",
        "file_path": tmp_path / "chapter_1.md",
        "part_files": [
            tmp_path / "chapter_1.part1.md",
            tmp_path / "chapter_1.part2.md",
        ],
        "anchor": "toc-1",
        "children": [{
            "node_id": "missing-parent",
            "title": "Missing child",
            "level": 2,
            "anchor": "missing-child",
        }],
    }]
    _set_navigation_binding(structure, "container_only")
    toc_path = tmp_path / "toc.html"
    ncx_path = tmp_path / "toc.ncx"
    assert generate_hierarchical_toc_html(
        structure,
        "Book",
        toc_path,
        anchor_locations={},
    )
    assert generate_hierarchical_toc_ncx(
        structure,
        "Book",
        ncx_path,
        anchor_locations={},
    )
    toc = toc_path.read_text(encoding="utf-8")
    ncx = ncx_path.read_text(encoding="utf-8")
    assert 'href="chapter_1_part1.html"' in toc
    assert 'src="text/chapter_1_part1.html"' in ncx
    assert "#missing-child" not in toc
    assert "#missing-child" not in ncx


def test_nested_entries_fall_back_to_first_descendant_file(
    tmp_path: Path,
) -> None:
    structure = [{
        "node_id": "container",
        "title": "Container",
        "level": 1,
        "anchor": "missing-container",
        "children": [{
            "node_id": "missing-parent",
            "title": "Missing parent",
            "level": 2,
            "anchor": "missing-parent",
            "children": [{
                "node_id": "leaf",
                "title": "Leaf",
                "level": 3,
                "unit_id": "chapter_2",
                "file_path": tmp_path / "chapter_2.md",
                "part_files": [tmp_path / "chapter_2.md"],
                "anchor": "missing-leaf",
            }],
        }],
    }]
    _set_navigation_binding(structure, "container_only")
    toc_path = tmp_path / "toc.html"
    ncx_path = tmp_path / "toc.ncx"
    assert generate_hierarchical_toc_html(
        structure,
        "Book",
        toc_path,
        anchor_locations={},
    )
    assert generate_hierarchical_toc_ncx(
        structure,
        "Book",
        ncx_path,
        anchor_locations={},
    )
    toc = toc_path.read_text(encoding="utf-8")
    ncx = ncx_path.read_text(encoding="utf-8")
    assert '<a href="chapter_2.html">Container</a>' in toc
    assert '<a href="chapter_2.html">Missing parent</a>' in toc
    assert 'src="text/chapter_2.html"' in ncx
    for anchor in ("missing-container", "missing-parent", "missing-leaf"):
        assert f"#{anchor}" not in toc
        assert f"#{anchor}" not in ncx


def test_build_epub_structure_points_structural_parent_at_first_child(
    tmp_path: Path,
) -> None:
    markdown_dir = tmp_path / "markdown"
    markdown_dir.mkdir()
    (markdown_dir / "chapter_1.1.md").write_text(
        "## Parent\n\n### Child\n\nChild body", encoding="utf-8"
    )

    structure = build_epub_structure(
        flatten_toc_tree(
            [
                {
                    "node_id": "parent",
                    "title": "Parent",
                    "level": 1,
                    "children": [
                        {
                            "node_id": "child",
                            "title": "Child",
                            "level": 2,
                        }
                    ],
                }
            ]
        ),
        markdown_dir,
    )

    assert "virtual_file_name" not in structure[0]
    assert structure[0]["children"][0]["file_path"].name == "chapter_1.1.md"
    _set_navigation_binding(structure, "visible")
    toc_path = tmp_path / "toc.html"
    assert generate_hierarchical_toc_html(
        structure,
        "Book",
        toc_path,
        anchor_locations={"toc-parent": "chapter_1.1.html", "toc-child": "chapter_1.1.html"},
    )
    toc = toc_path.read_text(encoding="utf-8")
    assert 'href="chapter_1.1.html#toc-parent">Parent</a>' in toc
    assert 'href="chapter_1.1.html#toc-child">Child</a>' in toc


def test_toc_file_target_uses_unit_fragment(tmp_path: Path) -> None:
    chapter = tmp_path / "chapter_1.md"
    chapter.write_text("# Parent\n\nBody", encoding="utf-8")
    structure = build_epub_structure(
        flatten_toc_tree([{
            "node_id": "parent",
            "title": "Parent",
            "level": 1,
        }]),
        tmp_path,
    )
    _set_navigation_binding(structure, "visible")

    toc_path = tmp_path / "toc.html"
    ncx_path = tmp_path / "toc.ncx"
    assert generate_hierarchical_toc_html(
        structure,
        "Book",
        toc_path,
        anchor_locations={"toc-parent": "chapter_1.html"},
    )
    assert generate_hierarchical_toc_ncx(
        structure,
        "Book",
        ncx_path,
        anchor_locations={"toc-parent": "chapter_1.html"},
    )
    assert 'href="chapter_1.html#toc-parent">Parent</a>' in toc_path.read_text(
        encoding="utf-8"
    )
    assert 'src="text/chapter_1.html#toc-parent"' in ncx_path.read_text(
        encoding="utf-8"
    )


def test_relocate_leading_carryover_before_previous_footnotes() -> None:
    generated = {
        "chapter_1.html": (
            '<html><body><h1 id="toc-1">Previous</h1>'
            "<p>Previous body</p>"
            '<div class="footnote-def" id="fn-1">Previous note</div>'
            "</body></html>"
        ),
        "chapter_2.html": (
            '<html><body>'
            '<p>Carry-over <a class="footnote-ref" '
            'href="chapter_2.html#fn-2" id="fnref-chapter_2-2">[2]</a></p>'
            '<h1 id="toc-2">Current</h1><p>Current body</p>'
            '<div class="footnote-def" id="fn-2">'
            '<a class="footnote-backref" '
            'href="chapter_2.html#fnref-chapter_2-2">↩</a>'
            "</div></body></html>"
        ),
    }
    structure = [
        {
            "unit_id": "chapter_1",
            "file_path": Path("chapter_1.md"),
            "part_files": [Path("chapter_1.md")],
            "anchor": "toc-1",
            "children": [],
        },
        {
            "unit_id": "chapter_2",
            "file_path": Path("chapter_2.md"),
            "part_files": [Path("chapter_2.md")],
            "anchor": "toc-2",
            "children": [],
        },
    ]

    records = relocate_leading_page_carryover(generated, structure)

    assert records == [
        {
            "source": "chapter_2.html",
            "destination": "chapter_1.html",
            "moved_blocks": 1,
            "moved_footnotes": ["2"],
        }
    ]
    previous = generated["chapter_1.html"]
    assert previous.index("Carry-over") < previous.index("fn-1")
    assert 'href="chapter_1.html#fn-2"' in previous
    assert 'href="chapter_1.html#fnref-chapter_1-2"' in previous
    current = generated["chapter_2.html"]
    assert "Carry-over" not in current
    assert "id=\"fn-2\"" not in current


def test_combined_markdown_follows_toc_and_split_part_order(tmp_path: Path) -> None:
    chapter_one = tmp_path / "chapter_1.md"
    chapter_one.write_text("# First\n\nFirst body", encoding="utf-8")
    chapter_two_part_one = tmp_path / "chapter_2.part1.md"
    chapter_two_part_one.write_text("# Second\n\nPart one", encoding="utf-8")
    chapter_two_part_two = tmp_path / "chapter_2.part2.md"
    chapter_two_part_two.write_text("Part two", encoding="utf-8")
    output_path = tmp_path / "book_en.md"

    structure = [
        {
            "file_path": chapter_one,
            "part_files": [chapter_one],
            "children": [],
        },
        {
            "file_path": chapter_two_part_one,
            "part_files": [chapter_two_part_one, chapter_two_part_two],
            "children": [],
        },
    ]

    assert write_combined_markdown(structure, output_path) == output_path
    assert output_path.read_text(encoding="utf-8") == (
        "# First\n\nFirst body\n\n# Second\n\nPart one\n\nPart two\n"
    )


def test_pdf_source_stage_can_be_auto_selected_or_explicit(tmp_path: Path) -> None:
    polished_dir = tmp_path / "polished_markdown" / "validated"
    polished_dir.mkdir(parents=True)
    (polished_dir / "chapter_1.md").write_text("polished", encoding="utf-8")
    ocr_dir = tmp_path / "ocr_markdown"
    ocr_dir.mkdir()
    (ocr_dir / "chapter_1.md").write_text("ocr", encoding="utf-8")
    source_hash = hashlib.sha256(
        (ocr_dir / "chapter_1.md").read_bytes()
    ).hexdigest()
    (tmp_path / "polish_validation.json").write_text(
        json.dumps(
                {
                    "schema_version": MARKDOWN_VALIDATION_SCHEMA_VERSION,
                    "task": "polish",
                    "scope": "full",
                    "all_passed": True,
                    "source_sha256": {"chapter_1.md": source_hash},
                    "target_sha256": {
                        "chapter_1.md": hashlib.sha256(
                            (polished_dir / "chapter_1.md").read_bytes()
                        ).hexdigest()
                    },
                }
        ),
        encoding="utf-8",
    )

    assert _resolve_pdf_markdown_source(tmp_path, {}) == (polished_dir, "polished")
    assert _resolve_pdf_markdown_source(
        tmp_path, {"translation": {"source_stage": "ocr"}}
    ) == (ocr_dir, "ocr")


def test_pdf_source_stage_ignores_stale_polished_output(tmp_path: Path) -> None:
    polished_dir = tmp_path / "polished_markdown" / "validated"
    polished_dir.mkdir(parents=True)
    (polished_dir / "chapter_1.md").write_text("old polished", encoding="utf-8")
    ocr_dir = tmp_path / "ocr_markdown"
    ocr_dir.mkdir()
    (ocr_dir / "chapter_1.md").write_text("new ocr", encoding="utf-8")
    (tmp_path / "polish_validation.json").write_text(
        '{"all_passed": true, "source_sha256": {"chapter_1.md": "stale"}}',
        encoding="utf-8",
    )

    assert _resolve_pdf_markdown_source(
        tmp_path, {"translation": {"source_stage": "auto"}}
    ) == (ocr_dir, "ocr")


def test_book_metadata_prefers_explicit_values_and_ignores_unknown(tmp_path: Path) -> None:
    metadata = resolve_book_metadata(
        [
            {"author": "Unknown", "publisher": "Agent Publisher"},
            {"author": "Translated Author"},
        ],
        {"metadata": {"author": "Explicit Author"}},
        tmp_path,
    )

    assert metadata == {
        "author": "Explicit Author",
        "publisher": "Agent Publisher",
    }


def test_subchapter_anchor_preserves_existing_id_and_prefers_exact_later_part():
    converter = object.__new__(ContentConverter)
    rendered = [
        ("part1.html", '<h2 id="old">1: Introduction</h2>'),
        ("part2.html", '<h2 id="existing">1. Introduction</h2>'),
    ]
    rendered, locations = add_subchapter_anchors_to_parts(
        converter,
        rendered,
        chapter_index=1,
        subchapter_info=[{"title": "1. Introduction", "anchor": "toc-1-1"}],
    )

    assert locations == {"toc-1-1": "part2.html"}
    assert 'id="old"' in rendered[0][1]
    assert 'id="existing"' in rendered[1][1]
    assert 'id="toc-1-1"' in rendered[1][1]

    formatted = [("part1.html", "<h2>2: 商业周期</h2>")]
    rendered, locations = add_subchapter_anchors_to_parts(
        converter, formatted, 1,
        [{"title": "2. 商业周期", "anchor": "toc-1-2"}],
    )
    assert locations == {"toc-1-2": "part1.html"}


def test_subchapter_anchor_requires_unique_unnumbered_fallback_and_avoids_duplicate_ids():
    converter = object.__new__(ContentConverter)
    ambiguous = [
        ("part1.html", "<h2>Introduction</h2>"),
        ("part2.html", "<h3>Introduction</h3>"),
    ]
    rendered, locations = add_subchapter_anchors_to_parts(
        converter, ambiguous, 1,
        [{"title": "Introduction", "anchor": "toc-1-1"}],
    )
    assert locations == {}

    wrong_number = [("part1.html", "<h2>6. \u5546\u4e1a\u5468\u671f</h2>")]
    rendered, locations = add_subchapter_anchors_to_parts(
        converter, wrong_number, 1,
        [{"title": "5. \u5546\u4e1a\u5468\u671f", "anchor": "toc-1-7"}],
    )
    assert locations == {}

    missing_number = [("part1.html", "<h2>\u5546\u4e1a\u5468\u671f</h2>")]
    rendered, locations = add_subchapter_anchors_to_parts(
        converter, missing_number, 1,
        [{"title": "5. \u5546\u4e1a\u5468\u671f", "anchor": "toc-1-2"}],
    )
    assert locations == {"toc-1-2": "part1.html"}
    assert 'id="toc-1-2"' in rendered[0][1]
    first_rendered = list(rendered)
    rendered_again, locations_again = add_subchapter_anchors_to_parts(
        converter, rendered, 1,
        [{"title": "5. \u5546\u4e1a\u5468\u671f", "anchor": "toc-1-2"}],
    )
    assert locations_again == locations
    assert rendered_again == first_rendered

    conflicting = [
        ("part1.html", "<h2>\u5546\u4e1a\u5468\u671f</h2>"),
        ("part2.html", "<h2>6. \u5546\u4e1a\u5468\u671f</h2>"),
    ]
    rendered, locations = add_subchapter_anchors_to_parts(
        converter, conflicting, 1,
        [{"title": "5. \u5546\u4e1a\u5468\u671f", "anchor": "toc-1-4"}],
    )
    assert locations == {}

    different_chinese = [("part1.html", "<h2>5. \u5546\u4e1a\u5468\u671f</h2>")]
    rendered, locations = add_subchapter_anchors_to_parts(
        converter, different_chinese, 1,
        [{"title": "5. \u5229\u6da6\u7406\u8bba", "anchor": "toc-1-3"}],
    )
    assert locations == {}

    punctuation = [("part1.html", "<h2>A+B</h2>")]
    rendered, locations = add_subchapter_anchors_to_parts(
        converter, punctuation, 1,
        [{"title": "AB", "anchor": "toc-1-5"}],
    )
    assert locations == {}

    data_id = [("part1.html", '<h2 data-id="toc-1-6">Introduction</h2>')]
    rendered, locations = add_subchapter_anchors_to_parts(
        converter, data_id, 1,
        [{"title": "Introduction", "anchor": "toc-1-6"}],
    )
    assert locations == {"toc-1-6": "part1.html"}
    assert rendered[0][1].count('<span id="toc-1-6">') == 1

    already_anchored = [("part1.html", '<h2 id="toc-1-1">Introduction</h2>')]
    rendered, locations = add_subchapter_anchors_to_parts(
        converter, already_anchored, 1,
        [{"title": "Introduction", "anchor": "toc-1-1"}],
    )
    assert locations == {"toc-1-1": "part1.html"}
    assert rendered[0][1].count('id="toc-1-1"') == 1

    misplaced = [("part1.html", '<h2 id="toc-1-8">Other title</h2><h2>Introduction</h2>')]
    rendered, locations = add_subchapter_anchors_to_parts(
        converter, misplaced, 1,
        [{"title": "Introduction", "anchor": "toc-1-8"}],
    )
    assert locations == {}

    duplicate = [("part1.html", '<h2 id="toc-1-9">Introduction</h2><p id="toc-1-9">Introduction</p>')]
    rendered, locations = add_subchapter_anchors_to_parts(
        converter, duplicate, 1,
        [{"title": "Introduction", "anchor": "toc-1-9"}],
    )
    assert locations == {}
