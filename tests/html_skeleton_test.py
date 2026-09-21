import pytest

from pdf2epub.html_translation.skeleton import mask_text, restore_text


def test_skeleton_mask_and_restore_preserves_nested_tags_entities_and_context():
    source = '<a href="chapter.xhtml#one"><i>History</i> &amp; <span>RPG</span></a>\n'

    masked, contract = mask_text(source)

    assert "History" in masked
    assert "RPG" in masked
    assert "<a" not in masked
    assert "⟦HTML_0001⟧" in masked

    translated = masked.replace("History", "《历史》").replace("RPG", "角色扮演游戏")
    assert restore_text(translated, contract) == (
        '<a href="chapter.xhtml#one"><i>《历史》</i> &amp; '
        '<span>角色扮演游戏</span></a>\n'
    )


def test_skeleton_restore_rejects_missing_or_reordered_placeholders():
    masked, contract = mask_text("<i>One</i> and <b>two</b>\n")
    assert "⟦HTML_0001⟧" in masked

    with pytest.raises(ValueError, match="placeholder (sequence|placement) mismatch"):
        restore_text(masked.replace("⟦HTML_0001⟧", ""), contract)

    with pytest.raises(ValueError, match="placeholder (sequence|placement) mismatch"):
        restore_text(masked.replace("⟦HTML_0001⟧", "⟦HTML_0003⟧"), contract)

    moved_masked, moved_contract = mask_text("<i>One</i>\n<b>Two</b>\n")
    moved_lines = moved_masked.splitlines(keepends=True)
    moved = moved_lines[1] + moved_lines[0]
    with pytest.raises(ValueError, match="placeholder placement mismatch"):
        restore_text(moved, moved_contract)
