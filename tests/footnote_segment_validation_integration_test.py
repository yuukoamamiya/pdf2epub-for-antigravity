from __future__ import annotations

import json
import re
import runpy
from pathlib import Path

import pytest

from pdf2epub.refine.footnote_apply import apply_footnote_normalization
from pdf2epub.refine.footnote_prepare import prepare_footnote_candidates
from pdf2epub.refine.footnote_validation import validate_footnote_decisions


@pytest.mark.parametrize(
    ("marker_markup", "normalized_marker"),
    [("<sup>17</sup>", "[^17]"), ("<sup>17)</sup>", "[^17])")],
)
def test_long_mixed_block_segment_validates_and_applies_through_public_entry(
    tmp_path, monkeypatch, marker_markup, normalized_marker
):
    helper = runpy.run_path(str(Path(__file__).with_name("footnote_result_recovery_test.py")))
    helper["_write_recovery_book"](tmp_path, monkeypatch)
    body = "Body prose remains ordinary. " * 12
    note = "17. A long footnote definition recovered from the page image."
    block_text = body + note

    source_path = tmp_path / "ocr_markdown" / "chapter_1.md"
    source_path.write_text(
        "The source calls it " + marker_markup + ".\n\n" + block_text + "\n",
        encoding="utf-8",
    )
    sidecar_path = tmp_path / "pages" / "page_001.ocr.json"
    sidecar = json.loads(sidecar_path.read_text(encoding="utf-8"))
    sidecar["blocks"] = [{
        "order": 0,
        "label": "Footnote",
        "bbox": [80, 820, 920, 870],
        "text": block_text,
    }]
    sidecar_path.write_text(json.dumps(sidecar), encoding="utf-8")

    report = prepare_footnote_candidates(tmp_path)
    candidate = report["pages"][0]["candidates"][0]
    assert len(candidate["text"]) == 240
    assert "17. A long footnote" not in candidate["text"]

    (tmp_path / "footnote_decisions.json").write_text(json.dumps({
        "schema_version": report["schema_version"],
        "decisions": [{
            "page": 1,
            "source": "primary",
            "block": 0,
            "role": "body",
            "override": True,
            "segments": [
                {"text": body, "role": "body"},
                {"text": note, "role": "footnote_start", "key": "17"},
            ],
        }],
    }), encoding="utf-8")

    validation = validate_footnote_decisions(tmp_path)
    assert validation["valid"] is True, validation.get("errors")
    applied = apply_footnote_normalization(tmp_path)
    assert applied["valid"] is True, applied.get("errors")
    normalized = (tmp_path / "footnote_normalized" / "chapter_1.md").read_text(encoding="utf-8")
    assert body.rstrip() in normalized
    assert "The source calls it " + normalized_marker + "." in normalized
    assert "[^17]: A long footnote definition recovered from the page image." in normalized


def test_long_multi_note_mixed_block_segments_validate_and_apply_in_order(tmp_path, monkeypatch):
    helper = runpy.run_path(str(Path(__file__).with_name("footnote_result_recovery_test.py")))
    helper["_write_recovery_book"](tmp_path, monkeypatch)
    body_before = "Body paragraph before note 17. " * 10
    note17 = "17. First note definition from the page image."
    body_between = "Body paragraph between both note definitions. " * 3
    note18 = "18. Second note definition from the page image."
    block_text = body_before + note17 + body_between + note18

    source_path = tmp_path / "ocr_markdown" / "chapter_1.md"
    source_path.write_text(
        "First reference <sup>17</sup>; second reference <sup>18</sup>.\n\n"
        + block_text + "\n",
        encoding="utf-8",
    )
    sidecar_path = tmp_path / "pages" / "page_001.ocr.json"
    sidecar = json.loads(sidecar_path.read_text(encoding="utf-8"))
    sidecar["blocks"] = [{
        "order": 0,
        "label": "Footnote",
        "bbox": [80, 820, 920, 870],
        "text": block_text,
    }]
    sidecar_path.write_text(json.dumps(sidecar), encoding="utf-8")

    report = prepare_footnote_candidates(tmp_path)
    candidate = report["pages"][0]["candidates"][0]
    assert len(candidate["text"]) == 240
    assert "17. First note" not in candidate["text"]
    (tmp_path / "footnote_decisions.json").write_text(json.dumps({
        "schema_version": report["schema_version"],
        "decisions": [{
            "page": 1,
            "source": "primary",
            "block": 0,
            "role": "body",
            "override": True,
            "segments": [
                {"text": body_before, "role": "body"},
                {"text": note17, "role": "footnote_start", "key": "17"},
                {"text": body_between, "role": "body"},
                {"text": note18, "role": "footnote_start", "key": "18"},
            ],
        }],
    }), encoding="utf-8")

    validation = validate_footnote_decisions(tmp_path)
    assert validation["valid"] is True, validation.get("errors")
    applied = apply_footnote_normalization(tmp_path)
    assert applied["valid"] is True, applied.get("errors")
    normalized = (tmp_path / "footnote_normalized" / "chapter_1.md").read_text(encoding="utf-8")
    assert body_before.rstrip() in normalized
    assert body_between.rstrip() in normalized
    assert "[^17]: First note definition from the page image." in normalized
    assert "[^18]: Second note definition from the page image." in normalized
    assert normalized.index(body_before.rstrip()) < normalized.index(body_between.rstrip())


def test_page_two_mixed_continuation_and_new_note_keep_keys_separate(tmp_path, monkeypatch):
    helper = runpy.run_path(str(Path(__file__).with_name("footnote_result_recovery_test.py")))
    helper["_write_recovery_book"](tmp_path, monkeypatch)
    progress_path = tmp_path / "ocr_markdown" / "tree_progress.json"
    progress = json.loads(progress_path.read_text(encoding="utf-8"))
    progress["units"][0]["page_range"] = [1, 2]
    progress_path.write_text(json.dumps(progress), encoding="utf-8")
    page1_note = "17. The opening note text continues from"
    page2_body = "Second-page body paragraph before the continuation. "
    continuation = "the previous page and ends here."
    page2_body_after = " Body after note 17 references <sup>18</sup>. "
    note18 = "18. New note 18 starts on the second page."
    mixed_page2 = page2_body + continuation + page2_body_after + note18
    source_path = tmp_path / "ocr_markdown" / "chapter_1.md"
    source_path.write_text(
        "First-page reference <sup>17</sup>.\n\n" + page1_note + "\n\n"
        + mixed_page2 + "\n",
        encoding="utf-8",
    )

    page1_path = tmp_path / "pages" / "page_001.ocr.json"
    page1 = json.loads(page1_path.read_text(encoding="utf-8"))
    page1["blocks"] = [{
        "order": 0, "label": "Footnote", "bbox": [80, 820, 920, 870], "text": page1_note,
    }]
    page1_path.write_text(json.dumps(page1), encoding="utf-8")
    page2_path = tmp_path / "pages" / "page_002.ocr.json"
    page2 = {
        "schema_version": 1,
        "page_number": 2,
        "backend": "synthetic",
        "page_box": [0, 0, 1000, 1000],
        "blocks": [{
            "order": 0, "label": "Footnote", "bbox": [80, 820, 920, 870], "text": mixed_page2,
        }],
    }
    page2_path.write_text(json.dumps(page2), encoding="utf-8")
    (tmp_path / "pages" / "page_002.md").write_text(mixed_page2 + "\n", encoding="utf-8")

    report = prepare_footnote_candidates(tmp_path)
    page1_candidate = next(candidate for row in report["pages"] if row["page"] == 1 for candidate in row["candidates"])
    page2_candidate = next(candidate for row in report["pages"] if row["page"] == 2 for candidate in row["candidates"])
    (tmp_path / "footnote_decisions.json").write_text(json.dumps({
        "schema_version": report["schema_version"],
        "decisions": [
            {"page": 1, "source": "primary", "block": page1_candidate["block"], "role": "footnote_start", "key": "17"},
            {
                "page": 2,
                "source": "primary",
                "block": page2_candidate["block"],
                "role": "body",
                "override": True,
                "segments": [
                    {"text": page2_body, "role": "body"},
                    {"text": continuation, "role": "footnote_continuation", "key": "17"},
                    {"text": " Body after note 17 references 18 .", "role": "body"},
                    {"text": note18, "role": "footnote_start", "key": "18"},
                ],
            },
        ],
    }), encoding="utf-8")

    validation = validate_footnote_decisions(tmp_path)
    assert validation["valid"] is True, validation.get("errors")
    applied = apply_footnote_normalization(tmp_path)
    assert applied["valid"] is True, applied.get("errors")
    normalized = (tmp_path / "footnote_normalized" / "chapter_1.md").read_text(encoding="utf-8")
    assert re.search(
        r"\[\^17\]: The opening note text continues from\s+the previous page and ends here\.",
        normalized,
    )
    assert "[^18]: New note 18 starts on the second page." in normalized
    assert "Second-page body paragraph before the continuation." in normalized
    assert re.search(r"Body after note 17 references\s*\[\^18\]\.", normalized)
