import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from xml.etree import ElementTree as ET

from pdf2epub.cli import _resolve_pdf_markdown_source
from pdf2epub.build_epub import (
    flatten_toc_tree,
    build_epub_structure,
    generate_hierarchical_toc_html,
    process_chapter_content,
    generate_hierarchical_toc_ncx,
    resolve_book_metadata,
    write_combined_markdown,
)
from pdf2epub.epub.builder import EpubBuilder
from pdf2epub.epub.converter import ContentConverter


def test_ncx_and_opf_share_publication_identifier(tmp_path: Path) -> None:
    config = SimpleNamespace(
        book_title="Test Book",
        author="Test Author",
        language="en",
    )
    builder = EpubBuilder(config)
    ncx_path = tmp_path / "toc.ncx"
    opf_path = tmp_path / "content.opf"

    assert builder.create_toc_ncx({"chapters": []}, ncx_path)
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

    assert builder.create_toc_ncx({"chapters": []}, ncx_path)
    assert builder.create_toc_html({"chapters": []}, html_path)
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
            "title": "Parent",
            "children": [
                {
                    "title": "First child",
                    "unit_id": "chapter_1",
                    "file_path": tmp_path / "chapter_1.md",
                    "part_files": [],
                }
            ],
        }
    ]

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
            "title": "译名",
            "level": 1,
            "start_page": 1,
            "end_page": 2,
            "children": [{
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


def test_toc_uses_stable_fragment_for_child_without_own_file(tmp_path: Path) -> None:
    parent_file = tmp_path / "chapter_1.md"
    parent_file.write_text("# Parent\n\n## Child\n", encoding="utf-8")
    structure = build_epub_structure(
        flatten_toc_tree([{
            "title": "Parent",
            "level": 1,
            "start_page": 1,
            "end_page": 2,
            "children": [{
                "title": "Child",
                "level": 2,
                "start_page": 1,
                "end_page": 2,
            }],
        }]),
        tmp_path,
    )
    from pdf2epub.build_epub import generate_hierarchical_toc_html
    toc_path = tmp_path / "toc.html"
    assert generate_hierarchical_toc_html(structure, "Book", toc_path)
    toc = toc_path.read_text(encoding="utf-8")
    assert 'href="chapter_1.html#toc-1-1"' in toc


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


def test_process_chapter_content_keeps_related_subsection_heading() -> None:
    processed = process_chapter_content(
        "第一部分附录",
        1,
        "# Appendices to Part One\n\n## A. 简要文献目录\n\nBody",
        True,
    )
    assert "# 第一部分附录" in processed
    assert "## A. 简要文献目录" in processed


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
            {"all_passed": True, "source_sha256": {"chapter_1.md": source_hash}}
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

    assert _resolve_pdf_markdown_source(tmp_path, {}) == (ocr_dir, "ocr")


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
