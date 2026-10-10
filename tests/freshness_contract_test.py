import hashlib
import json
from pathlib import Path

import pytest

from pdf2epub.html_translation import builder as html_builder
from pdf2epub.html_translation.builder import HTMLEpubPipeline
from pdf2epub.commands.novel import _novel_child_dir
from pdf2epub.commands.entities import _entity_context_is_current
from pdf2epub.entity_extractor import create_entity_template
from pdf2epub.ocr_consensus import validate_ocr_config
from pdf2epub.tex_translation.arxiv import normalize_arxiv_id
from pdf2epub.tex_translation.document import (
    TexProjectDocument,
    TranslationUnit,
    inject_cjk_support,
    tex_structure_tokens,
)


def _html_contract(tmp_path: Path, source: str, target: str) -> HTMLEpubPipeline:
    pipeline = object.__new__(HTMLEpubPipeline)
    pipeline.output_dir = tmp_path
    pipeline.compressed_units_dir = tmp_path / "compressed_units"
    pipeline.translated_dir = tmp_path / "translated_compressed"
    pipeline.config = {"translation": {"target_language": "Chinese"}}
    pipeline.compressed_units_dir.mkdir()
    pipeline.translated_dir.mkdir()
    source_path = pipeline.compressed_units_dir / "chapter.md"
    source_path.write_text(source, encoding="utf-8")
    (pipeline.compressed_units_dir / "chapter.mapping.json").write_text(
        "{}", encoding="utf-8"
    )
    (pipeline.translated_dir / "chapter.md").write_text(target, encoding="utf-8")
    input_epub = tmp_path / "input.epub"
    input_epub.write_bytes(b"epub snapshot")
    (tmp_path / "translate-html_subagent_manifest.json").write_text(
        json.dumps(
            {
                "files": ["chapter.md"],
                "source_sha256": {
                    "chapter.md": hashlib.sha256(source.encode("utf-8")).hexdigest()
                },
                "input_epub_sha256": hashlib.sha256(input_epub.read_bytes()).hexdigest(),
            }
        ),
        encoding="utf-8",
    )
    return pipeline


def test_html_validation_blocks_long_source_left_in_english(tmp_path: Path):
    source = "This is a long source paragraph about translation quality. " * 8
    pipeline = _html_contract(tmp_path, source, source)

    report = pipeline.validate_translated_units(file_name="chapter.md")

    assert report["all_passed"] is False
    assert report["target_language_blocked"] == ["chapter.md"]
    assert any("untranslated_source_detected" in item["reason"] for item in report["invalid"])


def test_html_validation_rejects_changed_source_and_input_snapshot(tmp_path: Path):
    pipeline = _html_contract(tmp_path, "Original source paragraph.\n", "译文段落。\n")
    (pipeline.compressed_units_dir / "chapter.md").write_text(
        "Changed source paragraph.\n", encoding="utf-8"
    )
    (tmp_path / "input.epub").write_bytes(b"changed epub snapshot")

    report = pipeline.validate_translated_units(file_name="chapter.md")

    assert report["source_snapshot"]["valid"] is False
    assert "input EPUB changed after html-prepare" in report["source_snapshot"]["errors"]
    assert any(item["reason"] == "source changed after html-prepare" for item in report["invalid"])


def test_tex_render_refuses_missing_translation_instead_of_using_source():
    unit = TranslationUnit(
        id="unit-00001",
        relative_path="main.tex",
        start=0,
        end=5,
        source_sha256="source",
        source_text="Hello",
    )
    document = TexProjectDocument(
        root=Path("."),
        main_tex="main.tex",
        sources={"main.tex": "Hello"},
        units=(unit,),
        source_fingerprint="source",
        layout_fingerprint="layout",
    )

    with pytest.raises(ValueError, match="missing translations"):
        document.render({})


def test_tex_structure_tokens_preserve_references_environments_and_math():
    source = r"\begin{equation}\label{eq:one} x = 1 \end{equation} See \ref{eq:one} $x$ \cite{paper}."
    target = r"\begin{equation}\label{eq:one} x = 2 \end{equation} 参见 \ref{eq:one} $x$ \cite{paper}."
    changed = r"\begin{equation}\label{eq:two} x = 2 \end{equation} 参见 \ref{eq:one} $x$ \cite{paper}."

    assert tex_structure_tokens(source) == tex_structure_tokens(target)
    assert tex_structure_tokens(source) != tex_structure_tokens(changed)


def test_tex_xelatex_normalization_disables_microtype_setup_commands():
    source = (
        "\\documentclass{article}\n"
        "\\usepackage{microtype}\n"
        "\\UseMicrotypeSet[protrusion]{basicmath}\n"
        "\\begin{document}Body\\end{document}\n"
    )

    prepared = inject_cjk_support(source)

    assert "\\usepackage{microtype}" not in prepared
    assert "\\UseMicrotypeSet" not in prepared
    assert "together with the microtype package" in prepared


def test_enabled_secondary_ocr_requires_a_backend():
    with pytest.raises(ValueError, match="backend is missing"):
        validate_ocr_config({"ocr": {"secondary": {"enabled": True}}})


def test_unknown_ocr_backend_is_rejected_before_workflow_start():
    with pytest.raises(ValueError, match="ocr.backend"):
        validate_ocr_config({"ocr": {"backend": "mistral"}})


def test_unknown_secondary_backend_is_rejected_before_workflow_start():
    with pytest.raises(ValueError, match="ocr.secondary.backend"):
        validate_ocr_config(
            {
                "ocr": {
                    "backend": "chandra",
                    "secondary": {"enabled": True, "backend": "mistral"},
                }
            }
        )


def test_retired_backend_configuration_is_rejected_even_when_disabled():
    with pytest.raises(ValueError, match="retired OCR backend"):
        validate_ocr_config(
            {
                "ocr": {
                    "backend": "chandra",
                    "secondary": {"enabled": False, "backend": "vision"},
                    "backends": {"mistral": {"model": "legacy"}},
                }
            }
        )


def test_enabled_layout_detection_is_rejected_after_removal():
    with pytest.raises(ValueError, match="no longer supported"):
        validate_ocr_config({"ocr": {"layout": {"enabled": True}}})


def test_retired_secondary_paddle_is_rejected_even_when_disabled():
    with pytest.raises(ValueError, match="retired OCR backend"):
        validate_ocr_config(
            {
                "ocr": {
                    "backend": "chandra",
                    "secondary": {"enabled": False, "backend": "paddle"},
                }
            }
        )


def test_retired_paddle_backend_block_is_rejected_even_when_unused():
    with pytest.raises(ValueError, match="retired OCR backend"):
        validate_ocr_config(
            {
                "ocr": {
                    "backend": "chandra",
                    "backends": {"paddle": {"device": "gpu:0"}},
                }
            }
        )


def test_layout_block_is_rejected_even_when_disabled():
    with pytest.raises(ValueError, match="remove the layout block"):
        validate_ocr_config({"ocr": {"layout": {"enabled": False}}})


def test_arxiv_url_normalization_allows_only_https_arxiv_hosts():
    assert normalize_arxiv_id("https://arxiv.org/abs/2301.12345v2") == "2301.12345v2"
    with pytest.raises(ValueError, match="HTTPS URLs hosted by arxiv.org"):
        normalize_arxiv_id("https://example.com/abs/2301.12345")


def test_entity_checkpoint_rejects_a_new_source_unit(tmp_path: Path):
    source_dir = tmp_path / "compressed_units"
    source_dir.mkdir()
    source = source_dir / "chapter.md"
    source.write_text("Source", encoding="utf-8")
    (tmp_path / "translation_entities.json").write_text(
        json.dumps(
            {
                **create_entity_template("Book", "English", "Chinese", [source.name]),
                "metadata": {
                    "book_title": "Book",
                    "source_language": "English",
                    "target_language": "Chinese",
                    "source_files": [source.name],
                    "extraction_complete": True,
                },
            }
        ),
        encoding="utf-8",
    )
    (tmp_path / "entity_subagent_manifest.json").write_text(
        json.dumps(
            {
                "source_dir": "compressed_units",
                "source_sha256": {
                    source.name: hashlib.sha256(source.read_bytes()).hexdigest()
                },
            }
        ),
        encoding="utf-8",
    )

    assert _entity_context_is_current(
        tmp_path, source_dir, "English", "Chinese"
    ) is True
    (source_dir / "new.md").write_text("New source", encoding="utf-8")
    assert _entity_context_is_current(
        tmp_path, source_dir, "English", "Chinese"
    ) is False


def test_novel_manifest_directory_cannot_escape_output(tmp_path: Path):
    with pytest.raises(ValueError, match="escapes"):
        _novel_child_dir(tmp_path, "../outside", "source_dir")


def test_html_build_clears_stale_final_xhtml(tmp_path: Path, monkeypatch):
    pipeline = object.__new__(HTMLEpubPipeline)
    pipeline.output_dir = tmp_path
    pipeline.translated_dir = tmp_path / "translated_compressed"
    pipeline.compressed_units_dir = tmp_path / "compressed_units"
    pipeline.final_dir = tmp_path / "final_xhtml"
    pipeline.epub_path = tmp_path / "input.epub"
    pipeline.book_title = "Book"
    pipeline.config = {}
    pipeline.navigation_report = {}
    pipeline.final_dir.mkdir()
    stale = pipeline.final_dir / "old.xhtml"
    stale.write_text("stale", encoding="utf-8")
    pipeline.validate_translated_units = lambda: {"all_passed": True}
    pipeline.write_translation_report = lambda **_kwargs: tmp_path / "report.json"
    pipeline._merge_part_files = lambda: {}
    pipeline._logical_unit_records = lambda _merged: []
    output_epub = tmp_path / "built.epub"

    monkeypatch.setattr(
        html_builder,
        "build_html_epub",
        lambda **kwargs: output_epub,
    )
    assert pipeline.postprocess_and_build(output_epub) == output_epub
    assert not stale.exists()
