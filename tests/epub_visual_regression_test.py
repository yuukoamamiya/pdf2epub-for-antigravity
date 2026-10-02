from __future__ import annotations

import hashlib
import re
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZIP_STORED, ZipFile

import pymupdf

from pdf2epub.html_translation.builder import BuildConfig, HTMLEpubBuilder


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
