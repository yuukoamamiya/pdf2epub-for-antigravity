import json
from pathlib import Path

import pytest

from pdf2epub.build_epub import BuildEpubConfig, build_epub
from pdf2epub.toc_model import TOC_TREE_SCHEMA_VERSION


def _write_tree(output: Path, entries: list[dict]) -> Path:
    toc = output / "toc_tree.json"
    toc.write_text(json.dumps({
        "schema_version": TOC_TREE_SCHEMA_VERSION,
        "book_title": "Scope Test",
        "chapters": entries,
    }), encoding="utf-8")
    return toc


def _unit(unit_id: str, file_path: Path, *, entry_type: str | None = None) -> dict:
    entry = {
        "node_id": unit_id,
        "unit_id": unit_id,
        "title": unit_id,
        "level": 1,
        "binding_mode": "container_only",
        "file_path": file_path.name,
        "part_files": [file_path.name],
        "children": [],
    }
    if entry_type:
        entry["type"] = entry_type
    return entry


def _build(tmp_path: Path, *, broken: bool = False) -> tuple[Path, Path]:
    output = tmp_path / "output"
    markdown = output / "polished_markdown"
    source = output / "ocr_markdown"
    markdown.mkdir(parents=True)
    source.mkdir()
    first = markdown / "chapter_1.md"
    second = markdown / "chapter_2.md"
    notes = markdown / "chapter_3.md"
    first_text = "First [^1]. Again [^1].\n\n[^1]: Article A.\n"
    second_text = "Missing [^3].\n" if broken else "Central [^2].\n"
    notes_text = "[^2]: Central note.\n"
    for directory in (markdown, source):
        (directory / first.name).write_text(first_text, encoding="utf-8")
        (directory / second.name).write_text(second_text, encoding="utf-8")
        (directory / notes.name).write_text(notes_text, encoding="utf-8")
    (source / "tree_progress.json").write_text(json.dumps({"units": [
        {"node_id": f"chapter_{i}", "index_path": [i], "file": f"chapter_{i}.md"}
        for i in (1, 2, 3)
    ]}), encoding="utf-8")
    toc = _write_tree(output, [
        _unit("chapter_1", first),
        _unit("chapter_2", second),
        _unit("chapter_3", notes, entry_type="notes"),
    ])
    return output, toc


def test_build_links_local_repeated_refs_and_cross_file_central_note(tmp_path: Path) -> None:
    output, toc = _build(tmp_path)
    epub = build_epub(BuildEpubConfig(
        book_title="Scope Test", output_dir=output,
        markdown_dir=output / "polished_markdown", toc_tree_path=toc,
        cover_image=output / "no-cover.jpg",
        config={"footnotes": {"mode": "footnotes"}},
    ))

    assert epub.is_file()
    rendered = output / "epub_build" / "text"
    chapter1 = (rendered / "chapter_1.html").read_text(encoding="utf-8")
    chapter2 = (rendered / "chapter_2.html").read_text(encoding="utf-8")
    notes = (rendered / "chapter_3.html").read_text(encoding="utf-8")
    assert 'href="chapter_1.html#fn-1-1"' in chapter1
    assert chapter1.count('href="chapter_1.html#fn-1-1"') >= 2
    assert 'href="chapter_3.html#fn-2-1"' in chapter2
    assert 'href="chapter_2.html#fnref-chapter_2-2"' in notes


def test_build_blocks_missing_footnote_even_in_mixed_footnotes_mode(tmp_path: Path) -> None:
    output, toc = _build(tmp_path, broken=True)
    with pytest.raises(ValueError, match="Footnote graph contains unlinked references"):
        build_epub(BuildEpubConfig(
            book_title="Scope Test", output_dir=output,
            markdown_dir=output / "polished_markdown", toc_tree_path=toc,
            cover_image=output / "no-cover.jpg",
            config={"footnotes": {"mode": "footnotes"}},
        ))


def test_build_blocks_markdown_footnote_reference_without_any_definition(tmp_path: Path) -> None:
    output = tmp_path / "output"
    markdown = output / "polished_markdown"
    source = output / "ocr_markdown"
    markdown.mkdir(parents=True)
    source.mkdir()
    chapter = markdown / "chapter_1.md"
    chapter.write_text("Explicit missing [^missing].\n", encoding="utf-8")
    (source / chapter.name).write_text(chapter.read_text(encoding="utf-8"), encoding="utf-8")
    (source / "tree_progress.json").write_text(json.dumps({"units": [{
        "node_id": "chapter_1", "index_path": [1], "file": chapter.name,
    }]}), encoding="utf-8")
    toc = _write_tree(output, [_unit("chapter_1", chapter)])

    with pytest.raises(ValueError, match="Footnote graph contains unlinked references"):
        build_epub(BuildEpubConfig(
            book_title="Scope Test", output_dir=output, markdown_dir=markdown,
            toc_tree_path=toc, cover_image=output / "no-cover.jpg",
            config={"footnotes": {"mode": "footnotes"}},
        ))


def test_build_blocks_excess_local_references(tmp_path: Path) -> None:
    output = tmp_path / "output"
    markdown = output / "polished_markdown"
    source = output / "ocr_markdown"
    markdown.mkdir(parents=True)
    source.mkdir()
    chapter = markdown / "chapter_1.md"
    content = "One [^1]. Two [^1]. Three [^1].\n\n[^1]: First.\n\n[^1]: Second.\n"
    chapter.write_text(content, encoding="utf-8")
    (source / chapter.name).write_text(content, encoding="utf-8")
    (source / "tree_progress.json").write_text(json.dumps({"units": [{
        "node_id": "chapter_1", "index_path": [1], "file": chapter.name,
    }]}), encoding="utf-8")
    toc = _write_tree(output, [_unit("chapter_1", chapter)])

    with pytest.raises(ValueError, match="Footnote graph contains unlinked references"):
        build_epub(BuildEpubConfig(
            book_title="Scope Test", output_dir=output, markdown_dir=markdown,
            toc_tree_path=toc, cover_image=output / "no-cover.jpg",
            config={"footnotes": {"mode": "footnotes"}},
        ))
