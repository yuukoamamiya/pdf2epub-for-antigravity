from __future__ import annotations

import zipfile
import json
from pathlib import Path

import pytest

from pdf2epub.html_translation.epub_parser import EPUBParser
from pdf2epub.html_translation.builder import HTMLEpubPipeline, _package_relative_href
from pdf2epub.html_translation.novel_extractor import NovelExtractor


def _write_epub(
    path: Path,
    *,
    href: str,
    members: dict[str, bytes] | None = None,
    manifest_extra: str = "",
) -> None:
    opf = f'''<?xml version="1.0" encoding="UTF-8"?>
<package xmlns="http://www.idpf.org/2007/opf"
         xmlns:dc="http://purl.org/dc/elements/1.1/" version="3.0">
  <metadata><dc:title>Path fixture</dc:title><dc:language>en</dc:language></metadata>
  <manifest><item id="chapter" href="{href}" media-type="application/xhtml+xml"/>{manifest_extra}</manifest>
  <spine><itemref idref="chapter"/></spine>
</package>'''.encode()
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(
            "META-INF/container.xml",
            b'''<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
<rootfiles><rootfile full-path="OEBPS/content.opf"/></rootfiles></container>''',
        )
        archive.writestr("OEBPS/content.opf", opf)
        for name, content in (members or {}).items():
            archive.writestr(name, content)


def test_manifest_uri_encoded_space_resolves_to_zip_member(tmp_path: Path) -> None:
    epub = tmp_path / "encoded.epub"
    body = b"<html><body><p>Chapter body</p></body></html>"
    _write_epub(epub, href="Text/chapter%201.xhtml", members={"OEBPS/Text/chapter 1.xhtml": body})

    parser = EPUBParser(str(epub))
    try:
        assert parser.spine[0]["href"] == "Text/chapter%201.xhtml"
        assert parser.spine[0]["full_href"] == "OEBPS/Text/chapter 1.xhtml"
        assert parser.get_raw_content(parser.spine[0]["href"]) == body
    finally:
        parser._zf.close()


def test_uri_escape_is_decoded_once_for_literal_percent_filename(tmp_path: Path) -> None:
    epub = tmp_path / "literal-percent.epub"
    body = b"<html><body><p>Literal percent path</p></body></html>"
    _write_epub(
        epub,
        href="Text/literal%2520.xhtml",
        members={"OEBPS/Text/literal%20.xhtml": body},
    )
    parser = EPUBParser(str(epub))
    try:
        assert parser.spine[0]["full_href"] == "OEBPS/Text/literal%20.xhtml"
        assert parser.get_raw_content(parser.spine[0]["href"]) == body
    finally:
        parser._zf.close()


def test_navigation_uri_path_is_decoded_and_fragment_is_preserved(tmp_path: Path) -> None:
    extract_dir = tmp_path / "extract"
    text_dir = extract_dir / "OEBPS" / "Text"
    text_dir.mkdir(parents=True)
    assert _package_relative_href(
        extract_dir, text_dir, "chapter%201.xhtml#section-1"
    ) == "OEBPS/Text/chapter 1.xhtml#section-1"


def test_dot_segments_are_allowed_when_they_stay_inside_package(tmp_path: Path) -> None:
    epub = tmp_path / "relative.epub"
    body = b"<html><body><p>Chapter body</p></body></html>"
    _write_epub(
        epub,
        href="../Text/chapter.xhtml",
        members={"Text/chapter.xhtml": body},
    )
    parser = EPUBParser(str(epub))
    try:
        assert parser.get_raw_content(parser.spine[0]["href"]) == body
    finally:
        parser._zf.close()


def test_duplicate_basename_is_not_guessed_from_another_directory(tmp_path: Path) -> None:
    epub = tmp_path / "duplicate-basename.epub"
    _write_epub(
        epub,
        href="Missing/chapter.xhtml",
        members={
            "OEBPS/Text/chapter.xhtml": b"wrong location",
            "OEBPS/Other/chapter.xhtml": b"also wrong location",
        },
    )
    parser = EPUBParser(str(epub))
    try:
        assert parser.get_raw_content(parser.spine[0]["href"]) is None
    finally:
        parser._zf.close()


def test_duplicate_exact_zip_member_is_rejected_as_ambiguous(tmp_path: Path) -> None:
    epub = tmp_path / "duplicate-member.epub"
    _write_epub(epub, href="Text/chapter.xhtml")
    with pytest.warns(UserWarning, match="Duplicate name"):
        with zipfile.ZipFile(epub, "a") as archive:
            archive.writestr("OEBPS/Text/chapter.xhtml", b"first")
            archive.writestr("OEBPS/Text/chapter.xhtml", b"second")
    parser = EPUBParser(str(epub))
    try:
        assert parser.get_raw_content(parser.spine[0]["href"]) is None
    finally:
        parser._zf.close()


def test_remote_non_spine_resource_is_skipped_without_fetching(tmp_path: Path) -> None:
    epub = tmp_path / "remote-resource.epub"
    body = b"<html><body><p>Local chapter</p></body></html>"
    _write_epub(
        epub,
        href="Text/chapter.xhtml",
        members={"OEBPS/Text/chapter.xhtml": body},
        manifest_extra=(
            '<item id="audio" href="https://media.example/audio.mp3" '
            'media-type="audio/mpeg"/>'
        ),
    )
    parser = EPUBParser(str(epub))
    try:
        assert parser.get_raw_content(parser.spine[0]["href"]) == body
        assert parser.resources["other"] == []
    finally:
        parser._zf.close()


def test_extract_and_preprocess_reads_encoded_spine_uri_end_to_end(tmp_path: Path) -> None:
    epub = tmp_path / "encoded-extract.epub"
    body = b"<html><body><p>A chapter with encoded-space URI.</p></body></html>"
    _write_epub(
        epub,
        href="Text/chapter%201.xhtml",
        members={"OEBPS/Text/chapter 1.xhtml": body},
    )
    pipeline = HTMLEpubPipeline(epub, tmp_path / "work", {})
    try:
        assert pipeline.extract_and_preprocess(target_language="Chinese") == 1
        mapping_path = next(pipeline.compressed_units_dir.glob("*.mapping.json"))
        mapping = json.loads(mapping_path.read_text(encoding="utf-8"))
        unit_stem = mapping_path.name[:-len(".mapping.json")]
        assert mapping["package_relative_path"] == "OEBPS/Text/chapter 1.xhtml"
        assert (pipeline.compressed_units_dir / f"{unit_stem}.md").is_file()
    finally:
        pipeline.parser._zf.close()


def test_novel_extractor_reads_encoded_uri_with_real_parser(tmp_path: Path) -> None:
    epub = tmp_path / "novel-encoded.epub"
    body = "<html><body><p>これは本文です。</p></body></html>".encode()
    _write_epub(
        epub,
        href="Text/chapter%201.xhtml",
        members={"OEBPS/Text/chapter 1.xhtml": body},
    )
    parser = EPUBParser(str(epub))
    try:
        units = NovelExtractor(parser).extract_all(tmp_path / "novel-text")
        assert len(units) == 1
        assert units[0].source_href == "Text/chapter%201.xhtml"
        assert units[0].text_path is not None
        assert "これは本文です。" in units[0].text_path.read_text(encoding="utf-8")
    finally:
        parser._zf.close()


def test_missing_spine_document_fails_extraction_without_success_cache(tmp_path: Path) -> None:
    epub = tmp_path / "missing.epub"
    _write_epub(epub, href="Text/missing.xhtml")
    pipeline = HTMLEpubPipeline(epub, tmp_path / "work", {})
    try:
        with pytest.raises(FileNotFoundError, match="Spine document"):
            pipeline.extract_and_preprocess(target_language="Chinese")
        assert list(pipeline.compressed_units_dir.iterdir()) == []
        assert not pipeline._compressed_cache_path().exists()
    finally:
        pipeline.parser._zf.close()


@pytest.mark.parametrize("href", ["../../outside.xhtml", "/absolute.xhtml", "https://example.test/chapter.xhtml"])
def test_manifest_rejects_external_or_escaping_paths(tmp_path: Path, href: str) -> None:
    epub = tmp_path / "unsafe.epub"
    _write_epub(epub, href=href)
    if href.startswith("https:"):
        parser = EPUBParser(str(epub))
        try:
            parser.spine
        except ValueError:
            pass
        else:
            pytest.fail("remote spine document should be rejected")
        finally:
            parser._zf.close()
    else:
        with pytest.raises(ValueError):
            EPUBParser(str(epub))
