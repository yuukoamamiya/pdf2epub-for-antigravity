"""Coverage for optional ``base:`` configuration inheritance."""

from pathlib import Path

import pytest

from pdf2epub.pipeline_policy import PipelinePolicy
from pdf2epub.utils.configuration import load_config

BASE_TEXT = """
pipeline: translation
ocr:
  backend: chandra
footnotes:
  mode: auto
subagent:
  batching:
    max_files: 5
    max_concurrency: 3
"""


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def test_base_sections_are_inherited(tmp_path: Path):
    base = _write(tmp_path / "config.base.yaml", BASE_TEXT)
    child = _write(
        tmp_path / "book.yaml",
        f"base: {base.name}\ntitle: Book\n",
    )

    config = load_config(str(child))

    assert config["title"] == "Book"
    assert config["pipeline"] == "translation"
    assert config["ocr"] == {"backend": "chandra"}
    assert config["footnotes"] == {"mode": "auto"}
    assert "base" not in config
    assert PipelinePolicy.from_config(config).requires_translation is True


def test_override_is_shallow_and_replaces_whole_section(tmp_path: Path):
    _write(tmp_path / "config.base.yaml", BASE_TEXT)
    child = _write(
        tmp_path / "book.yaml",
        "base: config.base.yaml\n"
        "subagent:\n"
        "  models:\n"
        "    default: gemini-3.8-flash\n",
    )

    config = load_config(str(child))

    assert config["subagent"] == {"models": {"default": "gemini-3.8-flash"}}
    assert "batching" not in config["subagent"]


def test_pipeline_can_be_overridden_for_conversion(tmp_path: Path):
    _write(tmp_path / "config.base.yaml", BASE_TEXT)
    child = _write(
        tmp_path / "convert.yaml",
        "base: config.base.yaml\npipeline: epub_conversion\n",
    )

    config = load_config(str(child))

    assert config["pipeline"] == "epub_conversion"
    assert PipelinePolicy.from_config(config).is_conversion is True


def test_base_path_resolves_relative_to_declaring_file(tmp_path: Path, monkeypatch):
    book_dir = tmp_path / "books"
    _write(book_dir / "shared" / "common.yaml", BASE_TEXT)
    child = _write(
        book_dir / "book.yaml",
        "base: shared/common.yaml\ntitle: Relative\n",
    )
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)

    config = load_config(str(child))

    assert config["title"] == "Relative"
    assert config["ocr"] == {"backend": "chandra"}


def test_chained_bases_merge_in_order(tmp_path: Path):
    _write(tmp_path / "root.yaml", "pipeline: translation\n")
    _write(
        tmp_path / "middle.yaml",
        "base: root.yaml\nfootnotes:\n  mode: auto\n",
    )
    child = _write(
        tmp_path / "book.yaml",
        "base: middle.yaml\ntitle: Chained\n",
    )

    config = load_config(str(child))

    assert config["title"] == "Chained"
    assert config["pipeline"] == "translation"
    assert config["footnotes"] == {"mode": "auto"}


def test_missing_base_file_reports_path(tmp_path: Path):
    child = _write(tmp_path / "book.yaml", "base: missing.yaml\n")

    with pytest.raises(FileNotFoundError, match="missing.yaml"):
        load_config(str(child))


def test_empty_base_value_is_rejected(tmp_path: Path):
    child = _write(tmp_path / "book.yaml", "base: ''\n")

    with pytest.raises(ValueError, match="invalid `base` value"):
        load_config(str(child))


def test_base_cycle_is_rejected(tmp_path: Path):
    _write(tmp_path / "a.yaml", "base: b.yaml\n")
    _write(tmp_path / "b.yaml", "base: a.yaml\n")

    with pytest.raises(ValueError, match="cycle"):
        load_config(str(tmp_path / "a.yaml"))


def test_flat_config_without_base_still_loads(tmp_path: Path):
    config = load_config(str(_write(tmp_path / "flat.yaml", "title: Flat\n")))

    assert config == {"title": "Flat"}


def test_shipped_config_templates_load_through_base():
    root = Path(__file__).resolve().parents[1]

    pdf_config = load_config(str(root / "config.yaml.example"))
    epub_config = load_config(str(root / "config_epub.yaml.example"))

    for config in (pdf_config, epub_config):
        assert "base" not in config
        assert config["pipeline"] == "translation"
        assert config["ocr"]["backend"] == "chandra"
        assert config["footnotes"]["mode"] == "auto"
        assert config["subagent"]["batching"]["max_files"] == 5
    assert pdf_config["input_pdf"].endswith(".pdf")
    assert epub_config["input_epub"].endswith(".epub")
