import json
from pathlib import Path

from pdf2epub.refine.pagination import build_pagination_map


def test_build_pagination_map_detects_arabic_offset_and_roman_pages(tmp_path: Path):
    pages = tmp_path / "pages"
    pages.mkdir()
    (pages / "page_014.md").write_text("Introduction\n1\n", encoding="utf-8")
    (pages / "page_015.md").write_text("A paragraph\n2\n", encoding="utf-8")
    (pages / "page_003.md").write_text("Preface\niv\n", encoding="utf-8")
    output = tmp_path / "pagination_map.json"

    result = build_pagination_map(pages, output)

    assert result["arabic_offset_hint"] == 13
    assert result["offset_confidence"] == "medium"
    assert any(c["kind"] == "roman" for p in result["pages"] for c in p["candidates"])
    assert json.loads(output.read_text(encoding="utf-8"))["physical_page_is_authoritative"]
