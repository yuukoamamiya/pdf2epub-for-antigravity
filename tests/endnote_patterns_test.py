"""Coverage for the shared endnote regex definitions."""

from pdf2epub.endnote_patterns import MARKDOWN_REF_RE, UNICODE_DIGITS, UNICODE_SUP_RE


def test_unicode_superscripts_match_and_translate_to_ascii():
    match = UNICODE_SUP_RE.search("see note ¹²³ here")
    assert match is not None
    assert match.group("value") == "¹²³"
    assert "¹²³".translate(UNICODE_DIGITS) == "123"


def test_markdown_reference_matches_reference_not_definition():
    assert MARKDOWN_REF_RE.search("body[^7] tail").group("key") == "7"
    assert MARKDOWN_REF_RE.search("body[^note-a] tail").group("key") == "note-a"
    assert MARKDOWN_REF_RE.search("[^7]: definition") is None


def test_markdown_reference_requires_known_key_characters():
    assert MARKDOWN_REF_RE.search("bad[^a b]") is None
    assert MARKDOWN_REF_RE.search("bad[^a/b]") is None
