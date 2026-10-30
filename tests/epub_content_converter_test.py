from pathlib import Path
from types import SimpleNamespace
from lxml import etree

from pdf2epub.epub.converter import ContentConverter
from pdf2epub.markdown_to_html import (
    _normalize_ordered_list_starts,
    convert_markdown_to_html,
)


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
    assert '<span class="epub-list-marker">02.</span>' in html
    assert "counter-reset" not in html
    etree.fromstring(f'<body xmlns="http://www.w3.org/1999/xhtml">{html}</body>')


def test_ordered_list_markers_preserve_nested_and_continued_numbers() -> None:
    html = convert_markdown_to_html(
        "17. Continued first\n18. Continued second\n\n"
        "A distinct list begins here.\n\n"
        "3. Outer item\n    1. Nested item\n4. Next outer item",
        standalone=False,
    )

    markers = etree.fromstring(
        f'<body xmlns="http://www.w3.org/1999/xhtml">{html}</body>'
    ).xpath("//*[local-name()='ol']/*[local-name()='li']/*[contains(concat(' ', normalize-space(@class), ' '), ' epub-list-marker ')]/text()")
    assert markers == ["17.", "18.", "03.", "01.", "04."]
    assert 'start="17"' not in html
    assert "counter-reset" not in html


def test_ordered_list_marker_materialization_tracks_structured_list_ownership() -> None:
    source = (
        '<ol start="2"><li title="quoted > remains intact" id="outer">'
        '<a id="outer-link" href="#target">Outer</a>'
        '<ul><li id="bullet">Bullet item</li></ul>'
        '<ol start="4"><li id="nested">Nested ordered item</li></ol>'
        '</li><!-- <ol><li>comment text</li></ol> -->'
        '<li id="following">Following outer item</li></ol>'
    )
    html = _normalize_ordered_list_starts(source)
    root = etree.fromstring(f'<body xmlns="http://www.w3.org/1999/xhtml">{html}</body>')
    ordered = root.xpath("//*[local-name()='ol']")
    assert len(ordered) == 2
    markers = "//*[local-name()='li']/*[contains(concat(' ', normalize-space(@class), ' '), ' epub-list-marker ')]"
    assert [node.text for node in ordered[0].xpath("./*[local-name()='li']/*[contains(concat(' ', normalize-space(@class), ' '), ' epub-list-marker ')]")] == [
        "02.", "03."
    ]
    assert [node.text for node in ordered[1].xpath("./*[local-name()='li']/*[contains(concat(' ', normalize-space(@class), ' '), ' epub-list-marker ')]")] == ["04."]
    assert not root.xpath("//*[local-name()='ul']/*[local-name()='li']/*[contains(concat(' ', normalize-space(@class), ' '), ' epub-list-marker ')]")
    assert root.xpath("string(//*[local-name()='li'][@id='outer']/@title)") == "quoted > remains intact"
    assert root.xpath("string(//*[local-name()='li'][@id='nested']/@id)") == "nested"
    assert root.xpath("string(//*[local-name()='a'][@id='outer-link']/@href)") == "#target"
    assert "<!-- <ol><li>comment text</li></ol> -->" in html
    assert 'start="2"' not in html and 'start="4"' not in html
    assert len(root.xpath(markers)) == 3


def test_ordered_list_normalization_preserves_mixed_case_svg_and_cdata() -> None:
    fixtures = (
        '<div><svg xmlns="http://www.w3.org/2000/svg"><linearGradient id="g">'
        '<stop offset="0" /></linearGradient></svg><ol start="17"><li>Item</li></ol></div>',
        '<DIV><![CDATA[alpha<beta]]><OL start="17"><LI>Item</LI></OL></DIV>',
        '<div><!-- <![CDATA[ --><ol start="17"><li>Real</li></ol><![CDATA[x]]></div>',
    )
    for source in fixtures:
        html = _normalize_ordered_list_starts(source)
        root = etree.fromstring(html.encode("utf-8"))
        marker = root.xpath("//*[local-name()='span' and @class='epub-list-marker']")
        assert [node.text for node in marker] == ["17."]
        assert 'start="17"' not in html
    svg_root = etree.fromstring(_normalize_ordered_list_starts(fixtures[0]).encode("utf-8"))
    assert svg_root.xpath("//*[local-name()='linearGradient']/@id") == ["g"]
    assert svg_root.xpath("//*[local-name()='stop']/@offset") == ["0"]
    cdata_html = _normalize_ordered_list_starts(fixtures[1])
    assert "<![CDATA[alpha<beta]]>" in cdata_html


def test_standalone_legacy_footnote_list_uses_only_materialized_markers() -> None:
    html = convert_markdown_to_html(
        '<div class="footnotes"><ol><li>Legacy footnote</li></ol></div>',
        standalone=True,
    )
    root = etree.fromstring(html.encode("utf-8"))
    markers = root.xpath("//*[local-name()='li']/*[contains(concat(' ', normalize-space(@class), ' '), ' epub-list-marker ')]")
    assert [node.text for node in markers] == ["01."]
    styles = "".join(root.xpath("//*[local-name()='style']/text()"))
    assert ".footnotes ol" in styles
    assert "list-style-type: decimal" not in styles


def test_standalone_list_styles_use_materialized_markers_without_css_counters() -> None:
    html = convert_markdown_to_html("3. Continued item", standalone=True)
    root = etree.fromstring(html.encode("utf-8"))

    assert root.xpath("count(//*[local-name()='li']/*[contains(concat(' ', normalize-space(@class), ' '), ' epub-list-marker ')])") == 1
    styles = "".join(root.xpath("//*[local-name()='style']/text()"))
    assert ".epub-list-marker" in styles
    assert "counter(" not in styles

    stylesheet = (
        Path(__file__).parents[1]
        / "pdf2epub"
        / "epub"
        / "resources"
        / "stylesheet.css"
    ).read_text(encoding="utf-8")
    assert "counter(" not in stylesheet


def test_single_markdown_newline_is_a_soft_wrap_by_default() -> None:
    html = convert_markdown_to_html(
        "A philosophical sentence split by a visual line wrap.\n"
        "The sentence continues here.",
        standalone=False,
    )

    assert "<br" not in html
    assert "A philosophical sentence split by a visual line wrap." in html
    assert "The sentence continues here." in html


def test_explicit_hard_break_and_legacy_mode_remain_available() -> None:
    explicit = convert_markdown_to_html(
        "A line<br />\nThe next line",
        standalone=False,
    )
    legacy = convert_markdown_to_html(
        "A line\nThe next line",
        standalone=False,
        preserve_soft_line_breaks=True,
    )

    assert "<br" in explicit
    assert "<br" in legacy


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


def test_simple_latin_math_uses_font_safe_ordinary_letters() -> None:
    html = convert_markdown_to_html("$C$, $M$, $C'$, $C-M-C'$, and $M-C-M'$", standalone=False)

    assert "<i>C</i>" in html
    assert "<i>M</i>" in html
    assert "<i>C'</i>" in html
    assert "<i>C-M-C'</i>" in html
    assert "<i>M-C-M'</i>" in html
    assert "𝑪" not in html
    assert "𝑴" not in html


def test_sanitized_markdown_fragments_remain_well_formed_xhtml() -> None:
    html = convert_markdown_to_html("![cover](cover.png)  \nnext line", standalone=False)

    etree.fromstring(f'<body xmlns="http://www.w3.org/1999/xhtml">{html}</body>')
    assert "<img " in html and "/>" in html
    assert "<br" in html and "/>" in html


def test_url_query_values_are_not_rewritten_as_html_attributes() -> None:
    html = convert_markdown_to_html(
        "<https://youtu.be/example?t=186>",
        standalone=True,
    )

    assert 'href="https://youtu.be/example?t=186"' in html
    assert '?t="186"' not in html


def test_tables_are_wrapped_for_epub_readers() -> None:
    ordinary = convert_markdown_to_html(
        "| Name | 1 | 2 |\n| --- | --- | --- |\n| Animator | x | x |",
        standalone=False,
    )
    wide = convert_markdown_to_html(
        "| A | B | C | D | E | F | G | H |\n"
        "| --- | --- | --- | --- | --- | --- | --- | --- |\n"
        "| 1 | 2 | 3 | 4 | 5 | 6 | 7 | 8 |",
        standalone=False,
    )

    assert '<div class="table-scroll"><table>' in ordinary
    assert "table-scroll--wide" not in ordinary
    assert '<div class="table-scroll table-scroll--wide"><table>' in wide


def test_math_and_code_are_not_misread_as_markdown_emphasis() -> None:
    html = convert_markdown_to_html(
        r"$R^*(t)$ and *ordinary emphasis*."
        "\n\n```tex\n$$z^*$$\n<math>y^*</math>\n```",
        standalone=False,
    )

    assert "<em>ordinary emphasis</em>" in html
    assert "<em>" not in html.split("<code>", 1)[-1]
    assert "math-display" not in html
    assert "&lt;math&gt;y" in html
    assert "&lt;/math&gt;" in html


def test_currency_prefixed_math_keeps_the_currency_sign() -> None:
    html = convert_markdown_to_html(
        r"Cost was $\$1.1 \times 10^9$.",
        standalone=False,
    )

    assert "PDF2EPUBESCAPEDDOLLARTOKEN" not in html
    assert "$1.1" in html


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
