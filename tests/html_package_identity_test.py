from __future__ import annotations

import json
import hashlib
import zipfile
from pathlib import Path

import pytest

from pdf2epub.commands.html import _load_html_chapter_groups
from pdf2epub.html_translation.builder import HTMLEpubPipeline
from pdf2epub.html_translation.package_identity import unit_id_for_package_path
from pdf2epub.html_translation.package_resources import EPUBFilePackager, extract_epub
from pdf2epub.html_translation.unit_files import HTMLUnitStore, HTMLUnitWorkspace
from pdf2epub.html_translation.compressed_cache import CompressedExtractionCache


def _write_identity_epub(path: Path) -> dict[str, str]:
    documents = {
        "OEBPS/A/chapter.xhtml": "<html><head><title>A</title></head><body><p>Original A</p><a href='../B/chapter.xhtml#target'>Go B</a></body></html>",
        "OEBPS/B/chapter.xhtml": "<html><head><title>B</title></head><body><p id='target'>Original B</p></body></html>",
        "OEBPS/A/chapter.html": "<html><body><p>Original HTML extension</p></body></html>",
        "OEBPS/Text/space name.xhtml": "<html><body><p>Original encoded space</p></body></html>",
        "OEBPS/Text/literal%20.xhtml": "<html><body><p>Original literal percent</p></body></html>",
    }
    resources = {
        "OEBPS/A/shared.css": b"/* A stylesheet */",
        "OEBPS/B/shared.css": b"/* B stylesheet */",
    }
    hrefs = {
        "OEBPS/A/chapter.xhtml": ("A/chapter.xhtml", "application/xhtml+xml"),
        "OEBPS/B/chapter.xhtml": ("B/chapter.xhtml", "application/xhtml+xml"),
        "OEBPS/A/chapter.html": ("A/chapter.html", "text/html"),
        "OEBPS/Text/space name.xhtml": ("Text/space%20name.xhtml", "application/xhtml+xml"),
        "OEBPS/Text/literal%20.xhtml": ("Text/literal%2520.xhtml", "application/xhtml+xml"),
    }
    manifest = [
        '<item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>',
        '<item id="ncx" href="Navigation/toc.ncx" media-type="application/x-dtbncx+xml"/>',
    ]
    spine = []
    for index, (member, (href, media_type)) in enumerate(hrefs.items(), 1):
        manifest.append(
            f'<item id="doc{index}" href="{href}" media-type="{media_type}"/>'
        )
        spine.append(f'<itemref idref="doc{index}"/>')
    manifest_xml = "\n".join(manifest)
    spine_xml = "\n".join(spine)
    nav = """<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops"><body><nav epub:type="toc"><ol>
<li><a href="A/chapter.xhtml">A chapter</a></li>
<li><a href="B/chapter.xhtml">B chapter</a></li>
<li><a href="A/chapter.html">HTML chapter</a></li>
<li><a href="Text/space%20name.xhtml">Encoded space</a></li>
<li><a href="Text/literal%2520.xhtml">Literal percent</a></li>
</ol></nav></body></html>"""
    nav_points = []
    for index, (title, href) in enumerate(
        (
            ("A chapter", "../A/chapter.xhtml"),
            ("B chapter", "../B/chapter.xhtml"),
            ("HTML chapter", "../A/chapter.html"),
            ("Encoded space", "../Text/space%20name.xhtml"),
            ("Literal percent", "../Text/literal%2520.xhtml"),
        ),
        1,
    ):
        nav_points.append(
            f'<navPoint id="n{index}" playOrder="{index}"><navLabel><text>{title}</text></navLabel><content src="{href}"/></navPoint>'
        )
    ncx = (
        '<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/"><navMap>'
        + "".join(nav_points)
        + "</navMap></ncx>"
    )
    opf = f"""<?xml version="1.0" encoding="utf-8"?>
<package xmlns="http://www.idpf.org/2007/opf" xmlns:dc="http://purl.org/dc/elements/1.1/" version="3.0">
<metadata><dc:title>Identity fixture</dc:title><dc:language>en</dc:language></metadata>
<manifest>{manifest_xml}</manifest><spine>{spine_xml}</spine></package>"""
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("mimetype", "application/epub+zip", compress_type=zipfile.ZIP_STORED)
        archive.writestr(
            "META-INF/container.xml",
            '<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container"><rootfiles><rootfile full-path="OEBPS/content.opf"/></rootfiles></container>',
        )
        archive.writestr("OEBPS/content.opf", opf)
        archive.writestr("OEBPS/nav.xhtml", nav)
        archive.writestr("OEBPS/Navigation/toc.ncx", ncx)
        for member, content in resources.items():
            archive.writestr(member, content)
        for member, content in documents.items():
            archive.writestr(member, content)
    return documents


def test_full_package_identity_survives_extraction_grouping_reconstruction_and_replace(
    tmp_path: Path,
) -> None:
    source_epub = tmp_path / "source.epub"
    source_documents = _write_identity_epub(source_epub)
    pipeline = HTMLEpubPipeline(
        source_epub,
        tmp_path / "work",
        {"html_translation": {"epubcheck_mode": "off"}},
    )
    try:
        assert pipeline.extract_and_preprocess(target_language="Chinese") == len(source_documents)
        mapping_paths = sorted(pipeline.compressed_units_dir.glob("*.mapping.json"))
        assert len(mapping_paths) == len(source_documents)

        mappings = {}
        compressed_by_path = {}
        package_path_by_stem = {}
        for mapping_path in mapping_paths:
            mapping = json.loads(mapping_path.read_text(encoding="utf-8"))
            package_path = mapping["package_relative_path"]
            mappings[package_path] = mapping
            unit_stem = mapping_path.name[:-len(".mapping.json")]
            package_path_by_stem[unit_stem] = package_path
            compressed_by_path[package_path] = (
                pipeline.compressed_units_dir / f"{unit_stem}.md"
            )
        assert set(mappings) == set(source_documents)
        assert len({path.casefold() for path in mappings}) == len(mappings)
        assert len({unit_id_for_package_path(path).casefold() for path in mappings}) == len(mappings)
        assert unit_id_for_package_path("OEBPS/A/page.xhtml") != unit_id_for_package_path(
            "OEBPS/a/page.xhtml"
        )
        assert mappings["OEBPS/A/chapter.xhtml"]["package_relative_path"] != mappings[
            "OEBPS/B/chapter.xhtml"
        ]["package_relative_path"]
        assert unit_id_for_package_path("OEBPS/A/chapter.xhtml") != unit_id_for_package_path(
            "OEBPS/A/chapter.html"
        )
        assert "OEBPS/Text/space name.xhtml" in mappings
        assert "OEBPS/Text/literal%20.xhtml" in mappings

        declared = pipeline.declared_translation_files()
        groups = _load_html_chapter_groups(pipeline, declared)
        assert len(groups) == len(source_documents)
        assigned = [name for names in groups.values() for name in names]
        assert len(assigned) == len(set(assigned)) == len(source_documents)
        grouped_package_paths = []
        for names in groups.values():
            assert len(names) == 1
            grouped_package_paths.append(package_path_by_stem[Path(names[0]).stem])
        assert set(grouped_package_paths) == set(mappings)
        expected_group_paths = [
            "OEBPS/A/chapter.xhtml",
            "OEBPS/B/chapter.xhtml",
            "OEBPS/A/chapter.html",
            "OEBPS/Text/space name.xhtml",
            "OEBPS/Text/literal%20.xhtml",
        ]
        for index, expected_path in enumerate(expected_group_paths, 1):
            assert len(groups[f"toc_{index:03d}"]) == 1
            assert package_path_by_stem[Path(groups[f"toc_{index:03d}"][0]).stem] == expected_path

        pipeline.parser.get_raw_content = lambda _href: pytest.fail(
            "current package identity cache should skip source reads"
        )
        assert pipeline.extract_and_preprocess(target_language="Chinese") == len(source_documents)

        translations = {
            "OEBPS/A/chapter.xhtml": "Translated A",
            "OEBPS/B/chapter.xhtml": "Translated B",
            "OEBPS/A/chapter.html": "Translated HTML extension",
            "OEBPS/Text/space name.xhtml": "Translated encoded space",
            "OEBPS/Text/literal%20.xhtml": "Translated literal percent",
        }
        for package_path, source_path in compressed_by_path.items():
            unit_stem = unit_id_for_package_path(package_path)
            translated = source_path.read_text(encoding="utf-8")
            original_text = {
                "OEBPS/A/chapter.xhtml": "Original A",
                "OEBPS/B/chapter.xhtml": "Original B",
                "OEBPS/A/chapter.html": "Original HTML extension",
                "OEBPS/Text/space name.xhtml": "Original encoded space",
                "OEBPS/Text/literal%20.xhtml": "Original literal percent",
            }[package_path]
            translated = translated.replace(original_text, translations[package_path])
            (pipeline.translated_dir / f"{unit_stem}.md").write_text(
                translated, encoding="utf-8"
            )

        pipeline.validate_translated_units = lambda *_args: {"all_passed": True, "missing": []}
        pipeline.write_translation_report = lambda **_kwargs: tmp_path / "report.json"
        output_epub = tmp_path / "translated.epub"
        pipeline.postprocess_and_build(output_epub=output_epub)

        with zipfile.ZipFile(output_epub) as archive:
            for package_path, translation in translations.items():
                rendered = archive.read(package_path).decode("utf-8")
                assert translation in rendered, package_path
            a_document = archive.read("OEBPS/A/chapter.xhtml").decode("utf-8")
            b_document = archive.read("OEBPS/B/chapter.xhtml").decode("utf-8")
            assert "href=\"../B/chapter.xhtml#target\"" in a_document
            assert 'id="target"' in b_document
            assert "Translated B" not in a_document
            assert "Translated A" not in b_document
            assert archive.read("OEBPS/A/shared.css") == b"/* A stylesheet */"
            assert archive.read("OEBPS/B/shared.css") == b"/* B stylesheet */"
    finally:
        pipeline.parser._zf.close()


def _write_target(extract_dir: Path, relative_path: str, text: str) -> Path:
    target = extract_dir.joinpath(*Path(relative_path).parts)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(f"<html><body>{text}</body></html>", encoding="utf-8")
    return target


def test_legacy_flat_translation_is_allowed_for_one_matching_package_target(
    tmp_path: Path,
) -> None:
    extract_dir = tmp_path / "extract"
    _write_target(extract_dir, "OEBPS/unique.xhtml", "original")
    translated = tmp_path / "translated"
    translated.mkdir()
    (translated / "unique.xhtml").write_text(
        "<html><body>translated</body></html>", encoding="utf-8"
    )

    count = EPUBFilePackager(tmp_path / "output.epub", translated).replace_xhtml_files(extract_dir)
    assert count == 1
    assert "translated" in (extract_dir / "OEBPS/unique.xhtml").read_text(encoding="utf-8")


def test_ambiguous_legacy_translation_fails_before_any_replacement(
    tmp_path: Path,
) -> None:
    extract_dir = tmp_path / "extract"
    first = _write_target(extract_dir, "OEBPS/A/chapter.xhtml", "original A")
    second = _write_target(extract_dir, "OEBPS/B/chapter.xhtml", "original B")
    unique = _write_target(extract_dir, "OEBPS/unique.xhtml", "original unique")
    before = {path: path.read_text(encoding="utf-8") for path in (first, second, unique)}
    translated = tmp_path / "translated"
    translated.mkdir()
    (translated / "chapter.xhtml").write_text("<html><body>ambiguous</body></html>", encoding="utf-8")
    (translated / "unique.xhtml").write_text("<html><body>should not apply</body></html>", encoding="utf-8")

    with pytest.raises(ValueError, match="Ambiguous legacy translated file"):
        EPUBFilePackager(tmp_path / "output.epub", translated).replace_xhtml_files(extract_dir)
    assert {path: path.read_text(encoding="utf-8") for path in before} == before


@pytest.mark.parametrize(
    "mapping",
    [
        {"package_relative_path": "../outside.xhtml", "original_extension": ".xhtml"},
        {"package_relative_path": "C:/outside.xhtml", "original_extension": ".xhtml"},
        {"package_relative_path": "OEBPS\\chapter.xhtml", "original_extension": ".xhtml"},
        {"original_extension": "../../outside.xhtml"},
    ],
)
def test_unsafe_mapping_path_fails_before_reconstruction(
    tmp_path: Path,
    mapping: dict,
) -> None:
    compressed = tmp_path / "compressed"
    translated = tmp_path / "translated"
    final = tmp_path / "final"
    for directory in (compressed, translated, final):
        directory.mkdir()
    (compressed / "chapter.mapping.json").write_text(json.dumps(mapping), encoding="utf-8")
    (compressed / "chapter.md").write_text("source", encoding="utf-8")
    (translated / "chapter.md").write_text("translation", encoding="utf-8")
    store = HTMLUnitStore(HTMLUnitWorkspace(compressed, translated, final))

    with pytest.raises(ValueError, match="package-relative path|portable|unsafe|original_extension"):
        store.logical_unit_records()


def test_legacy_mapping_migrates_unique_identity_without_touching_translation(
    tmp_path: Path,
) -> None:
    class Parser:
        resources = {"css": []}
        spine = [{"href": "Text/chapter.xhtml", "full_href": "OEBPS/Text/chapter.xhtml"}]

        @staticmethod
        def get_raw_content(_href: str) -> str:
            return "<html><body><p>Current source</p></body></html>"

    work = tmp_path / "work"
    compressed = work / "compressed_units"
    translated = work / "translated_compressed"
    compressed.mkdir(parents=True)
    translated.mkdir()
    epub = work / "input.epub"
    epub.write_bytes(b"synthetic")
    (compressed / "chapter.mapping.json").write_text(
        json.dumps({"original_extension": ".xhtml", "units": []}), encoding="utf-8"
    )
    (compressed / "chapter.md").write_text("old source", encoding="utf-8")
    translation_path = translated / "chapter.md"
    translation_path.write_text("user translation", encoding="utf-8")
    pipeline = object.__new__(HTMLEpubPipeline)
    pipeline.output_dir = work
    pipeline.compressed_units_dir = compressed
    pipeline.translated_dir = translated
    pipeline.final_dir = work / "final_xhtml"
    pipeline.epub_path = epub
    pipeline.parser = Parser()
    pipeline.config = {}
    pipeline.create_metadata_translation_source = lambda **_kwargs: None

    assert pipeline.extract_and_preprocess(target_language="Chinese") == 1
    migrated = json.loads((compressed / "chapter.mapping.json").read_text(encoding="utf-8"))
    assert migrated["package_relative_path"] == "OEBPS/Text/chapter.xhtml"
    assert migrated["legacy_unit_id"] is True
    assert translation_path.read_text(encoding="utf-8") == "user translation"
    assert pipeline._compressed_cache_is_current(max_nonempty_lines=350) is True


def test_pathless_schema_one_cache_is_not_reused(tmp_path: Path) -> None:
    work = tmp_path / "work"
    compressed = work / "compressed_units"
    compressed.mkdir(parents=True)
    epub = work / "input.epub"
    epub.write_bytes(b"old input")
    (compressed / "chapter.mapping.json").write_text(
        json.dumps({"original_extension": ".xhtml"}), encoding="utf-8"
    )
    cache = CompressedExtractionCache(work, epub, compressed)
    cache.path.write_text(
        json.dumps({
            "schema_version": 1,
            "input_epub_sha256": hashlib.sha256(epub.read_bytes()).hexdigest(),
            "max_nonempty_lines": 350,
            "compressed_files": {},
        }),
        encoding="utf-8",
    )
    assert cache.is_current(max_nonempty_lines=350) is False


def test_epub_extraction_rejects_member_paths_that_collide_on_windows(
    tmp_path: Path,
) -> None:
    epub = tmp_path / "case-collision.epub"
    with zipfile.ZipFile(epub, "w") as archive:
        archive.writestr("OEBPS/A/page.xhtml", "upper")
        archive.writestr("OEBPS/a/page.xhtml", "lower")
    with pytest.raises(ValueError, match="collide on Windows"):
        extract_epub(epub, tmp_path / "extract")


def test_ambiguous_legacy_mapping_fails_before_extraction_writes(
    tmp_path: Path,
) -> None:
    class Parser:
        resources = {"css": []}
        spine = [
            {"href": "A/chapter.xhtml", "full_href": "OEBPS/A/chapter.xhtml"},
            {"href": "B/chapter.xhtml", "full_href": "OEBPS/B/chapter.xhtml"},
        ]

        @staticmethod
        def get_raw_content(href: str) -> str:
            return f"<html><body><p>{href}</p></body></html>"

    work = tmp_path / "work"
    compressed = work / "compressed_units"
    translated = work / "translated_compressed"
    compressed.mkdir(parents=True)
    translated.mkdir()
    epub = work / "input.epub"
    epub.write_bytes(b"synthetic")
    old_mapping = {"original_extension": ".xhtml", "units": []}
    mapping_path = compressed / "chapter.mapping.json"
    mapping_path.write_text(json.dumps(old_mapping), encoding="utf-8")
    source_path = compressed / "chapter.md"
    source_path.write_text("old source", encoding="utf-8")
    translation_path = translated / "chapter.md"
    translation_path.write_text("user translation", encoding="utf-8")
    pipeline = object.__new__(HTMLEpubPipeline)
    pipeline.output_dir = work
    pipeline.compressed_units_dir = compressed
    pipeline.translated_dir = translated
    pipeline.final_dir = work / "final_xhtml"
    pipeline.epub_path = epub
    pipeline.parser = Parser()
    pipeline.config = {}
    pipeline.create_metadata_translation_source = lambda **_kwargs: None

    with pytest.raises(ValueError, match="multiple current spine members"):
        pipeline.extract_and_preprocess(target_language="Chinese")
    assert json.loads(mapping_path.read_text(encoding="utf-8")) == old_mapping
    assert source_path.read_text(encoding="utf-8") == "old source"
    assert translation_path.read_text(encoding="utf-8") == "user translation"
    assert list(compressed.glob("*.mapping.json")) == [mapping_path]
    assert not (work / "html_prepare_cache.json").exists()
