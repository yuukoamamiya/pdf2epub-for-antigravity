from pdf2epub.markdown_handoff import prepare_markdown_subagent
from pdf2epub.markdown_subagent_validation import validate_markdown_subagent


def _validate_polish(tmp_path, source_text, target_text):
    source_dir = tmp_path / "source"
    target_dir = tmp_path / "target"
    source_dir.mkdir()
    target_dir.mkdir()
    (source_dir / "unit.md").write_text(source_text, encoding="utf-8")
    (target_dir / "unit.md").write_text(target_text, encoding="utf-8")
    prepare_markdown_subagent(
        tmp_path, "polish", source_dir, target_dir, "English", "English"
    )
    return validate_markdown_subagent(tmp_path, "polish", source_dir, target_dir)


def test_polish_entrypoint_rejects_new_numeric_superscript(tmp_path):
    result = _validate_polish(
        tmp_path,
        "An ordinary paragraph.",
        "An ordinary paragraph with a new <sup>42</sup> marker.",
    )
    assert not result["all_passed"]
    assert any("introduced numeric superscript markers" in item["reason"] for item in result["invalid"])


def test_polish_entrypoint_rejects_definition_continuation_moved_to_body(tmp_path):
    result = _validate_polish(
        tmp_path,
        "[^1]: First sentence.\n    The continuation belongs to note one.\n",
        "[^1]: First sentence.\n\nThe continuation belongs to note one.\n",
    )
    assert not result["all_passed"]
    assert any("moved or truncated text outside footnote definition 1" in item["reason"] for item in result["invalid"])


def test_polish_entrypoint_allows_unchanged_math_superscripts_and_definition(tmp_path):
    text = "Equation x<sup>2</sup> + y<sup>2</sup> = z<sup>2</sup>.\n\n[^1]: A note stays in its definition.\n"
    result = _validate_polish(
        tmp_path,
        text,
        text,
    )
    assert result["all_passed"]


def test_polish_entrypoint_rejects_new_unicode_superscript_and_unclosed_definition(tmp_path):
    result = _validate_polish(
        tmp_path,
        "A paragraph.\n\n[^1]: Citation (5.435).\n",
        "A paragraph ⁴².\n\n[^1]: Citation (5.435.\n",
    )
    reasons = [item["reason"] for item in result["invalid"]]
    assert any("introduced Unicode superscript markers" in reason for reason in reasons)
    assert any("introduced an unclosed parenthesis" in reason for reason in reasons)


def test_polish_entrypoint_preserves_source_approved_raw_superscript(tmp_path):
    text = "The original contains <sup>73</sup> as a reviewed exception."
    result = _validate_polish(tmp_path, text, text)
    assert result["all_passed"]
