import hashlib
import json
from pathlib import Path

import pytest

from pdf2epub.refine.endnote_normalization import (
    EndnoteNormalizationError,
    normalize_endnotes,
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
    assert report["schema_version"] == 1
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


def _write_decisions(tmp_path: Path, decisions: list[dict], schema_version: int = 1):
    (tmp_path / DECISIONS_FILENAME).write_text(
        json.dumps({"schema_version": schema_version, "decisions": decisions}),
        encoding="utf-8",
    )


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
    assert DECISIONS_FILENAME in prompt
    assert AMBIGUITIES_FILENAME in prompt

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
    assert "Ambiguous centralized endnote definitions" not in prompt


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
