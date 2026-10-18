import json
from pathlib import Path

import pytest

from pdf2epub.refine.footnote_apply import apply_footnote_normalization
from pdf2epub.refine.footnote_options import (
    DEFAULT_FOOTNOTE_MODE,
    FOOTNOTE_MODES,
    resolve_effective_footnote_mode,
    resolve_footnote_options,
)
from pdf2epub.refine.footnote_prepare import prepare_footnote_candidates
from pdf2epub.refine.footnote_status import footnote_normalization_status
from pdf2epub.refine.footnote_validation import validate_footnote_decisions
from pdf2epub.refine.subagent_workflow import (
    prepare_refine_subagent,
    validate_toc_tree_data,
)


def _write_toc(
    tmp_path: Path,
    mode: str | None = None,
    *,
    progress_mode: str | None = None,
) -> None:
    toc = {
        "chapters": [
            {"title": "One", "level": 1, "start_page": 1, "end_page": 1}
        ]
    }
    if mode is not None:
        toc["footnote_mode"] = mode
    (tmp_path / "toc_tree.json").write_text(json.dumps(toc), encoding="utf-8")
    if progress_mode is not None:
        progress_dir = tmp_path / "ocr_markdown"
        progress_dir.mkdir(exist_ok=True)
        (progress_dir / "tree_progress.json").write_text(
            json.dumps(
                {"units": [], "book_metadata": {"footnote_mode": progress_mode}}
            ),
            encoding="utf-8",
        )


def _write_minimal_book(
    tmp_path: Path,
    toc_mode: str | None,
    *,
    progress_mode: str | None = None,
) -> None:
    _write_toc(tmp_path, toc_mode, progress_mode=progress_mode)
    pages_dir = tmp_path / "pages"
    pages_dir.mkdir()
    (pages_dir / "page_001.ocr.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "page_number": 1,
                "backend": "test",
                "page_box": [0, 0, 1000, 1000],
                "blocks": [
                    {
                        "order": 0,
                        "label": "Footnote",
                        "bbox": [80, 820, 920, 870],
                        "html": "1 Endnote entry",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    source_dir = tmp_path / "ocr_markdown"
    source_dir.mkdir(exist_ok=True)
    (source_dir / "chapter_1.md").write_text(
        "Body [^1].\n", encoding="utf-8"
    )
    (source_dir / "chapter_12.md").write_text(
        "1. Endnote entry.\n", encoding="utf-8"
    )
    (source_dir / "tree_progress.json").write_text(
        json.dumps(
            {
                "units": [
                    {
                        "unit_id": "chapter_1",
                        "file": "chapter_1.md",
                        "page_range": [1, 1],
                        "type": "",
                    },
                    {
                        "unit_id": "chapter_12",
                        "file": "chapter_12.md",
                        "page_range": [1, 1],
                        "type": "notes",
                    },
                ],
                "book_metadata": (
                    {"footnote_mode": progress_mode}
                    if progress_mode is not None
                    else {}
                ),
            }
        ),
        encoding="utf-8",
    )


def test_resolve_footnote_options_defaults_to_auto():
    assert DEFAULT_FOOTNOTE_MODE == "auto"
    assert "auto" in FOOTNOTE_MODES
    assert resolve_footnote_options({}).mode == "auto"
    assert resolve_footnote_options(None).mode == "auto"


@pytest.mark.parametrize("mode", ["footnotes", "endnotes_only", "disabled"])
def test_resolve_footnote_options_keeps_explicit_mode(mode: str):
    assert resolve_footnote_options({"footnotes": {"mode": mode}}).mode == mode
    assert resolve_footnote_options(None, mode=mode).mode == mode


def test_resolve_footnote_options_rejects_unknown_mode():
    with pytest.raises(ValueError, match="footnotes.mode must be one of"):
        resolve_footnote_options({"footnotes": {"mode": "automatic"}})


def test_effective_mode_config_explicit_overrides_toc(tmp_path: Path):
    _write_toc(tmp_path, "endnotes_only")

    result = resolve_effective_footnote_mode(
        tmp_path, {"footnotes": {"mode": "disabled"}}
    )

    assert result == {
        "configured": "disabled",
        "effective": "disabled",
        "source": "config",
    }


def test_effective_mode_auto_uses_toc_endnotes(tmp_path: Path):
    _write_toc(tmp_path, "endnotes_only")

    result = resolve_effective_footnote_mode(
        tmp_path, {"footnotes": {"mode": "auto"}}
    )

    assert result == {
        "configured": "auto",
        "effective": "endnotes_only",
        "source": "toc",
    }


def test_effective_mode_auto_defaults_to_footnotes_without_toc_field(
    tmp_path: Path,
):
    _write_toc(tmp_path)

    result = resolve_effective_footnote_mode(
        tmp_path, {"footnotes": {"mode": "auto"}}
    )

    assert result == {
        "configured": "auto",
        "effective": "footnotes",
        "source": "default",
    }


def test_effective_mode_auto_ignores_invalid_toc_value(tmp_path: Path):
    _write_toc(tmp_path, "disabled")

    result = resolve_effective_footnote_mode(
        tmp_path, {"footnotes": {"mode": "auto"}}
    )

    assert result["effective"] == "footnotes"
    assert result["source"] == "default"


def test_effective_mode_prefers_tree_progress_metadata(tmp_path: Path):
    _write_toc(tmp_path, "endnotes_only", progress_mode="footnotes")

    result = resolve_effective_footnote_mode(
        tmp_path, {"footnotes": {"mode": "auto"}}
    )

    assert result["effective"] == "footnotes"
    assert result["source"] == "toc"


def _toc_with_mode(mode):
    return {
        "chapters": [
            {"title": "One", "level": 1, "start_page": 1, "end_page": 1}
        ],
        "footnote_mode": mode,
    }


@pytest.mark.parametrize("mode", ["footnotes", "endnotes_only", "auto"])
def test_validate_toc_tree_accepts_footnote_mode(mode: str):
    assert validate_toc_tree_data(_toc_with_mode(mode), 1, [1]) == []


@pytest.mark.parametrize("mode", ["disabled", "Automatic", "", 1, None])
def test_validate_toc_tree_rejects_invalid_footnote_mode(mode):
    errors = validate_toc_tree_data(_toc_with_mode(mode), 1, [1])
    assert any("footnote_mode" in error for error in errors)


def test_refine_prompt_instructs_footnote_mode_decision(tmp_path: Path):
    pages_dir = tmp_path / "pages"
    pages_dir.mkdir()
    (pages_dir / "page_001.md").write_text("Chapter", encoding="utf-8")

    paths = prepare_refine_subagent(tmp_path, "A Book", 8000)
    prompt = paths["prompt"].read_text(encoding="utf-8")

    assert '"footnote_mode": "footnotes"' in prompt
    assert "endnotes_only" in prompt
    assert "Never use `disabled`" in prompt
    assert "auto" in prompt


def test_auto_mode_prepares_endnotes_from_toc_decision(tmp_path: Path):
    _write_minimal_book(tmp_path, "endnotes_only")
    config = {"footnotes": {"mode": "auto"}}

    report = prepare_footnote_candidates(tmp_path, config=config)

    assert report["footnote_mode"] == "endnotes_only"
    assert report["configured_footnote_mode"] == "auto"
    assert report["footnote_mode_source"] == "toc"
    assert report["pages"] == []

    validation = validate_footnote_decisions(tmp_path, config=config)
    assert validation["valid"] is True
    assert validation["footnote_mode"] == "endnotes_only"

    result = apply_footnote_normalization(tmp_path, config=config)

    assert result["valid"] is True
    assert result["footnote_mode"] == "endnotes_only"
    assert result["configured_footnote_mode"] == "auto"
    assert (tmp_path / "footnote_normalized" / "chapter_1.md").read_text(
        encoding="utf-8"
    ) == "Body [^1].\n"
    assert (tmp_path / "footnote_normalized" / "chapter_12.md").read_text(
        encoding="utf-8"
    ) == "[^1]: Endnote entry.\n"

    status = footnote_normalization_status(tmp_path, config=config)
    assert status["current"] is True


def test_auto_mode_defaults_to_page_footnotes_without_toc_field(tmp_path: Path):
    _write_minimal_book(tmp_path, None)
    config = {"footnotes": {"mode": "auto"}}

    report = prepare_footnote_candidates(tmp_path, config=config)

    assert report["footnote_mode"] == "footnotes"
    assert report["configured_footnote_mode"] == "auto"
    assert report["footnote_mode_source"] == "default"


def test_toc_mode_change_makes_checkpoint_stale(tmp_path: Path):
    _write_minimal_book(tmp_path, "endnotes_only")
    config = {"footnotes": {"mode": "auto"}}
    prepare_footnote_candidates(tmp_path, config=config)
    assert validate_footnote_decisions(tmp_path, config=config)["valid"] is True
    assert apply_footnote_normalization(tmp_path, config=config)["valid"] is True
    assert footnote_normalization_status(tmp_path, config=config)["current"] is True

    _write_toc(tmp_path, "footnotes")

    status = footnote_normalization_status(tmp_path, config=config)
    assert status["current"] is False
    assert any(
        item["code"] == "configured_footnote_mode_mismatch"
        for item in status["failures"]
    )
    assert "footnote-prepare" in status["detail"]

    validation = validate_footnote_decisions(tmp_path, config=config)
    assert validation["valid"] is False
    assert any("requires footnotes" in error for error in validation["errors"])


def test_old_checkpoint_without_configured_mode_remains_compatible(
    tmp_path: Path,
):
    _write_minimal_book(tmp_path, "endnotes_only")
    config = {"footnotes": {"mode": "auto"}}
    report = prepare_footnote_candidates(tmp_path, config=config)
    report.pop("configured_footnote_mode", None)
    (tmp_path / "footnote_candidates.json").write_text(
        json.dumps(report), encoding="utf-8"
    )

    validation = validate_footnote_decisions(tmp_path, config=config)

    assert validation["valid"] is True
    assert validation["footnote_mode"] == "endnotes_only"
