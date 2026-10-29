import json
from pathlib import Path

from lxml import etree
import pytest

from pdf2epub.epub.footnotes import FootnoteManager
from pdf2epub.footnote_keys import marker_from_label, marker_label_from_key, output_marker_key
from pdf2epub.epub.footnotes.keys import display_footnote_key
from pdf2epub.markdown_to_html import convert_markdown_to_html
from pdf2epub.refine.footnote_apply import apply_footnote_normalization
from pdf2epub.refine.footnote_matching import marker_spans, marker_context_matches
from pdf2epub.refine.footnote_prepare import prepare_footnote_candidates
from pdf2epub.refine.footnote_handoff import prepare_footnote_subagent
from pdf2epub.refine.footnote_signals import (
    leading_footnote_key,
    leading_footnote_marker,
)
from pdf2epub.refine.footnote_validation import validate_footnote_decisions
from pdf2epub.refine.endnote_normalization import normalize_endnotes
from pdf2epub.refine.endnote_normalization import validate_normalized_endnotes
from pdf2epub.footnote_normalization import endnote_definition_keys


def _block(text: str, *, label: str, y0: int) -> dict:
    return {
        "label": label,
        "bbox": [80, y0, 920, y0 + 50],
        "html": text,
    }


def _write_minimal_page(output_dir: Path, blocks: list[dict], markdown: str) -> None:
    (output_dir / "pages").mkdir(parents=True)
    (output_dir / "pages" / "page_001.ocr.json").write_text(
        json.dumps({
            "schema_version": 1,
            "page_number": 1,
            "backend": "test",
            "page_box": [0, 0, 1000, 1000],
            "blocks": blocks,
        }, ensure_ascii=False),
        encoding="utf-8",
    )
    (output_dir / "pages" / "page_001.md").write_text(markdown, encoding="utf-8")


def test_marker_identity_preserves_printed_form_and_case():
    expected = {
        "* Star note": ("fnm-star-1", "*"),
        "** Star note": ("fnm-star-2", "**"),
        "*** Star note": ("fnm-star-3", "***"),
        "† Note": ("fnm-dagger", "†"),
        "‡ Note": ("fnm-double-dagger", "‡"),
        "†† Note": ("fnm-dagger-2", "††"),
        "a) Note": ("fnm-letter-a", "a"),
        "<sup>A</sup> Note": ("fnm-letter-A", "A"),
    }
    for text, (key, label) in expected.items():
        marker = leading_footnote_marker(text)
        assert marker is not None
        assert (marker.key, marker.label) == (key, label)
    assert leading_footnote_key("1 Note") == "1"
    assert leading_footnote_key("① Note") == "1"
    assert leading_footnote_key("a list item") is None
    assert marker_from_label("") is None
    assert marker_from_label("①②") is None
    assert marker_label_from_key("fnm-letter-A") == "A"
    assert marker_label_from_key("fnm-star-1-o10") == "*"
    assert marker_label_from_key("fnm-star-999999999999") is None
    assert output_marker_key("1", "1", 2) == "fnm-number-1-o2"
    assert display_footnote_key("fnm-number-1-o2") == "1"
    assert display_footnote_key("fnm-circle-1-o2") == "①"


def test_special_marker_matching_is_exact_and_ignores_prose_star():
    source = "Multiply x * y; body <sup>a</sup> and <sup>A</sup>."
    assert marker_spans(source, "fnm-letter-a") == [
        (source.index("<sup>a</sup>"), source.index("<sup>a</sup>") + len("<sup>a</sup>"), "sup")
    ]
    assert marker_spans(source, "fnm-letter-A")[0][2] == "sup"
    assert marker_spans("Markdown **bold**", "fnm-star-2") == []
    assert marker_spans("Body <sup>&dagger;</sup>", "fnm-dagger")
    assert marker_spans("Body <sup>&#8224;</sup>", "fnm-dagger")
    assert marker_spans(r"Body \*", "fnm-star-1")


def test_exact_marker_context_selects_one_of_short_repeated_references():
    source = "Left <sup>1</sup>. Right <sup>1</sup>."
    spans = marker_spans(source, "1")
    assert len(spans) == 2
    assert marker_context_matches(source, spans[0], "Left <sup>1</sup>")
    assert not marker_context_matches(source, spans[1], "Left <sup>1</sup>")
    assert not marker_context_matches(source, spans[0], "<sup>1</sup>")
    assert not marker_context_matches(source, spans[1], "<sup>1</sup>")


def test_explicit_special_marker_normalizes_in_typed_endnotes():
    normalized, report = normalize_endnotes(
        {
            "chapter_1.md": "Body <sup>&dagger;</sup>.\n\n```html\n<sup>†</sup>\n```\n",
            "notes.md": "<sup>†</sup> Centralized note.\n",
        },
        {"chapter_1.md": "", "notes.md": "notes"},
    )
    assert report["valid"] is True
    assert normalized["chapter_1.md"] == "Body [^fnm-dagger].\n\n```html\n<sup>†</sup>\n```\n"
    assert normalized["notes.md"] == "[^fnm-dagger]: Centralized note.\n"
    assert validate_normalized_endnotes(normalized, {"notes.md": "notes", "chapter_1.md": ""})["valid"] is True
    raw = {
        "chapter_1.md": "Body <sup>†</sup>.\n",
        "notes.md": "<sup>†</sup> Centralized note.\n",
    }
    assert endnote_definition_keys(raw["notes.md"], file_role="notes") == ["fnm-dagger"]
    assert validate_normalized_endnotes(raw, {"notes.md": "notes", "chapter_1.md": ""})["valid"] is False


def test_endnote_letter_reference_guard_preserves_formula_superscript():
    normalized, report = normalize_endnotes(
        {
            "body.md": "Word<sup>a</sup> and x<sup>a</sup>.\n",
            "notes.md": "<sup>a</sup> Letter note.\n",
        },
        {"body.md": "", "notes.md": "notes"},
    )

    assert report["valid"] is True
    assert normalized["body.md"] == "Word[^fnm-letter-a] and x<sup>a</sup>.\n"
    assert normalized["notes.md"] == "[^fnm-letter-a]: Letter note.\n"
    assert validate_normalized_endnotes(
        normalized, {"notes.md": "notes", "body.md": ""}
    )["valid"] is True


def test_endnote_validation_ignores_unicode_superscripts_inside_fenced_code():
    normalized, report = normalize_endnotes(
        {
            "body.md": "Body <sup>1</sup>.\n\n```text\nValue¹\n```\n",
            "notes.md": "1. Note.\n",
        },
        {"body.md": "", "notes.md": "notes"},
    )

    assert report["valid"] is True
    assert "Value¹" in normalized["body.md"]
    assert validate_normalized_endnotes(
        normalized, {"notes.md": "notes", "body.md": ""}
    )["valid"] is True


def test_endnote_validation_rejects_raw_special_definition_even_if_orphaned():
    roles = {"body.md": "", "notes.md": "notes"}
    normalized_body_raw_definition = validate_normalized_endnotes(
        {
            "body.md": "Body [^fnm-dagger].\n",
            "notes.md": "<sup>†</sup> Note.\n",
        },
        roles,
    )
    orphan_raw_definition = validate_normalized_endnotes(
        {
            "body.md": "Body text.\n",
            "notes.md": "<sup>†</sup> Orphan note.\n",
        },
        roles,
    )

    assert normalized_body_raw_definition["valid"] is False
    assert orphan_raw_definition["valid"] is False
    assert "raw centralized endnote definitions remain" in (
        normalized_body_raw_definition["errors"][0]
    )
    assert "raw centralized endnote definitions remain" in (
        orphan_raw_definition["errors"][0]
    )


def test_scoped_special_endnotes_keep_printed_label_after_key_rewrites():
    body1 = "Body <sup>†</sup>."
    body2 = "Body <sup>†</sup>."
    source = {
        "chapter_1.md": body1,
        "chapter_2.md": body2,
        "notes_1.md": "<sup>†</sup> First note.",
        "notes_2.md": "<sup>†</sup> Second note.",
    }
    scopes = {
        "notes_files": ["notes_1.md", "notes_2.md"],
        "scope_order": ["u1", "u2"],
        "scopes": {
            "u1": {"title": "One", "unit_type": "chapter", "body_files": ["chapter_1.md"]},
            "u2": {"title": "Two", "unit_type": "chapter", "body_files": ["chapter_2.md"]},
        },
        "notes_spans": {
            "notes_1.md": [{"start_line": 1, "end_line": 2, "scope_key": "u1"}],
            "notes_2.md": [{"start_line": 1, "end_line": 2, "scope_key": "u2"}],
        },
        "scope_ref_keys": {
            "u1": [{"key": "fnm-dagger", "file": "chapter_1.md", "line": 1,
                    "start": body1.index("<sup>"), "end": body1.index("</sup>") + len("</sup>"), "kind": "html"}],
            "u2": [{"key": "fnm-dagger", "file": "chapter_2.md", "line": 1,
                    "start": body2.index("<sup>"), "end": body2.index("</sup>") + len("</sup>"), "kind": "html"}],
        },
    }
    normalized, report = normalize_endnotes(
        source,
        {"chapter_1.md": "", "chapter_2.md": "", "notes_1.md": "notes", "notes_2.md": "notes"},
        scopes=scopes,
    )
    assert report["valid"] is True
    assert "[^fnc-u1-fnm-dagger]" in normalized["chapter_1.md"]
    assert "[^fnc-u2-fnm-dagger]" in normalized["chapter_2.md"]
    assert display_footnote_key("fnc-u1-fnm-dagger") == "†"
    assert display_footnote_key("fnc-u2-fnm-dagger") == "†"


@pytest.mark.parametrize(
    ("printed", "key"),
    [("*", "fnm-star-1"), ("**", "fnm-star-2"), ("***", "fnm-star-3"),
     ("†", "fnm-dagger"), ("‡", "fnm-double-dagger"),
     ("a", "fnm-letter-a"), ("A", "fnm-letter-A"), ("①", "1")],
)
def test_page_marker_flows_through_validation_apply_and_xhtml(
    tmp_path: Path, printed: str, key: str
):
    source_dir = tmp_path / "ocr_markdown"
    source_dir.mkdir()
    source = source_dir / "chapter_1.md"
    source.write_text(
        f"Body <sup>{printed}</sup>\n\n{printed} Star note.\n", encoding="utf-8"
    )
    (source_dir / "tree_progress.json").write_text(
        json.dumps({"units": [{
            "unit_id": "chapter_1", "index_path": [1], "file": source.name,
            "page_range": [1, 1],
        }]}),
        encoding="utf-8",
    )
    _write_minimal_page(
        tmp_path,
        [
            _block(f"Body <sup>{printed}</sup>", label="Text", y0=100),
            _block(f"<sup>{printed}</sup> Star note.", label="Footnote", y0=850),
        ],
        f"Body <sup>{printed}</sup>\n\n{printed} Star note.\n",
    )

    report = prepare_footnote_candidates(tmp_path)
    candidate = report["pages"][0]["candidates"][0]
    assert candidate["key"] == key
    assert candidate["marker_label"] == printed
    assert validate_footnote_decisions(tmp_path)["valid"] is True
    result = apply_footnote_normalization(tmp_path)
    assert result["valid"] is True
    normalized_path = tmp_path / "footnote_normalized" / source.name
    normalized = normalized_path.read_text(encoding="utf-8")
    output_key = "fnm-circle-1" if printed == "①" else key
    assert f"Body [^{output_key}]" in normalized
    assert f"[^{output_key}]: Star note." in normalized

    manager = FootnoteManager(
        normalized_path.parent,
        epub_structure=[{
            "unit_id": "chapter_1", "file_path": normalized_path,
            "part_files": [normalized_path],
        }],
    )
    rendered = convert_markdown_to_html(
        normalized, "Book", standalone=False, footnote_manager=manager,
        source_chapter=normalized_path.stem,
    )
    dom = etree.fromstring(f'<body xmlns="http://www.w3.org/1999/xhtml">{rendered}</body>')
    references = dom.xpath('//*[local-name()="a" and contains(@class,"footnote-ref")]')
    assert references
    assert f"[{printed}]" in "".join(references[0].itertext())
    href = references[0].get("href")
    assert href and "#fn-" in href
    assert dom.xpath('//*[@id=$id]', id=href.split("#", 1)[1])


def test_reviewed_repeated_marker_context_binds_each_reference_exactly(tmp_path: Path):
    source_dir = tmp_path / "ocr_markdown"
    source_dir.mkdir()
    source = source_dir / "chapter_1.md"
    source.write_text(
        "Left <sup>*</sup>. Right <sup>*</sup>.\n\n* Left note.\n\n* Right note.\n",
        encoding="utf-8",
    )
    (source_dir / "tree_progress.json").write_text(
        json.dumps({"units": [{
            "unit_id": "chapter_1", "index_path": [1], "file": source.name,
            "page_range": [1, 1],
        }]}), encoding="utf-8",
    )
    _write_minimal_page(
        tmp_path,
        [
            _block("Left <sup>*</sup>. Right <sup>*</sup>.", label="Text", y0=100),
            _block("* Left note.", label="Footnote", y0=820),
            _block("* Right note.", label="Footnote", y0=900),
        ],
        "Left <sup>*</sup>. Right <sup>*</sup>.\n\n* Left note.\n\n* Right note.\n",
    )
    report = prepare_footnote_candidates(tmp_path, auto_accept=False)
    candidates = report["pages"][0]["candidates"]
    for candidate in candidates:
        candidate["confidence"] = "review"
        candidate["requires_marker_context"] = True
    (tmp_path / "footnote_candidates.json").write_text(
        json.dumps(report, ensure_ascii=False), encoding="utf-8"
    )
    (tmp_path / "footnote_decisions.json").write_text(
        json.dumps({"schema_version": 1, "decisions": [
            {"page": 1, "block": 1, "role": "footnote_start", "key": "fnm-star-1",
             "marker_label": "*", "marker_context": "Left <sup>*</sup>"},
            {"page": 1, "block": 2, "role": "footnote_start", "key": "fnm-star-1",
             "marker_label": "*", "marker_context": "Right <sup>*</sup>"},
        ]}), encoding="utf-8",
    )

    assert validate_footnote_decisions(tmp_path)["valid"] is True
    result = apply_footnote_normalization(tmp_path)
    assert result["valid"] is True
    normalized_path = tmp_path / "footnote_normalized" / source.name
    normalized = normalized_path.read_text(encoding="utf-8")
    assert "Left [^fnm-star-1]" in normalized
    assert "Right [^fnm-star-1-o2]" in normalized
    manager = FootnoteManager(
        normalized_path.parent,
        epub_structure=[{
            "unit_id": "chapter_1", "file_path": normalized_path,
            "part_files": [normalized_path],
        }],
    )
    rendered = convert_markdown_to_html(
        normalized, "Book", standalone=False, footnote_manager=manager,
        source_chapter=normalized_path.stem,
    )
    dom = etree.fromstring(f'<body xmlns="http://www.w3.org/1999/xhtml">{rendered}</body>')
    refs = dom.xpath('//*[local-name()="a" and contains(@class,"footnote-ref")]')
    assert len(refs) == 2
    assert ["".join(node.itertext()) for node in refs] == ["[*]", "[*]"]
    assert refs[0].get("href") != refs[1].get("href")
    for ref in refs:
        assert dom.xpath('//*[@id=$id]', id=ref.get("href").split("#", 1)[1])


def test_two_column_numeric_marker_requires_context_and_applies_per_lane(tmp_path: Path):
    source_dir = tmp_path / "ocr_markdown"
    source_dir.mkdir()
    source = source_dir / "chapter_1.md"
    source.write_text(
        "Left <sup>1</sup>. Right <sup>1</sup>.\n\n1 Left note.\n\n1 Right note.\n",
        encoding="utf-8",
    )
    (source_dir / "tree_progress.json").write_text(
        json.dumps({"units": [{
            "unit_id": "chapter_1", "index_path": [1], "file": source.name,
            "page_range": [1, 1],
        }]}), encoding="utf-8",
    )
    blocks = [
        {"label": "Text", "bbox": [70, 100, 450, 130], "html": "Left <sup>1</sup>."},
        {"label": "Text", "bbox": [550, 100, 930, 130], "html": "Right <sup>1</sup>."},
        {"label": "Footnote", "bbox": [70, 850, 450, 900], "html": "1 Left note."},
        {"label": "Footnote", "bbox": [550, 850, 930, 900], "html": "1 Right note."},
    ]
    _write_minimal_page(
        tmp_path, blocks,
        "Left <sup>1</sup>. Right <sup>1</sup>.\n\n1 Left note.\n\n1 Right note.\n",
    )
    paths = prepare_footnote_subagent(tmp_path, book_title="Test Book")
    report = json.loads(paths["report"].read_text(encoding="utf-8"))
    candidates = [item for item in report["pages"][0]["candidates"] if item["block"] in {2, 3}]
    assert {item["block"] for item in candidates} == {2, 3}
    assert all(item.get("requires_marker_context") for item in candidates)

    audit_path = tmp_path / report["visual_audit_manifest"]
    audit_manifest = json.loads(audit_path.read_text(encoding="utf-8"))
    contexts = {2: "Left <sup>1</sup>", 3: "Right <sup>1</sup>"}
    region_audits = []
    for task in audit_manifest.get("tasks", []):
        region_id = task["region_id"]
        region_candidates = [item for item in candidates if item.get("region_id") == region_id]
        if region_candidates:
            region_audits.append({
                "region_id": region_id,
                "disposition": "footnote_region",
                "decisions": [{
                    "page": item["page"], "source": "primary", "block": item["block"],
                    "role": "footnote_start", "key": item["key"],
                    "marker_label": item["marker_label"],
                    "marker_context": contexts[item["block"]],
                } for item in region_candidates],
            })
    decisions = [item for audit in region_audits for item in audit["decisions"]]
    (tmp_path / "footnote_decisions.json").write_text(
        json.dumps({"schema_version": 1, "region_audits": region_audits, "decisions": decisions}),
        encoding="utf-8",
    )
    assert validate_footnote_decisions(tmp_path)["valid"] is True
    applied = apply_footnote_normalization(tmp_path)
    assert applied["valid"] is True
    normalized = (tmp_path / "footnote_normalized" / source.name).read_text(encoding="utf-8")
    assert "Left [^1]" in normalized
    assert "Right [^fnm-number-1-o2]" in normalized
    for item in decisions:
        item["marker_context"] = "<sup>1</sup>"
    (tmp_path / "footnote_decisions.json").write_text(
        json.dumps({"schema_version": 1, "region_audits": region_audits, "decisions": decisions}),
        encoding="utf-8",
    )
    assert validate_footnote_decisions(tmp_path)["valid"] is True
    rejected = apply_footnote_normalization(tmp_path)
    assert rejected["valid"] is False
    assert any("marker" in error for error in rejected["errors"])
