from pathlib import Path

import pytest

from pdf2epub.utils.common import book_output_dir, sanitize_filename
from pdf2epub.utils.html_safety import sanitize_html_document


def test_sanitize_filename_replaces_separators_and_reserved_names() -> None:
    assert sanitize_filename(r"A/B: C") == "A_B_ C"
    assert sanitize_filename("CON.txt").startswith("_CON")
    assert sanitize_filename("...") == "untitled"


def test_book_output_dir_stays_under_output_root(tmp_path: Path) -> None:
    result = book_output_dir("../outside", tmp_path / "output")
    assert result.parent == (tmp_path / "output").resolve()
    assert result.name == "_outside"


@pytest.mark.parametrize("title", ["", "..", "CON", "A\\B:C"])
def test_book_output_dir_always_returns_safe_child(title: str, tmp_path: Path) -> None:
    root = (tmp_path / "output").resolve()
    result = book_output_dir(title, root)
    assert result.parent == root


def test_complete_html_keeps_epub_head_but_removes_active_content() -> None:
    document = (
        '<?xml version="1.0"?>\n'
        '<!DOCTYPE html><html xmlns="http://www.w3.org/1999/xhtml">'
        '<head><style>.book { color: red; }</style><script>alert(1)</script></head>'
        '<body><p onclick="alert(1)">safe</p><iframe src="bad"></iframe>'
        '<a href="javascript:alert(1)">bad</a></body></html>'
    )

    result = sanitize_html_document(document).lower()

    assert "<style" in result
    assert "color: red" in result
    assert "<script" not in result
    assert "<iframe" not in result
    assert "onclick" not in result
    assert "javascript:" not in result
