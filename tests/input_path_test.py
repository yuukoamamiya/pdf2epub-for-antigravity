from pathlib import Path

import pytest

from pdf2epub.utils.common import resolve_book_input_path


def test_resolve_book_input_prefers_config_path_relative_to_config(tmp_path: Path):
    config_dir = tmp_path / "project"
    input_dir = config_dir / "input"
    input_dir.mkdir(parents=True)
    source = input_dir / "book.pdf"
    source.write_bytes(b"pdf")
    config = config_dir / "config.yaml"
    config.write_text("input_pdf: input/book.pdf\n", encoding="utf-8")

    result = resolve_book_input_path(config_value="input/book.pdf", config_path=config)

    assert result == source.resolve()


def test_resolve_book_input_rejects_multiple_input_files(tmp_path: Path, monkeypatch):
    input_dir = tmp_path / "input"
    input_dir.mkdir()
    (input_dir / "a.pdf").write_bytes(b"a")
    (input_dir / "b.pdf").write_bytes(b"b")
    monkeypatch.chdir(tmp_path)

    with pytest.raises(ValueError, match="Multiple input files"):
        resolve_book_input_path(extensions=("pdf",))


def test_resolve_book_input_uses_output_fallback(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    output = tmp_path / "output"
    output.mkdir()
    original = output / "input_original.pdf"
    original.write_bytes(b"pdf")

    # Explicit output fallback is useful when OCR is resumed without input/.
    result = resolve_book_input_path(output_dir=output, extensions=("pdf",), output_names=("input_original.pdf", "input.pdf"))

    assert result == original
