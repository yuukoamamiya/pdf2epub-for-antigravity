from types import SimpleNamespace

from pdf2epub.epub.converter import ContentConverter
from pdf2epub.markdown_to_html import convert_markdown_to_html


def test_duplicate_title_cleanup_preserves_repeated_symbolic_section_breaks(
    tmp_path,
) -> None:
    chapter = tmp_path / "chapter_1.md"
    original = "# Chapter\n\n## *\n\nFirst section.\n\n## *\n\nSecond section.\n"
    chapter.write_text(original, encoding="utf-8")

    converter = ContentConverter(SimpleNamespace(markdown_dir=tmp_path))

    assert converter.remove_duplicate_titles() == 0
    assert chapter.read_text(encoding="utf-8") == original


def test_markdown_output_normalizes_epub2_ids_and_list_starts() -> None:
    html = convert_markdown_to_html(
        "## 2. Heading\n\n2. Second item\n\n[Jump](#2-heading)\n",
        standalone=False,
    )

    assert 'id="epub-id-2-heading"' in html
    assert 'href="#epub-id-2-heading"' in html
    assert 'start="2"' not in html
    assert 'class="epub-continued-list"' in html
    assert 'style="counter-reset: epub-list 1;"' in html


def test_japanese_ruby_uses_epub2_compatible_spans() -> None:
    html = convert_markdown_to_html("玄関(げんかん)", standalone=False)

    assert '<span class="ruby">玄関<span class="rt">げんかん</span></span>' in html
    assert "<ruby>" not in html
    assert "<rt>" not in html


def test_named_html_entities_converted_to_unicode() -> None:
    html = convert_markdown_to_html(
        "It&rsquo;s &ldquo;smart quotes&rdquo; &hellip; &amp; &lt;tag&gt;",
        standalone=False,
    )

    assert "It’s “smart quotes” … &amp; &lt;tag&gt;" in html
    assert "&rsquo;" not in html
    assert "&ldquo;" not in html
    assert "&rdquo;" not in html
    assert "&hellip;" not in html


def test_raw_html_math_is_converted_to_mathml() -> None:
    html = convert_markdown_to_html(
        "<table><tr><td><math>I_A(A)</math></td>"
        "<td><math>\\Gamma_x^1</math></td></tr></table>",
        standalone=False,
    )

    assert "<msub>" in html
    assert "<mi>Γ</mi>" in html
    assert "I_A(A)" not in html
    assert "\\Gamma_x^1" not in html


def test_generated_html_removes_active_content_and_escapes_title() -> None:
    html = convert_markdown_to_html(
        '<script>alert(1)</script>\n\n[bad](javascript:alert(1))\n\n<span onclick="alert(1)">safe</span>',
        title='A <unsafe> "title"',
        standalone=True,
    )

    assert "<script" not in html.lower()
    assert "javascript:" not in html.lower()
    assert "onclick" not in html.lower()
    assert "<title>A &lt;unsafe&gt; &quot;title&quot;</title>" in html
