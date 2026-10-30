from __future__ import annotations

import hashlib
import json
import os
import posixpath
import re
import shutil
from pathlib import Path
from xml.etree import ElementTree as ET
from zipfile import ZIP_DEFLATED, ZIP_STORED, ZipFile

import pymupdf
from lxml import etree

from pdf2epub.build_epub import BuildEpubConfig, build_epub
from pdf2epub.epub.footnotes import validate_footnote_graph
from pdf2epub.html_translation.builder import BuildConfig, HTMLEpubBuilder
from pdf2epub.toc_model import TOC_TREE_SCHEMA_VERSION


OPF = """<?xml version="1.0" encoding="UTF-8"?>
<package xmlns="http://www.idpf.org/2007/opf"
         xmlns:dc="http://purl.org/dc/elements/1.1/" version="3.0">
  <metadata>
    <dc:title>Visual regression fixture</dc:title>
    <dc:language>en</dc:language>
  </metadata>
  <manifest>
    <item id="chapter" href="chapter.xhtml" media-type="application/xhtml+xml"/>
    <item id="styles" href="styles.css" media-type="text/css"/>
  </manifest>
  <spine><itemref idref="chapter"/></spine>
</package>
"""

CONTAINER = """<?xml version="1.0" encoding="UTF-8"?>
<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container" version="1.0">
  <rootfiles>
    <rootfile full-path="OEBPS/content.opf"
              media-type="application/oebps-package+xml"/>
  </rootfiles>
</container>
"""

STYLES = """@page { size: 400px 600px; margin: 0; }
body {
  box-sizing: border-box;
  padding: 48px;
  background: #ffffff;
  color: #111111;
  font-family: sans-serif;
  font-size: 18px;
  line-height: 1.45;
}
h1 { margin: 0 0 24px; max-width: 300px; font-size: 22px; color: #163a5f; }
p { margin: 0; max-width: 300px; }
"""


def _chapter_html(heading: str, paragraph: str) -> str:
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<html xmlns="http://www.w3.org/1999/xhtml">
  <head>
    <title>Visual regression fixture</title>
    <link rel="stylesheet" type="text/css" href="styles.css"/>
  </head>
  <body>
    <h1>{heading}</h1>
    <p>{paragraph}</p>
  </body>
</html>
"""


def _write_epub(path: Path, chapter: str) -> None:
    files = {
        "META-INF/container.xml": CONTAINER,
        "OEBPS/content.opf": OPF,
        "OEBPS/chapter.xhtml": chapter,
        "OEBPS/styles.css": STYLES,
    }
    with ZipFile(path, "w") as archive:
        archive.writestr("mimetype", "application/epub+zip", compress_type=ZIP_STORED)
        for name, content in files.items():
            archive.writestr(name, content, compress_type=ZIP_DEFLATED)


def _render_signature(path: Path, expected_heading: str) -> tuple:
    with pymupdf.open(path) as document:
        assert document.page_count == 1
        page = document[0]
        assert page.rect.width == 400
        assert page.rect.height == 600

        text = page.get_text("text")
        normalized_text = re.sub(r"\s+", " ", text).strip()
        assert expected_heading in normalized_text
        assert "A final EPUB contract remains stable." in normalized_text

        heading_blocks = [
            block
            for block in page.get_text("blocks")
            if expected_heading in re.sub(r"\s+", " ", block[4]).strip()
        ]
        assert len(heading_blocks) == 1
        x0, y0, x1, y1 = heading_blocks[0][:4]
        assert 40 <= x0 < x1 <= 395
        assert 40 <= y0 < y1 <= 150

        pixmap = page.get_pixmap(alpha=False)
        assert pixmap.width == 400
        assert pixmap.height == 600
        samples = bytes(pixmap.samples)
        dark_pixels = sum(
            1
            for offset in range(0, len(samples), pixmap.n)
            if min(samples[offset : offset + 3]) < 245
        )
        assert dark_pixels > 1_000
        return (
            document.page_count,
            round(page.rect.width, 2),
            round(page.rect.height, 2),
            (round(x0, 2), round(y0, 2), round(x1, 2), round(y1, 2)),
            dark_pixels,
            hashlib.sha256(samples).hexdigest(),
        )


def test_final_epub_render_matches_visual_contract(tmp_path: Path) -> None:
    original_epub = tmp_path / "original.epub"
    expected_epub = tmp_path / "expected.epub"
    output_epub = tmp_path / "translated.epub"
    translated_dir = tmp_path / "translated"
    translated_dir.mkdir()

    translated_heading = "Translated visual contract"
    translated_paragraph = "A final EPUB contract remains stable."
    _write_epub(
        original_epub,
        _chapter_html("Original source heading", "Original source paragraph."),
    )
    _write_epub(
        expected_epub,
        _chapter_html(translated_heading, translated_paragraph),
    )
    (translated_dir / "chapter.xhtml").write_text(
        _chapter_html(translated_heading, translated_paragraph),
        encoding="utf-8",
    )

    builder = HTMLEpubBuilder(
        BuildConfig(
            original_epub=original_epub,
            translated_dir=translated_dir,
            output_path=output_epub,
            book_title="Visual regression fixture",
            epubcheck_mode="off",
        )
    )
    assert builder.build() == output_epub

    expected_signature = _render_signature(expected_epub, translated_heading)
    actual_signature = _render_signature(output_epub, translated_heading)
    assert actual_signature == expected_signature

    # Rendering the same final EPUB twice must be pixel-stable in the local
    # reader engine; this catches accidental dependence on transient staging.
    assert _render_signature(output_epub, translated_heading) == actual_signature


def _save_visual_qa_artifacts(epub_path: Path, stem: str, qa_dir: Path) -> list[str]:
    """Save a final synthetic EPUB, rendered pages, and a contact sheet."""
    qa_dir.mkdir(parents=True, exist_ok=True)
    for pattern in (f"{stem}-page-*.png", f"{stem}-contact-*.png", f"{stem}-contact-*.pdf"):
        for stale_artifact in qa_dir.glob(pattern):
            stale_artifact.unlink()
    saved_epub = qa_dir / f"{stem}.epub"
    shutil.copy2(epub_path, saved_epub)

    page_paths: list[Path] = []
    with pymupdf.open(epub_path) as document:
        for page_number, page in enumerate(document, 1):
            page_path = qa_dir / f"{stem}-page-{page_number:02d}.png"
            page.get_pixmap(matrix=pymupdf.Matrix(1.25, 1.25), alpha=False).save(page_path)
            page_paths.append(page_path)

    sheet_paths: list[str] = []
    for sheet_number, start in enumerate(range(0, len(page_paths), 4), 1):
        sheet_doc = pymupdf.open()
        sheet_page = sheet_doc.new_page(width=900, height=680)
        for offset, page_path in enumerate(page_paths[start : start + 4]):
            column, row = offset % 2, offset // 2
            x0, y0 = 20 + column * 445, 20 + row * 330
            sheet_page.insert_image(
                pymupdf.Rect(x0, y0, x0 + 425, y0 + 300),
                filename=str(page_path),
                keep_proportion=True,
            )
            sheet_page.insert_text((x0, y0 + 318), f"Page {start + offset + 1}", fontsize=10)
        sheet_pdf = qa_dir / f"{stem}-contact-{sheet_number:02d}.pdf"
        sheet_doc.save(sheet_pdf)
        sheet_doc.close()
        with pymupdf.open(sheet_pdf) as rendered_sheet:
            sheet_png = qa_dir / f"{stem}-contact-{sheet_number:02d}.png"
            rendered_sheet[0].get_pixmap(alpha=False).save(sheet_png)
        sheet_pdf.unlink()
        sheet_paths.append(str(sheet_png))

    return [str(saved_epub), *(str(path) for path in page_paths), *sheet_paths]


CHINESE_PARAGRAPHS = [
    "中文段落起始标记甲。这里检验长段落在阅读器中的换行、段落边界与标点：中文，顿号、分号；冒号：问号？感叹号！引号“甲乙”，括号（测试），破折号——以及省略号……。"
    * 5,
    "中文段落起始标记乙。理论论证沿着概念、历史与制度之间的关系展开；句子保持连续，标点保持可读。我们继续加入足够长的内容，以检查实际分页、字形和页面边界。"
    * 5,
    "中文段落结束标记丙。最后一段仍然完整，包含逗号，句号。分号；冒号：括号（完整），引号“保留”，并以这个独特的结束短语收尾。"
    * 5,
]


def _write_pdf_markdown_fixture(output_dir: Path) -> tuple[Path, Path, str]:
    source_dir = output_dir / "ocr_markdown"
    markdown_dir = output_dir / "polished_markdown"
    source_dir.mkdir(parents=True)
    markdown_dir.mkdir(parents=True)

    table_rows = [
        f"| ROW-{index:03d} | 第{index:03d}行的跨页表格内容，保持次序。 |"
        for index in range(1, 65)
    ]
    markdown = "\n\n".join(
        [
            "# 第一章",
            *CHINESE_PARAGRAPHS,
            "17\\. 普通编号论述段落保留数字标记和段落身份，不应变成有序列表。",
            "真正的有序列表如下：\n\n1. 真正列表第一项。\n2. 真正列表第二项。",
            "后续列表从十七开始：\n\n17. 连续列表第十七项。\n18. 连续列表第十八项。",
            "正文脚注引用[^note]继续说明。",
            "| 行号 | 说明 |\n| --- | --- |\n" + "\n".join(table_rows),
            "## 章节层级\n\n主要章节内容。",
            "### 深层导航\n\n深层章节正文。",
            "[^note]: 脚注第一段，提供第一部分说明。\n\n    脚注第二段继续同一条定义，并包含足够长的文字以便验证归属和回链。",
        ]
    ) + "\n"

    source_file = source_dir / "chapter_1.md"
    markdown_file = markdown_dir / "chapter_1.md"
    source_file.write_text(markdown, encoding="utf-8")
    markdown_file.write_text(markdown, encoding="utf-8")
    (source_dir / "tree_progress.json").write_text(
        json.dumps(
            {"units": [{"node_id": "chapter-one", "index_path": [1], "file": source_file.name}]},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    tree = {
        "schema_version": TOC_TREE_SCHEMA_VERSION,
        "book_title": "Synthetic Visual QA",
        "language": "zh",
        "chapters": [
            {
                "node_id": "chapter-one",
                "title": "第一章",
                "level": 1,
                "binding_mode": "visible",
                "children": [
                    {
                        "node_id": "section-two",
                        "title": "章节层级",
                        "level": 2,
                        "binding_mode": "visible",
                        "children": [
                            {
                                "node_id": "section-three",
                                "title": "深层导航",
                                "level": 3,
                                "binding_mode": "visible",
                            }
                        ],
                    }
                ],
            }
        ],
    }
    toc_path = output_dir / "toc_tree.json"
    toc_path.write_text(json.dumps(tree, ensure_ascii=False), encoding="utf-8")
    return source_dir, markdown_dir, str(toc_path)


def _normalized_epub_text(epub_path: Path) -> tuple[list[str], list[tuple[float, float, float, float]]]:
    with pymupdf.open(epub_path) as document:
        page_texts = [page.get_text("text") for page in document]
        boxes = [
            tuple(float(value) for value in span["bbox"])
            for page in document
            for block in page.get_text("dict")["blocks"]
            if "lines" in block
            for line in block["lines"]
            for span in line["spans"]
            if span.get("text", "").strip()
        ]
        for page in document:
            rect = page.rect
            for block in page.get_text("dict")["blocks"]:
                if "lines" not in block:
                    continue
                for line in block["lines"]:
                    for span in line["spans"]:
                        if not span.get("text", "").strip():
                            continue
                        x0, y0, x1, y1 = span["bbox"]
                        assert -1 <= x0 < x1 <= rect.width + 1
                        assert -1 <= y0 < y1 <= rect.height + 1
    return page_texts, boxes


def _assert_nested_pdf_toc(epub_path: Path) -> None:
    with ZipFile(epub_path) as archive:
        archive_members = set(archive.namelist())
        chapter_member = "text/chapter_1.html"
        toc_member = "text/toc.html"
        chapter_root = etree.fromstring(archive.read(chapter_member))
        toc_root = etree.fromstring(archive.read(toc_member))
        ncx_root = ET.fromstring(archive.read("toc.ncx"))

        def assert_href_resolves(document_member: str, href: str) -> tuple[str, str | None]:
            target, separator, fragment = href.partition("#")
            target_member = posixpath.normpath(
                posixpath.join(posixpath.dirname(document_member), target)
            )
            assert target_member in archive_members
            if separator:
                with ZipFile(epub_path) as package:
                    target_root = etree.fromstring(package.read(target_member))
                assert fragment in set(target_root.xpath("//@id"))
            return target_member, fragment if separator else None

    toc_links = toc_root.xpath(
        "//*[contains(concat(' ', normalize-space(@class), ' '), ' toc ')]//*[local-name()='a']"
    )
    links_by_label = {
        " ".join("".join(link.itertext()).split()): link.get("href")
        for link in toc_links
    }
    expected = ["第一章", "章节层级", "深层导航"]
    assert all(label in links_by_label for label in expected)
    assert toc_root.xpath(
        "count(//*[contains(concat(' ', normalize-space(@class), ' '), ' toc ')]//*[local-name()='ul']//*[local-name()='ul'])"
    ) >= 2
    for label in expected:
        href = links_by_label[label]
        assert href and "#" in href
        target_member, fragment = assert_href_resolves(toc_member, href)
        assert target_member == chapter_member
        assert fragment

    ns = {"ncx": "http://www.daisy.org/z3986/2005/ncx/"}
    points = ncx_root.findall(".//ncx:navPoint", ns)
    entries = [
        (point.findtext("ncx:navLabel/ncx:text", namespaces=ns),
         point.find("ncx:content", ns).get("src"))
        for point in points
    ]
    # The EPUB includes a synthetic TOC entry before the logical navigation tree.
    assert entries[0][0] == "目录"
    assert [label for label, _href in entries[1:]] == expected
    depth = ncx_root.find(".//ncx:meta[@name='dtb:depth']", ns)
    assert depth is not None and depth.get("content") == "3"
    for _label, href in entries:
        assert href
        target_member, fragment = assert_href_resolves("toc.ncx", href)
        if _label == "目录":
            assert target_member == toc_member
            assert fragment is None
        else:
            assert target_member == chapter_member
            assert fragment


def test_pdf_markdown_builder_final_epub_preserves_visual_and_structural_contract(
    tmp_path: Path,
) -> None:
    output_dir = tmp_path / "output"
    output_dir.mkdir()
    _source_dir, markdown_dir, toc_path_text = _write_pdf_markdown_fixture(output_dir)
    toc_path = Path(toc_path_text)
    final_epub = output_dir / "synthetic-final.epub"
    built_epub = build_epub(
        BuildEpubConfig(
            book_title="Synthetic Visual QA",
            output_dir=output_dir,
            markdown_dir=markdown_dir,
            toc_tree_path=toc_path,
            translated=False,
            config={"footnotes": {"mode": "auto"}},
            output_epub=final_epub,
        )
    )
    assert built_epub == final_epub and final_epub.is_file()

    with ZipFile(final_epub) as archive:
        chapter_bytes = archive.read("text/chapter_1.html")
    chapter_root = etree.fromstring(chapter_bytes)
    body_text = " ".join("".join(chapter_root.itertext()).split())
    for marker in ("中文段落起始标记甲", "中文段落起始标记乙", "中文段落结束标记丙", "17. 普通编号论述段落"):
        assert marker in body_text

    prose_paragraphs = chapter_root.xpath(
        "//*[local-name()='p' and starts-with(normalize-space(string(.)), '17.') and not(ancestor::*[local-name()='ol'])]"
    )
    assert len(prose_paragraphs) == 1
    actual_list = chapter_root.xpath("//*[local-name()='ol']/*[local-name()='li']")
    assert [" ".join("".join(item.itertext()).split()) for item in actual_list] == [
        "01.真正列表第一项。", "02.真正列表第二项。",
        "17.连续列表第十七项。", "18.连续列表第十八项。",
    ]
    list_markers = chapter_root.xpath("//*[contains(concat(' ', normalize-space(@class), ' '), ' epub-list-marker ')]/text()")
    assert list_markers == ["01.", "02.", "17.", "18."]

    tables = chapter_root.xpath("//*[local-name()='table']")
    assert len(tables) == 1
    row_markers = [
        " ".join("".join(row.itertext()).split()).split()[0]
        for row in tables[0].xpath("./*[local-name()='tbody']/*[local-name()='tr']")
    ]
    expected_rows = [f"ROW-{index:03d}" for index in range(1, 65)]
    assert row_markers == expected_rows

    definitions = chapter_root.xpath("//*[contains(concat(' ', normalize-space(@class), ' '), ' footnote-def ')]")
    references = chapter_root.xpath("//*[contains(concat(' ', normalize-space(@class), ' '), ' footnote-ref ')]")
    assert len(definitions) == len(references) == 1
    definition = definitions[0]
    definition_id = definition.get("id")
    reference_id = references[0].get("id") or references[0].getparent().get("id")
    assert definition_id and reference_id
    note_paragraphs = [" ".join("".join(p.itertext()).split()) for p in definition.xpath(".//*[local-name()='p']")]
    assert any("脚注第一段" in paragraph for paragraph in note_paragraphs)
    assert any("脚注第二段" in paragraph for paragraph in note_paragraphs)
    forward_links = [references[0].get("href")]
    assert any(href and href.endswith(f"#{definition_id}") for href in forward_links)
    back_links = [node.get("href") for node in definition.xpath(".//*[local-name()='a']")]
    assert any(href and href.endswith(f"#{reference_id}") for href in back_links)
    assert validate_footnote_graph({"chapter_1.html": chapter_bytes.decode("utf-8")})[
        "forward_hrefs"
    ] == 1

    _assert_nested_pdf_toc(final_epub)
    page_texts, _boxes = _normalized_epub_text(final_epub)
    assert len(page_texts) >= 3
    full_text = "\n".join(page_texts)
    compact_text = re.sub(r"\s+", "", full_text)
    assert "目录none" not in page_texts[0]
    assert "\ufffd" not in full_text
    for paragraph in CHINESE_PARAGRAPHS:
        assert re.sub(r"\s+", "", paragraph) in compact_text
    for marker in ("中文段落起始标记甲", "中文段落起始标记乙", "中文段落结束标记丙", "深层章节正文"):
        assert marker in full_text
    for marker in (
        "脚注第一段", "脚注第二段", "17.普通编号论述段落",
        "01.真正列表第一项。", "02.真正列表第二项。",
        "17.连续列表第十七项。", "18.连续列表第十八项。",
    ):
        assert re.sub(r"\s+", "", marker) in compact_text
    assert "counter" not in full_text
    for index in range(1, 65):
        complete_row = f"ROW-{index:03d}第{index:03d}行的跨页表格内容，保持次序。"
        assert re.sub(r"\s+", "", complete_row) in compact_text
    row_pages = {
        page_number
        for page_number, text in enumerate(page_texts)
        if re.search(r"ROW-\d{3}", text)
    }
    assert len(row_pages) >= 2, "the synthetic table must span rendered EPUB pages"
    observed_rows = re.findall(r"ROW-(\d{3})", full_text)
    assert observed_rows == [f"{index:03d}" for index in range(1, 65)]

    qa_dir = os.environ.get("PDF2EPUB_VISUAL_QA_DIR")
    if qa_dir:
        artifact_paths = _save_visual_qa_artifacts(final_epub, "pdf-markdown-rich", Path(qa_dir))
        assert artifact_paths


def _write_html_rebuild_input(path: Path) -> None:
    opf = """<?xml version="1.0" encoding="UTF-8"?>
<package xmlns="http://www.idpf.org/2007/opf" xmlns:dc="http://purl.org/dc/elements/1.1/" version="3.0">
 <metadata><dc:title>HTML visual fixture</dc:title><dc:language>en</dc:language></metadata>
 <manifest>
  <item id="chapter" href="chapter.xhtml" media-type="application/xhtml+xml"/>
  <item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>
  <item id="styles" href="styles.css" media-type="text/css"/>
  <item id="ncx" href="toc.ncx" media-type="application/x-dtbncx+xml"/>
 </manifest>
 <spine toc="ncx"><itemref idref="chapter"/></spine>
</package>
"""
    nav = """<?xml version="1.0" encoding="UTF-8"?>
<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops">
 <head><title>HTML visual fixture</title></head><body>
  <nav epub:type="toc"><h1>Contents</h1><ol>
   <li><a href="chapter.xhtml#chapter-one">Chapter One</a><ol>
    <li><a href="chapter.xhtml#section-a">Main Argument</a><ol>
     <li><a href="chapter.xhtml#section-b">Nested Reading</a></li>
    </ol></li>
   </ol></li>
  </ol></nav>
 </body>
</html>
"""
    ncx = """<?xml version="1.0" encoding="UTF-8"?>
<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/" version="2005-1"><head><meta name="dtb:uid" content="visual"/></head>
 <docTitle><text>HTML visual fixture</text></docTitle><navMap>
  <navPoint id="n1" playOrder="1"><navLabel><text>Chapter One</text></navLabel><content src="chapter.xhtml#chapter-one"/>
   <navPoint id="n2" playOrder="2"><navLabel><text>Main Argument</text></navLabel><content src="chapter.xhtml#section-a"/>
    <navPoint id="n3" playOrder="3"><navLabel><text>Nested Reading</text></navLabel><content src="chapter.xhtml#section-b"/></navPoint>
   </navPoint>
  </navPoint>
 </navMap></ncx>
"""
    chapter = _chapter_html(
        "Chapter One",
        "Original paragraph that will be replaced by the Chinese synthetic fixture.",
    ).replace("</body>", "<h2 id=\"section-a\">Main Argument</h2><h3 id=\"section-b\">Nested Reading</h3></body>")
    files = {
        "META-INF/container.xml": CONTAINER,
        "OEBPS/content.opf": opf,
        "OEBPS/chapter.xhtml": chapter,
        "OEBPS/nav.xhtml": nav,
        "OEBPS/toc.ncx": ncx,
        "OEBPS/styles.css": STYLES,
    }
    with ZipFile(path, "w") as archive:
        archive.writestr("mimetype", "application/epub+zip", compress_type=ZIP_STORED)
        for name, content in files.items():
            archive.writestr(name, content, compress_type=ZIP_DEFLATED)


def test_html_rebuild_final_epub_renders_chinese_nested_navigation(tmp_path: Path) -> None:
    original_epub = tmp_path / "original-html.epub"
    final_epub = tmp_path / "rebuilt-html.epub"
    translated_dir = tmp_path / "translated-html"
    translated_dir.mkdir()
    _write_html_rebuild_input(original_epub)
    translated = """<?xml version="1.0" encoding="UTF-8"?>
<html xmlns="http://www.w3.org/1999/xhtml"><head><title>视觉回归</title>
 <link rel="stylesheet" type="text/css" href="styles.css"/></head><body>
 <h1 id="chapter-one">第一章</h1>
 <p>中文段落起始标记。这里包含中文，顿号、分号；冒号：问号？感叹号！引号“保留”，括号（正常），破折号——和省略号……。</p>
 <h2 id="section-a">主要论证</h2><p>第二段内容继续，确保最终 HTML 重建后的中文正文能够由阅读器引擎实际渲染。</p>
 <h3 id="section-b">深层阅读</h3><p>深层导航目标正文完整。</p>
 </body></html>
"""
    (translated_dir / "chapter.xhtml").write_text(translated, encoding="utf-8")
    toc = [
        {"original": "Chapter One", "translated": "第一章", "href": "chapter.xhtml#chapter-one", "level": 1},
        {"original": "Main Argument", "translated": "主要论证", "href": "chapter.xhtml#section-a", "level": 2},
        {"original": "Nested Reading", "translated": "深层阅读", "href": "chapter.xhtml#section-b", "level": 3},
    ]
    report: dict = {}
    builder = HTMLEpubBuilder(
        BuildConfig(
            original_epub=original_epub,
            translated_dir=translated_dir,
            output_path=final_epub,
            book_title="视觉回归",
            translated_metadata={"translated_title": "视觉回归", "target_language_code": "zh", "toc": toc},
            epubcheck_mode="off",
            navigation_report=report,
        )
    )
    assert builder.build() == final_epub
    assert report["nav"]["status"] == "updated"
    assert report["ncx"]["status"] == "updated"

    with ZipFile(final_epub) as archive:
        nav_root = etree.fromstring(archive.read("OEBPS/nav.xhtml"))
        chapter_root = etree.fromstring(archive.read("OEBPS/chapter.xhtml"))
        ncx_root = ET.fromstring(archive.read("OEBPS/toc.ncx"))
    nav_links = nav_root.xpath("//*[local-name()='a']")
    translated_nav = [" ".join("".join(node.itertext()).split()) for node in nav_links]
    assert translated_nav == ["第一章", "主要论证", "深层阅读"]
    assert [len(node.xpath("ancestor::*[local-name()='ol']")) for node in nav_links] == [1, 2, 3]
    ids = set(chapter_root.xpath("//@id"))
    nav_hrefs = nav_root.xpath("//*[local-name()='a']/@href")
    assert [href.split("#", 1)[1] for href in nav_hrefs] == ["chapter-one", "section-a", "section-b"]
    assert all(href.split("#", 1)[1] in ids for href in nav_hrefs)
    ncx_ns = {"ncx": "http://www.daisy.org/z3986/2005/ncx/"}
    ncx_labels = ncx_root.findall(".//ncx:navLabel/ncx:text", ncx_ns)
    assert [node.text for node in ncx_labels] == ["第一章", "主要论证", "深层阅读"]
    ncx_roots = ncx_root.findall(".//ncx:navMap/ncx:navPoint", ncx_ns)
    assert len(ncx_roots) == 1
    ncx_child = ncx_roots[0].find("ncx:navPoint", ncx_ns)
    assert ncx_child is not None and ncx_child.find("ncx:navPoint", ncx_ns) is not None
    assert [node.get("src").split("#", 1)[1] for node in ncx_root.findall(".//ncx:content", ncx_ns)] == [
        "chapter-one", "section-a", "section-b"
    ]

    page_texts, _boxes = _normalized_epub_text(final_epub)
    full_text = "\n".join(page_texts)
    assert "中文段落起始标记" in full_text
    assert "深层导航目标正文完整" in full_text
    assert "\ufffd" not in full_text
    qa_dir = os.environ.get("PDF2EPUB_VISUAL_QA_DIR")
    if qa_dir:
        _save_visual_qa_artifacts(final_epub, "html-rebuild-nested-zh", Path(qa_dir))
