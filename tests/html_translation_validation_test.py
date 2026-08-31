from pdf2epub.html_translation.validation import (
    protected_sequence,
    tag_mismatch_count,
)


def test_protected_sequence_keeps_tags_attributes_entities_and_placeholders():
    line = '<a href="chapter.xhtml#one"><span class="term">&amp; {{TERM}}</span></a>'

    assert protected_sequence(line) == [
        '<a href="chapter.xhtml#one">',
        '<span class="term">',
        '&amp;',
        '{{TERM}}',
        '</span>',
        '</a>',
    ]


def test_tag_mismatch_count_rejects_protected_token_changes():
    source = [
        '<a href="chapter.xhtml#one"><span class="term">&amp; {{TERM}}</span></a>'
    ]

    assert tag_mismatch_count(source, source) == 0
    assert tag_mismatch_count(
        source,
        ['<a href="chapter.xhtml#two"><span class="term">&amp; {{TERM}}</span></a>'],
    ) == 1
    assert tag_mismatch_count(
        source,
        ['<a href="chapter.xhtml#one"><span class="term">& {{TERM}}</span></a>'],
    ) == 1
    assert tag_mismatch_count(
        source,
        ['<a href="chapter.xhtml#one"><span class="term">&amp; {{TERM}}</a>'],
    ) == 1
