import pytest

from pdf2epub.refine.footnote_matching import (
    block_text_variants,
    find_exact_signature_spans,
    plain_block_text,
    signature_with_map,
)
from pdf2epub.refine.footnote_apply_resolution import _locate_block


def test_inline_html_and_markdown_wrappers_match_without_added_spaces():
    block = {"html": "<span>Volks</span>« and <em>more</em>"}
    assert plain_block_text(block) == "Volks« and more"
    for source in ("before <span>Volks</span>« after", "before *Volks*« after"):
        spans = find_exact_signature_spans(source, "Volks«")
        assert len(spans) == 1
        start, end = spans[0]
        assert source[start:end] in ("<span>Volks</span>«", "*Volks*«")
        assert source[:start] + source[end:] == "before  after"


def test_nested_inline_and_block_html_keep_visible_separation_and_offsets():
    source = "prefix <p><span><b>First</b></span></p><div>Second</div> suffix"
    signature, offsets = signature_with_map(source)
    assert signature == "prefix First Second suffix"
    assert len(signature) == len(offsets)
    match = find_exact_signature_spans(source, "First Second")
    assert len(match) == 1
    assert "First" in source[match[0][0] : match[0][1]]
    assert "Second" in source[match[0][0] : match[0][1]]
    span = find_exact_signature_spans(source, "First")
    assert len(span) == 1
    assert source[span[0][0] : span[0][1]] == "<p><span><b>First</b></span></p>"
    assert plain_block_text({"html": "A<br>B"}) == "A B"


def test_entities_that_decode_to_angle_brackets_remain_literal_text():
    assert plain_block_text({"html": "&lt;i&gt;note&lt;/i&gt;"}) == "<i>note</i>"
    source = "left &lt;i&gt;note&lt;/i&gt; right"
    assert signature_with_map(source)[0] == "left <i>note</i> right"
    spans = find_exact_signature_spans(source, "&lt;i&gt;note&lt;/i&gt;")
    assert len(spans) == 1
    assert source[spans[0][0] : spans[0][1]] == "&lt;i&gt;note&lt;/i&gt;"


def test_html_entity_literal_variant_matches_encoded_source(tmp_path):
    source = "1 &lt;i&gt;note&lt;/i&gt;"
    variants = block_text_variants(
        tmp_path, 1, {"html": "1 &lt;i&gt;note&lt;/i&gt;"}
    )
    assert _locate_block(
        {"unit.md": "1 note\n\n" + source},
        [{"name": "unit.md", "start_page": 1, "end_page": 1}],
        1,
        variants,
    ) == ({"name": "unit.md", "start_page": 1, "end_page": 1}, (8, 33))
    text_variants = block_text_variants(
        tmp_path, 1, {"text": "1 &lt;i&gt;note&lt;/i&gt;"}
    )
    assert _locate_block(
        {"unit.md": "1 note\n\n" + source},
        [{"name": "unit.md", "start_page": 1, "end_page": 1}],
        1,
        text_variants,
    )[1] == (8, 33)


@pytest.mark.parametrize(
    ("open_entity", "close_entity"),
    [("&lt;", "&gt;"), ("&#60;", "&#62;"), ("&#x3c;", "&#x3e;"), ("&#X3C;", "&#X3E;")],
)
def test_entity_angle_literal_never_falls_back_to_ordinary_body_text(
    tmp_path, open_entity, close_entity
):
    literal = f"1 {open_entity}i{close_entity}note{open_entity}/i{close_entity}"
    variants = block_text_variants(tmp_path, 1, {"html": literal})
    with pytest.raises(ValueError, match="could not locate"):
        _locate_block(
            {"unit.md": "1 note"},
            [{"name": "unit.md", "start_page": 1, "end_page": 1}],
            1,
            variants,
        )


def test_list_items_use_same_inline_html_text_extraction(tmp_path):
    block = {"html": "<ul><li><span>Volks</span>«</li></ul>"}
    assert plain_block_text(block) == "Volks«"
    variants = block_text_variants(tmp_path, 1, block)
    assert "\x00unordered-list\x00- Volks«" in variants


def test_repeated_signature_is_ambiguous_and_numeric_boundary_is_strict():
    assert len(find_exact_signature_spans("5 then 5", "5")) == 2
    assert find_exact_signature_spans("15", "5") == []


def test_wrapped_match_expands_only_wrapper_and_preserves_other_source():
    source = "untouched <em><span>Volks</span></em>« tail"
    spans = find_exact_signature_spans(source, "Volks«")
    assert len(spans) == 1
    start, end = spans[0]
    assert source[start:end] == "<em><span>Volks</span></em>«"
    assert source[:start] + source[end:] == "untouched  tail"


def test_html_wrapper_with_greater_than_attribute_maps_exact_source_span():
    source = "prefix <span title='a > b'>Volks</span>« suffix"
    spans = find_exact_signature_spans(source, "Volks«")
    assert len(spans) == 1
    start, end = spans[0]
    assert source[start:end] == "<span title='a > b'>Volks</span>«"


def test_block_html_boundary_does_not_glue_adjacent_words():
    source = "Alpha<p>Beta</p>"
    assert signature_with_map(source)[0] == "Alpha Beta"
    assert find_exact_signature_spans(source, "AlphaBeta") == []


def test_mathml_whitelist_keeps_existing_math_text_matching():
    assert plain_block_text({"html": "<math><mi>x</mi></math>"}) == "x"
    assert find_exact_signature_spans("value x", "<math><mi>x</mi></math>") == [(6, 7)]


def test_unknown_angle_text_and_urls_are_preserved():
    assert plain_block_text({"html": "<x>literal</x> https://example.test/a?b=1"}) == (
        "<x>literal</x> https://example.test/a?b=1"
    )
    assert plain_block_text({"html": "<a title='a > b'>link</a>"}) == "link"
