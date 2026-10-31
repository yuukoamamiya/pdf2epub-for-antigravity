"""Direct tests for extracted HTML services and lazy package exports."""
from __future__ import annotations

import json
import subprocess
import sys
import zipfile
from pathlib import Path

from lxml import etree


ROOT = Path(__file__).parents[1]


def test_file_scope_validator_checks_one_unit_without_metadata_or_packaging(tmp_path, monkeypatch):
    from pdf2epub.html_translation.translation_validation import (
        HTMLTranslationValidator,
        HTMLValidationWorkspace,
    )

    units = tmp_path / "compressed_units"
    translated = tmp_path / "translated"
    units.mkdir()
    translated.mkdir()
    (units / "chapter.md").write_text("<p>Hello world</p>", encoding="utf-8")
    (units / "other.md").write_text("<p>Other unit</p>", encoding="utf-8")
    (units / "chapter.mapping.json").write_text(
        json.dumps({"translation_parts": ["chapter.md"]}), encoding="utf-8"
    )
    (units / "other.mapping.json").write_text(
        json.dumps({"translation_parts": ["other.md"]}), encoding="utf-8"
    )
    (translated / "chapter.md").write_text("<p>你好世界</p>", encoding="utf-8")
    validator = HTMLTranslationValidator(
        HTMLValidationWorkspace(tmp_path, units, translated, {"translation": {"target_language": "Chinese"}})
    )
    monkeypatch.setattr(validator, "validate_translated_metadata", lambda: (_ for _ in ()).throw(AssertionError("metadata scope called")))

    result = validator.validate("chapter.md")
    assert result["scope"] == "file"
    assert result["file"] == "chapter.md"
    assert result["total"] == 1
    assert result["metadata"]["skipped"] is True


def test_full_html_validation_keeps_metadata_checks_after_special_marker_error(tmp_path, monkeypatch):
    import hashlib
    import pdf2epub.html_translation.translation_validation as validation
    from pdf2epub.html_translation.translation_validation import (
        HTMLTranslationValidator,
        HTMLValidationWorkspace,
    )

    units = tmp_path / "compressed_units"
    translated = tmp_path / "translated_compressed"
    units.mkdir()
    translated.mkdir()
    source = "Reference 1"
    target = "参考文献 2"
    (units / "bibliography.md").write_text(source, encoding="utf-8")
    (translated / "bibliography.md").write_text(target, encoding="utf-8")
    (units / "bibliography.mapping.json").write_text(
        json.dumps({"translation_parts": ["bibliography.md"]}), encoding="utf-8"
    )
    input_epub = tmp_path / "input.epub"
    input_epub.write_bytes(b"synthetic epub")
    (tmp_path / "translate-html_subagent_manifest.json").write_text(
        json.dumps({
            "files": ["bibliography.md"],
            "file_roles": {"bibliography.md": "bibliography"},
            "source_sha256": {
                "bibliography.md": hashlib.sha256(source.encode()).hexdigest()
            },
            "input_epub_sha256": hashlib.sha256(input_epub.read_bytes()).hexdigest(),
        }), encoding="utf-8"
    )
    validator = HTMLTranslationValidator(
        HTMLValidationWorkspace(tmp_path, units, translated, {"translation": {"target_language": "Chinese"}})
    )
    calls = []
    monkeypatch.setattr(validator, "validate_translated_metadata", lambda: calls.append("metadata") or {"valid": False})
    monkeypatch.setattr(validation, "validate_translation_context", lambda *_: calls.append("context") or {"valid": False})

    result = validator.validate()

    assert calls == ["metadata", "context"]
    assert result["scope"] == "book"
    assert "numeric marker mismatch" in result["invalid"][0]["reason"]
    assert result["metadata"]["valid"] is False
    assert result["translation_context"]["valid"] is False
    assert result["all_passed"] is False
    assert result["book_complete"] is False


def test_html_split_unit_file_scope_and_book_completeness(tmp_path, monkeypatch):
    import hashlib
    import pdf2epub.html_translation.translation_validation as validation
    from pdf2epub.html_translation.translation_validation import (
        HTMLTranslationValidator,
        HTMLValidationWorkspace,
    )

    units = tmp_path / "compressed_units"
    translated = tmp_path / "translated_compressed"
    units.mkdir()
    translated.mkdir()
    names = ["Notes.part1.md", "Notes.part2.md"]
    sources = ["Reference 2020", "Reference 2021"]
    for name, source in zip(names, sources):
        (units / name).write_text(source, encoding="utf-8")
    (units / "Notes.mapping.json").write_text(
        json.dumps({"translation_parts": names}), encoding="utf-8"
    )
    input_epub = tmp_path / "input.epub"
    input_epub.write_bytes(b"synthetic epub")
    manifest = {
        "files": names,
        "file_roles": {name: "notes" for name in names},
        "source_sha256": {
            name: hashlib.sha256(source.encode()).hexdigest()
            for name, source in zip(names, sources)
        },
        "input_epub_sha256": hashlib.sha256(input_epub.read_bytes()).hexdigest(),
    }
    manifest_path = tmp_path / "translate-html_subagent_manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    (translated / names[0]).write_text("参考文献 2020", encoding="utf-8")
    validator = HTMLTranslationValidator(
        HTMLValidationWorkspace(tmp_path, units, translated, {"translation": {"target_language": "Chinese"}})
    )
    monkeypatch.setattr(validator, "validate_translated_metadata", lambda: {"valid": True})
    monkeypatch.setattr(validation, "validate_translation_context", lambda *_: {"valid": True})

    part_result = validator.validate(names[0])
    incomplete_book = validator.validate()
    assert part_result["all_passed"] is True
    assert part_result["metadata"]["skipped"] is True
    assert incomplete_book["all_passed"] is False
    assert names[1] in incomplete_book["missing"]

    (translated / names[1]).write_text("参考文献 2021", encoding="utf-8")
    complete_book = validator.validate()
    assert complete_book["all_passed"] is True

    (translated / names[1]).write_text("参考文献 2022", encoding="utf-8")
    marker_failure = validator.validate()
    assert marker_failure["all_passed"] is False
    assert any("numeric marker mismatch" in item["reason"] for item in marker_failure["invalid"])


def test_metadata_updater_changes_opf_without_unit_workspace(tmp_path):
    from pdf2epub.html_translation.build_contracts import BuildConfig
    from pdf2epub.html_translation.package_metadata import (
        EPUBMetadataUpdater,
        PackageMetadataContext,
    )

    opf = tmp_path / "content.opf"
    opf.write_text(
        '<package xmlns="http://www.idpf.org/2007/opf" xmlns:dc="http://purl.org/dc/elements/1.1/" version="3.0"><metadata><dc:title>Old</dc:title><dc:language>en</dc:language></metadata></package>',
        encoding="utf-8",
    )
    config = BuildConfig(tmp_path / "in.epub", tmp_path / "translated", tmp_path / "out.epub", "Book")
    updater = EPUBMetadataUpdater(PackageMetadataContext(config, lambda _root: opf))
    updater.update_opf(tmp_path, {"translated_title": "新书名", "target_language_code": "zh"})
    root = etree.parse(str(opf)).getroot()
    assert root.xpath("string(.//*[local-name()='title'])") == "新书名"
    assert root.xpath("string(.//*[local-name()='language'])") == "zh"


def test_navigation_service_updates_ncx_and_records_missing_nav_warning(tmp_path):
    from pdf2epub.html_translation.package_navigation import (
        EPUBNavigationUpdater,
        NavigationUpdateContext,
    )

    opf = tmp_path / "content.opf"
    ncx = tmp_path / "toc.ncx"
    opf.write_text("<package/>", encoding="utf-8")
    ncx.write_text(
        '<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/"><docTitle><text>Old</text></docTitle><navMap><navPoint><navLabel><text>Chapter</text></navLabel><content src="Text/Chapter.xhtml"/></navPoint></navMap></ncx>',
        encoding="utf-8",
    )
    report = {}
    updater = EPUBNavigationUpdater(
        NavigationUpdateContext(
            report,
            lambda _root: opf,
            lambda _root, _opf: {"ncx": ncx, "nav": None},
        )
    )
    updater.update_toc_ncx(
        tmp_path,
        {"translated_title": "新书", "toc": [{"href": "Text/Chapter.xhtml", "translated": "章节"}]},
    )
    assert "章节" in ncx.read_text(encoding="utf-8")
    updater.update_nav_xhtml(tmp_path, {"toc": []})
    assert report["nav"]["status"] == "not_found"


def test_navigation_service_regex_fallback_updates_nav_and_records_warning(tmp_path, monkeypatch):
    from pdf2epub.html_translation.package_navigation import (
        EPUBNavigationUpdater,
        NavigationUpdateContext,
    )

    opf = tmp_path / "content.opf"
    nav = tmp_path / "nav.xhtml"
    opf.write_text("<package/>", encoding="utf-8")
    nav.write_text('<html><body><a href="chapter.xhtml">Chapter</a></body></html>', encoding="utf-8")
    report = {}
    updater = EPUBNavigationUpdater(
        NavigationUpdateContext(
            report,
            lambda _root: opf,
            lambda _root, _opf: {"nav": nav},
        )
    )
    monkeypatch.setitem(sys.modules, "bs4", None)
    updater.update_nav_xhtml(
        tmp_path,
        {"toc": [{"href": "chapter.xhtml", "translated": "章节"}]},
    )
    assert "章节" in nav.read_text(encoding="utf-8")
    assert report["nav"]["status"] == "updated"

    malformed = tmp_path / "bad.ncx"
    malformed.write_text("not xml", encoding="utf-8")
    report.clear()
    fallback_updater = EPUBNavigationUpdater(
        NavigationUpdateContext(
            report,
            lambda _root: opf,
            lambda _root, _opf: {"ncx": malformed},
        )
    )
    fallback_updater.update_toc_ncx(tmp_path, {"toc": [{"href": "a.xhtml", "translated": "A"}]})
    assert report["ncx"]["status"] == "warning"


def test_resource_packager_writes_stored_mimetype_first_and_keeps_resources(tmp_path):
    from pdf2epub.html_translation.package_resources import EPUBFilePackager

    content = tmp_path / "content"
    (content / "META-INF").mkdir(parents=True)
    (content / "mimetype").write_text("application/epub+zip", encoding="ascii")
    (content / "META-INF" / "container.xml").write_text("<container/>", encoding="utf-8")
    output = tmp_path / "book.epub"
    EPUBFilePackager(output, tmp_path / "translated").package_epub(content)
    with zipfile.ZipFile(output) as archive:
        infos = archive.infolist()
        assert infos[0].filename == "mimetype"
        assert infos[0].compress_type == zipfile.ZIP_STORED
        assert "META-INF/container.xml" in archive.namelist()


def test_invalid_unit_filename_is_rejected_before_optional_workspace_access():
    from pdf2epub.html_translation.translation_validation import HTMLTranslationValidator

    validator = object.__new__(HTMLTranslationValidator)
    try:
        validator.validate("../escape.md")
    except ValueError as exc:
        assert "direct .md filename" in str(exc)
    else:
        raise AssertionError("unsafe filename must fail before workspace access")


def test_package_imports_are_lazy_but_preserve_legacy_public_names():
    code = """
import sys
import pdf2epub.html_translation.translation_validation
assert 'pdf2epub.html_translation.builder' not in sys.modules
assert 'pdf2epub.html_translation.package_resources' not in sys.modules
from pdf2epub.html_translation import HTMLEpubBuilder, HTMLEpubPipeline
from pdf2epub.html_translation.builder import HTMLEpubBuilder as Builder
assert HTMLEpubBuilder is Builder
assert HTMLEpubPipeline is not None
"""
    subprocess.run([sys.executable, "-c", code], cwd=ROOT, check=True)
