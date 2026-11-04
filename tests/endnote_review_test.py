import hashlib
import json
import re
from pathlib import Path

import pytest

from pdf2epub.refine.endnote_normalization import (
    EndnoteNormalizationError,
    normalize_endnotes,
    validate_normalized_endnotes,
)
from pdf2epub.refine.endnote_review import (
    AMBIGUITIES_FILENAME,
    DECISIONS_FILENAME,
    collect_endnote_definition_ambiguities,
    validate_endnote_definition_decisions,
    write_endnote_definition_ambiguities,
)
from pdf2epub.refine.footnote_apply import apply_footnote_normalization
from pdf2epub.refine.footnote_handoff import prepare_footnote_subagent
from pdf2epub.refine.footnote_status import footnote_normalization_status
from pdf2epub.refine.footnote_validation import validate_footnote_decisions


def _write_notes_book(tmp_path: Path, notes: str) -> Path:
    source = tmp_path / "ocr_markdown"
    source.mkdir()
    (source / "notes.md").write_text(notes, encoding="utf-8")
    (source / "tree_progress.json").write_text(
        json.dumps(
            {
                "units": [
                    {
                        "unit_id": "notes",
                        "file": "notes.md",
                        "page_range": [1, 1],
                        "type": "notes",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    return source


def _collect(tmp_path: Path, notes: str) -> dict:
    source = _write_notes_book(tmp_path, notes)
    report = collect_endnote_definition_ambiguities(
        tmp_path,
        {"notes.md": (source / "notes.md").read_text(encoding="utf-8")},
    )
    write_endnote_definition_ambiguities(tmp_path, report)
    return report


def test_collector_only_queues_numbered_lines_with_unclear_boundaries(tmp_path: Path):
    report = _collect(
        tmp_path,
        (
            "1. First note.\n"
            "continuation after a colon:\n"
            "2. Ambiguous two.\n"
            "continuation after a comma,\n"
            "3. Ambiguous three.\n"
            "\n"
            "4. Clear after a blank line.\n"
            "5. Clear after a sentence.\n"
            "30 January 2005, wrapped date continuation.\n"
            "ordinary prose line\n"
            "6) Another ambiguous definition.\n"
        ),
    )

    assert [(item["file"], item["line"], item["key"]) for item in report["items"]] == [
        ("notes.md", 3, "2"),
        ("notes.md", 5, "3"),
        ("notes.md", 11, "6"),
    ]
    assert report["schema_version"] == 2
    assert report["source_sha256"]["notes.md"] == hashlib.sha256(
        (tmp_path / "ocr_markdown" / "notes.md").read_bytes()
    ).hexdigest()


def test_collector_ignores_ordinary_paragraph_and_n_space_lines(tmp_path: Path):
    report = _collect(
        tmp_path,
        (
            "1. First note.\n"
            "The prose continues without a definition marker.\n"
            "30 January 2005, wrapped date continuation.\n"
            "\n"
            "2 Second note after a blank line.\n"
        ),
    )

    assert report["items"] == []


def test_inline_superscripts_get_exact_column_identity_and_multiple_candidates(tmp_path: Path):
    report = _collect(tmp_path, "[^69]: First.  <sup>70</sup> Second.  <sup>71</sup> Third.\n")
    assert [(x["kind"], x["key"], x["column"]) for x in report["items"]] == [
        ("inline", "70", 21), ("inline", "71", 44)
    ]
    _write_decisions(tmp_path, [{"file": "notes.md", "line": 1, "role": "definition"}], schema_version=2)
    result = validate_endnote_definition_decisions(tmp_path, report)
    assert result["valid"] is False
    assert any("missing" in error for error in result["errors"])
    _write_decisions(tmp_path, [
        {"file": "notes.md", "line": 1, "kind": "inline", "column": x["column"], "key": x["key"], "role": "definition"}
        for x in report["items"]
    ], schema_version=2)
    assert validate_endnote_definition_decisions(tmp_path, report)["valid"] is True


def test_inline_scanner_skips_fences_inline_code_and_math(tmp_path: Path):
    report = _collect(
        tmp_path,
        "[^1]: Real.  `<sup>2</sup> code`  $x <sup>3</sup>$  <sup>4</sup> Candidate.\n"
        "[^7]: Real.  ``<sup>8</sup> code``  $$x <sup>9</sup>$$  <sup>10</sup> Candidate.\n"
        "```html\n[^5]: Ignored.  <sup>6</sup> code\n```\n",
    )
    assert [(item["key"], item["column"]) for item in report["items"]] == [("4", 58), ("10", 62)]


def _write_decisions(tmp_path: Path, decisions: list[dict], schema_version: int = 1):
    (tmp_path / DECISIONS_FILENAME).write_text(
        json.dumps({"schema_version": schema_version, "decisions": decisions}),
        encoding="utf-8",
    )


def _prepare_inline_fixture(tmp_path: Path, note_text: str):
    config = _write_endnote_apply_fixture(tmp_path)
    (tmp_path / "ocr_markdown" / "notes.md").write_text(note_text, encoding="utf-8")
    prepare_footnote_subagent(tmp_path, book_title="Fresh inline review", config=config)
    candidate = json.loads((tmp_path / AMBIGUITIES_FILENAME).read_text(encoding="utf-8"))["items"][0]
    _write_decisions(tmp_path, [{
        "file": candidate["file"], "line": candidate["line"],
        "kind": candidate["kind"], "column": candidate["column"],
        "key": candidate["key"], "role": "continuation",
    }], schema_version=2)
    return config, candidate


def test_decision_validation_requires_exactly_one_valid_decision_per_item(tmp_path: Path):
    manifest = _collect(
        tmp_path,
        (
            "1. First note.\n"
            "citation continues,\n"
            "2. Ambiguous line.\n"
        ),
    )

    _write_decisions(
        tmp_path,
        [{"file": "notes.md", "line": 3, "role": "continuation", "confidence": "high"}],
    )
    result = validate_endnote_definition_decisions(tmp_path, manifest)
    assert result["valid"] is True
    assert result["status"] == "validated"
    assert result["decisions"][0]["key"] == "2"

    (tmp_path / DECISIONS_FILENAME).unlink()
    result = validate_endnote_definition_decisions(tmp_path, manifest)
    assert result["valid"] is False
    assert any("missing" in error for error in result["errors"])

    _write_decisions(
        tmp_path,
        [
            {"file": "notes.md", "line": 3, "role": "continuation"},
            {"file": "other.md", "line": 9, "role": "definition"},
        ],
    )
    result = validate_endnote_definition_decisions(tmp_path, manifest)
    assert result["valid"] is False
    assert any("does not match an ambiguity" in error for error in result["errors"])

    _write_decisions(
        tmp_path,
        [
            {"file": "notes.md", "line": 3, "role": "continuation"},
            {"file": "notes.md", "line": 3, "role": "definition"},
        ],
    )
    result = validate_endnote_definition_decisions(tmp_path, manifest)
    assert result["valid"] is False
    assert any("duplicate" in error for error in result["errors"])

    _write_decisions(
        tmp_path,
        [{"file": "notes.md", "line": 3, "role": "review_required"}],
    )
    result = validate_endnote_definition_decisions(tmp_path, manifest)
    assert result["valid"] is False
    assert any("unresolved" in error for error in result["errors"])

    _write_decisions(
        tmp_path,
        [{"file": "notes.md", "line": 3, "role": "sideways"}],
    )
    result = validate_endnote_definition_decisions(tmp_path, manifest)
    assert result["valid"] is False
    assert any("unsupported" in error for error in result["errors"])


def test_normalize_continuation_decision_prevents_a_duplicate_definition():
    source = {
        "chapter_1.md": "Body <sup>1</sup> <sup>2</sup>.\n",
        "notes.md": (
            "1. First note.\n"
            "citation continues,\n"
            "2. A wrapped page number starts this sentence.\n"
            "2. The real second note.\n"
            "3. Third note.\n"
        ),
    }
    roles = {"chapter_1.md": "", "notes.md": "notes"}

    with pytest.raises(EndnoteNormalizationError, match="notes.md:3"):
        normalize_endnotes(source, roles)

    normalized, report = normalize_endnotes(
        source,
        roles,
        definition_decisions=[
            {"file": "notes.md", "line": 3, "role": "continuation"}
        ],
    )

    assert report["definition_keys"] == ["1", "2", "3"]
    assert report["duplicate_definition_occurrences"] == []
    assert "[^2]: The real second note." in normalized["notes.md"]
    assert "2. A wrapped page number starts this sentence." in normalized["notes.md"]
    assert report["endnote_definition_decisions_sha256"]


def test_normalize_definition_decision_accepts_the_ambiguous_line():
    source = {
        "chapter_1.md": "Body <sup>1</sup> <sup>2</sup>.\n",
        "notes.md": (
            "1. First note.\n"
            "citation continues,\n"
            "2. A real second note.\n"
        ),
    }

    normalized, report = normalize_endnotes(
        source,
        {"chapter_1.md": "", "notes.md": "notes"},
        definition_decisions=[
            {"file": "notes.md", "line": 3, "role": "definition"}
        ],
    )

    assert report["definition_keys"] == ["1", "2"]
    assert "[^2]: A real second note." in normalized["notes.md"]


def test_normalize_reviewed_inline_definitions_and_continuation_without_line_shift():
    note_text = "[^69]: First note.  <sup>70</sup> Second note.  <sup>71</sup> Third note.\n    next paragraph.\n"
    first = note_text.index("70") + 1
    second = note_text.index("71") + 1
    source = {
        "chapter_1.md": "Body <sup>69</sup>, <sup>70</sup>, <sup>71</sup>.\n",
        "notes.md": note_text,
    }
    normalized, report = normalize_endnotes(
        source,
        {"chapter_1.md": "", "notes.md": "notes"},
        definition_decisions=[
            {"file": "notes.md", "line": 1, "kind": "inline", "column": first, "key": "70", "role": "continuation"},
            {"file": "notes.md", "line": 1, "kind": "inline", "column": second, "key": "71", "role": "definition"},
        ],
    )
    assert "[^71]: Third note." in normalized["notes.md"]
    assert "[^69]: First note.  <sup>70</sup> Second note." in normalized["notes.md"]
    assert "next paragraph." in normalized["notes.md"]
    assert "[^69]" in normalized["chapter_1.md"] and "[^71]" in normalized["chapter_1.md"]
    assert report["definition_keys"] == ["69", "71"]
    assert validate_normalized_endnotes(normalized, {"notes.md": "notes"})["valid"] is False
    assert validate_normalized_endnotes(
        normalized,
        {"notes.md": "notes"},
        reviewed_inline_continuations=[{
            "file": "notes.md", "key": "70",
            "text": "<sup>70</sup> Second note.",
        }],
    )["valid"] is True


def test_inline_definition_splits_skip_continuations_until_next_reviewed_definition():
    note_text = "[^69]: First.  <sup>70</sup> Second.  <sup>71</sup> continuation of 70.  <sup>72</sup> Fourth.\n"
    decisions = []
    for key, role in (("70", "definition"), ("71", "continuation"), ("72", "definition")):
        decisions.append({
            "file": "notes.md", "line": 1, "kind": "inline",
            "column": note_text.index(key) + 1, "key": key, "role": role,
        })
    normalized, _ = normalize_endnotes(
        {"chapter.md": "<sup>69</sup> <sup>70</sup> <sup>72</sup>\n", "notes.md": note_text},
        {"chapter.md": "", "notes.md": "notes"},
        definition_decisions=decisions,
    )
    assert "[^70]: Second.  <sup>71</sup> continuation of 70." in normalized["notes.md"]
    assert "[^72]: Fourth." in normalized["notes.md"]


def test_inline_definition_review_authorizes_split_after_comma():
    note_text = "[^69]: Ends with comma,  <sup>70</sup> New note.\n"
    normalized, report = normalize_endnotes(
        {"chapter.md": "<sup>69</sup> <sup>70</sup>\n", "notes.md": note_text},
        {"chapter.md": "", "notes.md": "notes"},
        definition_decisions=[{
            "file": "notes.md", "line": 1, "kind": "inline",
            "column": note_text.index("70") + 1, "key": "70", "role": "definition",
        }],
    )
    assert "[^70]: New note." in normalized["notes.md"]
    assert report["definition_keys"] == ["69", "70"]


def test_inline_definition_preserves_markdown_continuation_and_nested_code():
    from lxml import etree
    from pdf2epub.markdown_to_html import convert_markdown_to_html
    from pdf2epub.epub.footnotes import FootnoteManager
    from pathlib import Path
    import tempfile

    note_text = (
        "[^69]: First note.  <sup>70</sup> New note.\n"
        "    ordinary continuation paragraph.\n"
        "\n"
        "        nested code line\n"
    )
    normalized, _ = normalize_endnotes(
        {"chapter.md": "<sup>69</sup> <sup>70</sup>\n", "notes.md": note_text},
        {"chapter.md": "", "notes.md": "notes"},
        definition_decisions=[{
            "file": "notes.md", "line": 1, "kind": "inline",
            "column": note_text.index("70") + 1, "key": "70", "role": "definition",
        }],
    )
    assert "    ordinary continuation paragraph." in normalized["notes.md"]
    assert "        nested code line" in normalized["notes.md"]
    with tempfile.TemporaryDirectory() as temp_dir:
        normalized_dir = Path(temp_dir)
        for name, text in normalized.items():
            (normalized_dir / name).write_text(text, encoding="utf-8")
        notes_path = normalized_dir / "notes.md"
        manager = FootnoteManager(
            normalized_dir, auto_global=True,
            epub_structure=[{"unit_id": "notes", "file_path": notes_path, "part_files": [notes_path], "type": "notes"}],
        )
        rendered = convert_markdown_to_html(
            normalized["notes.md"], "Notes", standalone=False,
            footnote_manager=manager, source_chapter=notes_path.stem,
        )
    root = etree.fromstring(f"<div>{rendered}</div>")
    assert root.xpath(".//p[contains(., 'ordinary continuation paragraph')]")
    assert root.xpath(".//pre/code[contains(., 'nested code line')]")


@pytest.mark.parametrize(
    "decision, message",
    [
        ({"kind": "inline", "column": 20, "line": 1, "key": "70", "role": "definition"}, "unreviewed"),
        ({"kind": "inline", "column": "21", "line": 1, "key": "70", "role": "definition"}, "exact integers"),
        ({"kind": "inline", "column": 21, "line": 1, "key": "71", "role": "definition"}, "key mismatch"),
        ({"kind": "inline", "column": 21, "line": 1, "key": "70", "role": "review_required"}, "unresolved"),
    ],
)
def test_direct_normalization_rejects_bad_inline_decision_identity(decision, message):
    line = "[^69]: First.  <sup>70</sup> Second.\n"
    decision["file"] = "notes.md"
    with pytest.raises(EndnoteNormalizationError, match=message):
        normalize_endnotes(
            {"chapter.md": "<sup>69</sup>\n", "notes.md": line},
            {"chapter.md": "", "notes.md": "notes"},
            definition_decisions=[decision],
        )


def test_direct_normalization_rejects_duplicate_inline_decision():
    line = "[^69]: First.  <sup>70</sup> Second.\n"
    decision = {
        "file": "notes.md", "line": 1, "column": line.index("70") + 1,
        "kind": "inline", "key": "70", "role": "continuation",
    }
    with pytest.raises(EndnoteNormalizationError, match="duplicate inline"):
        normalize_endnotes(
            {"chapter.md": "<sup>69</sup>\n", "notes.md": line},
            {"chapter.md": "", "notes.md": "notes"},
            definition_decisions=[decision, dict(decision)],
        )


def test_normalize_leaves_non_ambiguous_definitions_unchanged_by_decisions():
    source = {
        "chapter_1.md": "Body <sup>1</sup> <sup>2</sup>.\n",
        "notes.md": "1. First note.\n\n2. Second note.\n",
    }

    normalized, report = normalize_endnotes(
        source,
        {"chapter_1.md": "", "notes.md": "notes"},
        definition_decisions=[
            {"file": "notes.md", "line": 3, "role": "continuation"}
        ],
    )

    assert report["definition_keys"] == ["1", "2"]
    assert "[^2]: Second note." in normalized["notes.md"]


def _write_endnote_apply_fixture(tmp_path: Path) -> dict:
    pages = tmp_path / "pages"
    pages.mkdir()
    (pages / "page_001.ocr.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "page_number": 1,
                "backend": "test",
                "page_box": [0, 0, 1000, 1000],
                "blocks": [
                    {
                        "order": 0,
                        "label": "Text",
                        "bbox": [80, 100, 920, 160],
                        "html": "Body text",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    source = tmp_path / "ocr_markdown"
    source.mkdir()
    (source / "chapter_1.md").write_text("Body <sup>1</sup>.\n", encoding="utf-8")
    (source / "notes.md").write_text(
        "1. First note.\n"
        "citation continues,\n"
        "2. A wrapped page number starts this sentence.\n",
        encoding="utf-8",
    )
    (source / "tree_progress.json").write_text(
        json.dumps(
            {
                "units": [
                    {
                        "unit_id": "chapter_1",
                        "index_path": [1],
                        "file": "chapter_1.md",
                        "page_range": [1, 1],
                    },
                    {
                        "unit_id": "notes",
                        "index_path": [2],
                        "file": "notes.md",
                        "page_range": [2, 2],
                        "type": "notes",
                    },
                ]
            }
        ),
        encoding="utf-8",
    )
    (tmp_path / "toc_tree.json").write_text(
        json.dumps({"chapters": []}),
        encoding="utf-8",
    )
    return {"footnotes": {"mode": "endnotes_only"}}


def test_prepare_validate_and_apply_wire_endnote_ambiguity_decisions(tmp_path: Path):
    config = _write_endnote_apply_fixture(tmp_path)

    paths = prepare_footnote_subagent(tmp_path, book_title="Review test", config=config)
    manifest = json.loads(paths["manifest"].read_text(encoding="utf-8"))
    prompt = paths["prompt"].read_text(encoding="utf-8")

    assert manifest["endnote_ambiguity_count"] == 1
    assert manifest["endnote_ambiguities"] == AMBIGUITIES_FILENAME
    assert manifest["endnote_decision_file"] == DECISIONS_FILENAME
    assert manifest["status"] == "pending_review"
    assert manifest["page_review_required"] is False
    assert manifest["decision_file"] is None
    assert "Ambiguous centralized endnote definitions" in prompt
    assert DECISIONS_FILENAME in prompt
    assert AMBIGUITIES_FILENAME in prompt
    assert "region_audits" not in prompt
    assert "footnote_start" not in prompt
    assert "footnote_decisions.json" not in prompt

    result = validate_footnote_decisions(tmp_path, config=config)
    assert result["valid"] is False
    assert result["endnote_definitions"]["valid"] is False

    _write_decisions(
        tmp_path,
        [{"file": "notes.md", "line": 3, "role": "continuation", "confidence": "high"}],
    )
    result = validate_footnote_decisions(tmp_path, config=config)
    assert result["valid"] is True
    assert result["endnote_definitions"]["valid"] is True

    applied = apply_footnote_normalization(tmp_path, config=config)
    assert applied["valid"] is True
    normalized = (tmp_path / "footnote_normalized" / "notes.md").read_text(
        encoding="utf-8"
    )
    assert "[^1]: First note." in normalized
    assert "2. A wrapped page number starts this sentence." in normalized
    assert "[^2]:" not in normalized
    assert applied["endnote_definition_ambiguities_sha256"]
    assert applied["endnote_definition_decisions_sha256"]
    status = footnote_normalization_status(tmp_path, config=config)
    assert status["current"] is True
    _write_decisions(
        tmp_path,
        [{"file": "notes.md", "line": 3, "role": "definition", "confidence": "high"}],
    )
    status = footnote_normalization_status(tmp_path, config=config)
    assert status["current"] is False
    assert any(
        failure["code"] == "endnote_definition_decisions_stale"
        for failure in status["failures"]
    )
    assert "footnote-prepare" in status["detail"]


def test_prepare_apply_and_build_gate_accepts_exact_reviewed_inline_continuation(tmp_path: Path):
    from pdf2epub.build_epub import validate_centralized_endnotes_for_build

    config = _write_endnote_apply_fixture(tmp_path)
    (tmp_path / "ocr_markdown" / "notes.md").write_text(
        "[^1]: First note.  <sup>70</sup> Citation continuation.\n\n"
        "```markdown\n[^999]: Fake.  <sup>70</sup> Example.\n```\n",
        encoding="utf-8",
    )
    paths = prepare_footnote_subagent(tmp_path, book_title="Inline continuation", config=config)
    manifest = json.loads(paths["manifest"].read_text(encoding="utf-8"))
    assert manifest["endnote_ambiguity_count"] == 1
    candidates = json.loads((tmp_path / AMBIGUITIES_FILENAME).read_text(encoding="utf-8"))["items"]
    candidate = candidates[0]
    assert candidate["kind"] == "inline" and candidate["key"] == "70"
    _write_decisions(
        tmp_path,
        [{
            "file": candidate["file"], "line": candidate["line"],
            "kind": candidate["kind"], "column": candidate["column"],
            "key": candidate["key"], "role": "continuation",
        }],
        schema_version=2,
    )
    validation = validate_footnote_decisions(tmp_path, config=config)
    assert validation["valid"] is True
    applied = apply_footnote_normalization(tmp_path, config=config)
    assert applied["valid"] is True
    assert footnote_normalization_status(tmp_path, config=config)["current"] is True

    notes_path = tmp_path / "footnote_normalized" / "notes.md"
    result = validate_centralized_endnotes_for_build(
        [{"type": "notes", "part_files": [str(notes_path)]}],
        tmp_path,
        tmp_path / "footnote_normalized",
        config=config,
    )
    assert result["valid"] is True, result["errors"]

    from pdf2epub.validation_receipts import markdown_directory_sha256
    from pdf2epub.workflow_contracts import markdown_validation_schema_version

    polished_dir = tmp_path / "polished_markdown" / "validated"
    translated_dir = tmp_path / "translated" / "validated"
    polished_dir.mkdir(parents=True)
    translated_dir.mkdir(parents=True)
    for name in ("chapter_1.md", "notes.md"):
        text = (tmp_path / "footnote_normalized" / name).read_text(encoding="utf-8")
        (polished_dir / name).write_text(text, encoding="utf-8")
        (translated_dir / name).write_text(text, encoding="utf-8")
    translated_notes = translated_dir / "notes.md"
    translated_notes.write_text(
        translated_notes.read_text(encoding="utf-8").replace(
            "First note.  <sup>70</sup> Citation continuation.",
            "Erste Notiz.  <sup>70</sup> Fortsetzung.",
        ),
        encoding="utf-8",
    )
    def write_receipt(task: str, source_dir: Path, target_dir: Path) -> None:
        (tmp_path / f"{task}_validation.json").write_text(json.dumps({
            "schema_version": markdown_validation_schema_version(task),
            "task": task, "scope": "full", "all_passed": True,
            "source_sha256": markdown_directory_sha256(source_dir),
            "target_sha256": markdown_directory_sha256(target_dir),
        }), encoding="utf-8")
    write_receipt("polish", tmp_path / "footnote_normalized", polished_dir)
    write_receipt("translate", polished_dir, translated_dir)
    translated_result = validate_centralized_endnotes_for_build(
        [{"type": "notes", "part_files": [str(translated_dir / "notes.md")] }],
        tmp_path, translated_dir, translated=True, config=config,
    )
    assert translated_result["valid"] is True, translated_result["errors"]
    translated_notes.write_text(
        translated_notes.read_text(encoding="utf-8")
        + "\n[^2]: Misplaced duplicate.  <sup>70</sup> Citation.\n",
        encoding="utf-8",
    )
    write_receipt("translate", polished_dir, translated_dir)
    moved_or_duplicate = validate_centralized_endnotes_for_build(
        [{"type": "notes", "part_files": [str(translated_notes)]}],
        tmp_path, translated_dir, translated=True, config=config,
    )
    assert moved_or_duplicate["valid"] is False


def test_prepare_apply_and_build_gate_remaps_continuation_owner_after_inline_splits(tmp_path: Path):
    from pdf2epub.build_epub import validate_centralized_endnotes_for_build

    config = _write_endnote_apply_fixture(tmp_path)
    (tmp_path / "ocr_markdown" / "chapter_1.md").write_text(
        "Body <sup>69</sup>, <sup>70</sup>, <sup>72</sup>.\n", encoding="utf-8"
    )
    note_text = (
        "[^69]: First note.  <sup>70</sup> Second note.  "
        "<sup>71</sup> continuation of 70.  <sup>72</sup> Fourth note.\n"
    )
    (tmp_path / "ocr_markdown" / "notes.md").write_text(note_text, encoding="utf-8")
    paths = prepare_footnote_subagent(tmp_path, book_title="Inline owners", config=config)
    candidates = json.loads((tmp_path / AMBIGUITIES_FILENAME).read_text(encoding="utf-8"))["items"]
    decisions = []
    for candidate in candidates:
        decisions.append({
            "file": candidate["file"], "line": candidate["line"],
            "kind": candidate["kind"], "column": candidate["column"],
            "key": candidate["key"],
            "role": {"70": "definition", "71": "continuation", "72": "definition"}[candidate["key"]],
        })
    _write_decisions(tmp_path, decisions, schema_version=2)
    assert validate_footnote_decisions(tmp_path, config=config)["valid"] is True
    assert apply_footnote_normalization(tmp_path, config=config)["valid"] is True

    notes_path = tmp_path / "footnote_normalized" / "notes.md"
    normalized = notes_path.read_text(encoding="utf-8")
    assert "[^70]: Second note.  <sup>71</sup> continuation of 70." in normalized
    result = validate_centralized_endnotes_for_build(
        [{"type": "notes", "part_files": [str(notes_path)]}],
        tmp_path,
        tmp_path / "footnote_normalized",
        config=config,
    )
    assert result["valid"] is True, result["errors"]


def test_prepare_apply_build_preserves_identical_continuations_in_repeated_scopes(tmp_path: Path):
    from pdf2epub.build_epub import validate_centralized_endnotes_for_build

    config = _write_endnote_apply_fixture(tmp_path)
    source_dir = tmp_path / "ocr_markdown"
    (source_dir / "chapter_1.md").write_text(
        "## Chapter 1\nBody <sup>1</sup>.\n", encoding="utf-8"
    )
    (source_dir / "chapter_2.md").write_text(
        "## Chapter 2\nBody <sup>1</sup>.\n", encoding="utf-8"
    )
    notes = (
        "# Notes\n## Chapter 1\n1. First.  <sup>70</sup> ibid.\n"
        "## Chapter 2\n1. First again.  <sup>70</sup> ibid.\n"
    )
    (source_dir / "notes.md").write_text(notes, encoding="utf-8")
    progress = {
        "units": [
            {"unit_id": "chapter_1", "index_path": [1], "title": "Chapter 1", "file": "chapter_1.md", "page_range": [1, 1]},
            {"unit_id": "chapter_2", "index_path": [2], "title": "Chapter 2", "file": "chapter_2.md", "page_range": [2, 2]},
            {"unit_id": "notes", "index_path": [3], "title": "Notes", "file": "notes.md", "type": "notes", "page_range": [3, 3]},
        ]
    }
    (source_dir / "tree_progress.json").write_text(json.dumps(progress), encoding="utf-8")
    toc = {"chapters": [
        {"title": "Chapter 1", "start_page": 1, "end_page": 1},
        {"title": "Chapter 2", "start_page": 2, "end_page": 2},
        {"title": "Notes", "start_page": 3, "end_page": 3, "type": "notes"},
    ]}
    (tmp_path / "toc_tree.json").write_text(json.dumps(toc), encoding="utf-8")

    paths = prepare_footnote_subagent(tmp_path, book_title="Scoped inline", config=config)
    candidates = json.loads((tmp_path / AMBIGUITIES_FILENAME).read_text(encoding="utf-8"))["items"]
    decisions = [{
        "file": item["file"], "line": item["line"], "kind": item["kind"],
        "column": item["column"], "key": item["key"], "role": "continuation",
    } for item in candidates]
    assert len(decisions) == 2
    _write_decisions(tmp_path, decisions, schema_version=2)
    assert validate_footnote_decisions(tmp_path, config=config)["valid"] is True
    applied = apply_footnote_normalization(tmp_path, config=config)
    assert applied["valid"] is True, applied["errors"]
    assert applied["endnotes"]["scope_mode"] == "per_chapter"
    notes_path = tmp_path / "footnote_normalized" / "notes.md"
    normalized_notes = notes_path.read_text(encoding="utf-8")
    assert normalized_notes.count("<sup>70</sup> ibid.") == 2
    scoped_owners = re.findall(r"^\[\^(fnc-[^]]+)\]:.*<sup>70</sup>", normalized_notes, re.M)
    assert len(scoped_owners) == 2 and len(set(scoped_owners)) == 2
    result = validate_centralized_endnotes_for_build(
        [{"type": "notes", "part_files": [str(notes_path)]}],
        tmp_path, tmp_path / "footnote_normalized", config=config,
    )
    assert result["valid"] is True, result["errors"]


def test_inline_review_source_change_after_prepare_is_rejected(tmp_path: Path):
    config, _ = _prepare_inline_fixture(
        tmp_path, "[^1]: First note.  <sup>70</sup> Citation continuation.\n"
    )
    (tmp_path / "ocr_markdown" / "notes.md").write_text(
        "[^1]: Other note.  <sup>70</sup> Citation continuation.\n",
        encoding="utf-8",
    )
    result = validate_footnote_decisions(tmp_path, config=config)
    assert result["valid"] is False
    assert any("source changed after footnote-prepare" in error for error in result["errors"])


def test_inline_manifest_missing_source_hash_is_rejected(tmp_path: Path):
    config, _ = _prepare_inline_fixture(
        tmp_path, "[^1]: First note.  <sup>70</sup> Citation continuation.\n"
    )
    manifest_path = tmp_path / AMBIGUITIES_FILENAME
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["source_sha256"] = {}
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    result = validate_footnote_decisions(tmp_path, config=config)
    assert result["valid"] is False
    assert any("source_sha256 is missing" in error for error in result["errors"])


@pytest.mark.parametrize("changed_file", [DECISIONS_FILENAME, AMBIGUITIES_FILENAME])
def test_inline_review_file_change_after_validate_blocks_apply(tmp_path: Path, changed_file: str):
    config, _ = _prepare_inline_fixture(
        tmp_path, "[^1]: First note.  <sup>70</sup> Citation continuation.\n"
    )
    assert validate_footnote_decisions(tmp_path, config=config)["valid"] is True
    path = tmp_path / changed_file
    path.write_text(path.read_text(encoding="utf-8") + " ", encoding="utf-8")
    applied = apply_footnote_normalization(tmp_path, config=config)
    assert applied["valid"] is False
    assert "changed after footnote-validate" in " ".join(applied["errors"])



def test_prepare_prompt_does_not_endnote_review_when_no_ambiguity(tmp_path: Path):
    config = _write_endnote_apply_fixture(tmp_path)
    (tmp_path / "ocr_markdown" / "notes.md").write_text(
        "1. First note.\n\n2. Second note.\n",
        encoding="utf-8",
    )

    paths = prepare_footnote_subagent(tmp_path, book_title="Clean test", config=config)
    manifest = json.loads(paths["manifest"].read_text(encoding="utf-8"))
    prompt = paths["prompt"].read_text(encoding="utf-8")

    assert manifest["endnote_ambiguity_count"] == 0
    assert manifest["status"] == "no_subagent_review_required"
    assert manifest["page_review_required"] is False
    assert manifest["decision_file"] is None
    assert "Ambiguous centralized endnote definitions" not in prompt
    assert DECISIONS_FILENAME not in prompt


def _add_page_review_block(tmp_path: Path) -> None:
    sidecar = tmp_path / "pages" / "page_001.ocr.json"
    payload = json.loads(sidecar.read_text(encoding="utf-8"))
    payload["blocks"].append(
        {
            "order": 1,
            "label": "Text",
            "bbox": [80, 820, 920, 870],
            "html": "36 Review this page footnote",
        }
    )
    sidecar.write_text(json.dumps(payload), encoding="utf-8")


def test_page_review_and_endnote_ambiguity_prompt_contains_both(tmp_path: Path):
    config = _write_endnote_apply_fixture(tmp_path)
    config["footnotes"]["mode"] = "footnotes"
    _add_page_review_block(tmp_path)

    paths = prepare_footnote_subagent(tmp_path, book_title="Mixed test", config=config)
    manifest = json.loads(paths["manifest"].read_text(encoding="utf-8"))
    prompt = paths["prompt"].read_text(encoding="utf-8")

    assert manifest["status"] == "pending_review"
    assert manifest["page_review_required"] is True
    assert manifest["decision_file"] == "footnote_decisions.json"
    assert "region_audits" in prompt
    assert "footnote_decisions.json" in prompt
    assert "Ambiguous centralized endnote definitions" in prompt
    assert DECISIONS_FILENAME in prompt
    assert "Do not create a decision file" not in prompt


def test_disabled_mode_skips_endnote_collection_and_decision_files(tmp_path: Path):
    config = _write_endnote_apply_fixture(tmp_path)
    config["footnotes"]["mode"] = "disabled"

    paths = prepare_footnote_subagent(tmp_path, book_title="Disabled test", config=config)
    manifest = json.loads(paths["manifest"].read_text(encoding="utf-8"))
    prompt = paths["prompt"].read_text(encoding="utf-8")

    assert manifest["endnote_ambiguity_count"] == 0
    assert manifest["status"] == "no_subagent_review_required"
    assert manifest["page_review_required"] is False
    assert manifest["decision_file"] is None
    assert "disabled" in prompt.lower()
    assert DECISIONS_FILENAME not in prompt
    assert "footnote_decisions.json" not in prompt


_REAL_BOOK = Path(__file__).resolve().parents[1] / "output" / "Digital Capitalism"


@pytest.mark.skipif(
    not (_REAL_BOOK / "ocr_markdown" / "tree_progress.json").is_file(),
    reason="Digital Capitalism read-only fixture is not present",
)
def test_real_book_collects_the_known_ambiguous_definition_boundaries():
    report = collect_endnote_definition_ambiguities(_REAL_BOOK)
    identities = {(item["file"], item["line"]) for item in report["items"]}

    assert ("chapter_10.part1.md", 774) in identities
    assert ("chapter_10.part3.md", 462) in identities

    progress = json.loads(
        (_REAL_BOOK / "ocr_markdown" / "tree_progress.json").read_text(
            encoding="utf-8"
        )
    )
    texts: dict[str, str] = {}
    roles: dict[str, str] = {}
    for unit in progress.get("units", []):
        for name in unit.get("part_files") or [unit.get("file")]:
            if not name:
                continue
            texts[name] = (_REAL_BOOK / "ocr_markdown" / name).read_text(
                encoding="utf-8"
            )
            roles[name] = str(unit.get("type") or "")
    decisions = [
        {"file": item["file"], "line": item["line"], "role": "continuation"}
        for item in report["items"]
    ]

    with pytest.raises(EndnoteNormalizationError) as failure:
        normalize_endnotes(texts, roles, definition_decisions=decisions)

    assert "223" in str(failure.value)
