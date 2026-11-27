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


def test_signature_with_map_decodes_scoped_footnote_keys():
    sig_def, off_def = signature_with_map("[^fnm-number-1-o7]: Sample note.")
    assert sig_def == "1 Sample note."
    assert len(sig_def) == len(off_def)

    sig_ref, off_ref = signature_with_map("Word[^fnm-number-1-o7].")
    assert sig_ref == "Word1."
    assert len(sig_ref) == len(off_ref)


def test_cross_part_polish_semantic_issue_binds_owner_source_file_sha256(tmp_path, monkeypatch):
    import hashlib
    import json
    import pdf2epub.ocr_correction as correction_module
    from pdf2epub.polish_footnote_semantics import build_semantic_review_contract
    from pdf2epub.refine.footnote_polish_review import polish_semantic_issue_tasks
    from pdf2epub.refine.footnote_result_review import validate_result_audit_reviews
    from pdf2epub.workflow_contracts import sha256_file

    monkeypatch.setattr(correction_module, "review_images_are_current", lambda *_args: True)
    ocr_dir = tmp_path / "ocr_markdown"
    polished_dir = tmp_path / "polished_markdown"
    pages_dir = tmp_path / "pages"
    img_dir = tmp_path / "ocr_review_images"
    ocr_dir.mkdir()
    polished_dir.mkdir()
    pages_dir.mkdir()
    img_dir.mkdir()

    part1_raw = "Body prose.[^1]\n\n1 Cross-part orphan note line.\n"
    part2_raw = "Second part body.\n"
    part2_norm = "Second part body.\n\n[^1]: Cross-part orphan note line.\n"
    (ocr_dir / "part1.md").write_bytes(part1_raw.encode("utf-8"))
    (ocr_dir / "part2.md").write_bytes(part2_raw.encode("utf-8"))
    (tmp_path / "footnote_normalized").mkdir()
    (tmp_path / "footnote_normalized" / "part1.md").write_bytes(b"Body prose.[^1]\n")
    (tmp_path / "footnote_normalized" / "part2.md").write_bytes(part2_norm.encode("utf-8"))
    (polished_dir / "part2.md").write_bytes(part2_norm.encode("utf-8"))

    sidecar_path = pages_dir / "page_001.ocr.json"
    block = {"type": "Text", "html": "1 Cross-part orphan note line."}
    sidecar_path.write_text(json.dumps({"blocks": [block]}), encoding="utf-8")
    (img_dir / "page_001.png").write_bytes(b"png")
    img_manifest = tmp_path / "ocr_review_images.json"
    img_manifest.write_text(json.dumps({"source_sha256": "pdf"}), encoding="utf-8")

    contract2 = build_semantic_review_contract("part2.md", part2_norm)
    (tmp_path / "polish_subagent_manifest.json").write_text(json.dumps({
        "task": "polish",
        "source_dir": "footnote_normalized",
        "target_dir": "polished_markdown",
        "source_sha256": {"part2.md": contract2["source_sha256"]},
        "footnote_semantics": {"schema_version": 1, "enabled": True, "files": {"part2.md": contract2}},
    }), encoding="utf-8")
    issue_sidecar = tmp_path / contract2["sidecar"]
    issue_sidecar.parent.mkdir(parents=True, exist_ok=True)
    issue_sidecar.write_text(json.dumps({
        "schema_version": 1,
        "file": "part2.md",
        "status": "issues",
        "source_sha256": contract2["source_sha256"],
        "target_sha256": hashlib.sha256(part2_norm.encode("utf-8")).hexdigest(),
        "context_sha256": contract2["context_sha256"],
        "coverage": {
            "body_reviewed": {"status": "reviewed"},
            "definitions_reviewed": {"status": "reviewed"},
            "cross_boundary_reviewed": {"status": "reviewed"},
        },
        "issues": [{
            "issue_id": "i1",
            "code": "cross_part_orphan",
            "unit_id": None,
            "file": "part2.md",
            "source_excerpt": "Cross-part orphan note line.",
            "target_excerpt": "Cross-part orphan note line.",
            "reason": "Belongs to part1 block.",
            "source_sha256": contract2["source_sha256"],
            "target_sha256": hashlib.sha256(part2_norm.encode("utf-8")).hexdigest(),
            "context_sha256": contract2["context_sha256"],
        }],
    }), encoding="utf-8")

    records = [
        {"name": "part1.md", "chapter_id": "ch1", "start_page": 1, "end_page": 1},
        {"name": "part2.md", "chapter_id": "ch1", "start_page": 2, "end_page": 2},
    ]
    tasks, diagnostics = polish_semantic_issue_tasks(
        tmp_path,
        records,
        {"part1.md": part1_raw, "part2.md": part2_raw},
        {1: {"primary": {"path": "pages/page_001.ocr.json", "sha256": sha256_file(sidecar_path)}}},
        {},
    )
    assert diagnostics == []
    assert len(tasks) == 1
    task = tasks[0]
    assert task["polish_raw_source_file"] == "part1.md"
    assert task["evidence_file_sha256"] == sha256_file(ocr_dir / "part1.md")
    task["visual_files"] = {"1": {"path": "ocr_review_images/page_001.png", "sha256": sha256_file(img_dir / "page_001.png")}}
    task["visual_image_manifest_sha256"] = sha256_file(img_manifest)
    task["source_pdf_sha256"] = "pdf"

    reviews, errs = validate_result_audit_reviews(tmp_path, [task], [{
        "task_id": task["task_id"],
        "evidence_sha256": task["evidence_sha256"],
        "outcome": "legitimate_non_footnote",
        "reason": "Confirmed.",
        "evidence_pages": [1],
    }])
    assert errs == []
    assert len(reviews) == 1
