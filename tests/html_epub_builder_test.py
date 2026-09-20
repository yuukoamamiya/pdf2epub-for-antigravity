from pathlib import Path
import json
from types import SimpleNamespace

import pytest
from lxml import etree

from pdf2epub.html_translation import builder as builder_module
from pdf2epub.html_translation.builder import BuildConfig, HTMLEpubBuilder, HTMLEpubPipeline


OPF_NS = "http://www.idpf.org/2007/opf"
DC_NS = "http://purl.org/dc/elements/1.1/"


def _builder(
    tmp_path: Path,
    *,
    book_title: str = "Book",
    epubcheck_mode: str = "off",
    epubcheck_path: str | None = None,
) -> HTMLEpubBuilder:
    return HTMLEpubBuilder(
        BuildConfig(
            original_epub=tmp_path / "input.epub",
            translated_dir=tmp_path / "translated",
            output_path=tmp_path / "output.epub",
            book_title=book_title,
            epubcheck_mode=epubcheck_mode,
            epubcheck_path=epubcheck_path,
        )
    )


def test_update_content_opf_preserves_authors_and_namespaced_attrs(
    tmp_path: Path,
) -> None:
    meta_inf = tmp_path / "META-INF"
    oebps = tmp_path / "OEBPS"
    meta_inf.mkdir()
    oebps.mkdir()
    (meta_inf / "container.xml").write_text(
        """<?xml version="1.0" encoding="UTF-8"?>
<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
  <rootfiles>
    <rootfile full-path="OEBPS/content.opf"
              media-type="application/oebps-package+xml"/>
  </rootfiles>
</container>
""",
        encoding="utf-8",
    )
    opf_path = oebps / "content.opf"
    opf_path.write_text(
        f"""<?xml version="1.0" encoding="UTF-8"?>
<package xmlns="{OPF_NS}" xmlns:dc="{DC_NS}" xmlns:opf="{OPF_NS}"
         unique-identifier="book-id" version="2.0">
  <metadata>
    <dc:title>Original title</dc:title>
    <dc:creator opf:role="aut" opf:file-as="Alpha, A">Author One</dc:creator>
    <dc:creator opf:role="aut">Author Two</dc:creator>
    <dc:contributor opf:role="bkp">Producer</dc:contributor>
    <dc:language>en</dc:language>
    <dc:identifier id="book-id" opf:scheme="ISBN">123</dc:identifier>
  </metadata>
  <manifest/>
  <spine/>
</package>
""",
        encoding="utf-8",
    )

    _builder(tmp_path)._update_content_opf(
        tmp_path,
        {
            "translated_title": "Translated title",
            "target_language_code": "zh",
            "translated_author": "作者一, 作者二",
            "translated_author_file_as": "作者一, 作者二",
        },
    )

    root = etree.parse(str(opf_path)).getroot()
    namespaces = {"dc": DC_NS}
    creators = root.findall(".//dc:creator", namespaces)
    assert len(creators) == 2
    assert [creator.text for creator in creators] == ["Author One", "Author Two"]
    assert creators[0].get(f"{{{OPF_NS}}}role") == "aut"
    assert creators[0].get(f"{{{OPF_NS}}}file-as") == "One, Author"
    assert creators[1].get(f"{{{OPF_NS}}}file-as") == "Two, Author"
    assert creators[0].get("role") is None
    assert creators[0].get("file-as") is None

    contributor = root.find(".//dc:contributor", namespaces)
    identifier = root.find(".//dc:identifier", namespaces)
    assert contributor is not None
    assert identifier is not None
    assert contributor.get(f"{{{OPF_NS}}}role") == "bkp"
    assert identifier.get(f"{{{OPF_NS}}}scheme") == "ISBN"
    assert root.findtext(".//dc:title", namespaces=namespaces) == "Translated title"
    assert root.findtext(".//dc:language", namespaces=namespaces) == "zh"


def test_update_epub3_creators_and_refinements_are_preserved(
    tmp_path: Path,
) -> None:
    meta_inf = tmp_path / "META-INF"
    oebps = tmp_path / "OEBPS"
    meta_inf.mkdir()
    oebps.mkdir()
    (meta_inf / "container.xml").write_text(
        """<?xml version="1.0" encoding="UTF-8"?>
<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
  <rootfiles>
    <rootfile full-path="OEBPS/package.opf"
              media-type="application/oebps-package+xml"/>
  </rootfiles>
</container>
""",
        encoding="utf-8",
    )
    opf_path = oebps / "package.opf"
    opf_path.write_text(
        f"""<?xml version="1.0" encoding="UTF-8"?>
<package xmlns="{OPF_NS}" xmlns:dc="{DC_NS}"
         unique-identifier="book-id" version="3.0">
  <metadata>
    <dc:identifier id="book-id">book-id</dc:identifier>
    <dc:title id="title-1">Original title</dc:title>
    <meta refines="#title-1" property="file-as">Original title</meta>
    <dc:language>en</dc:language>
    <dc:creator id="creator-1">Author One</dc:creator>
    <meta refines="#creator-1" property="role" scheme="marc:relators">aut</meta>
    <meta refines="#creator-1" property="file-as">Alpha, A</meta>
    <dc:creator id="creator-2">Author Two</dc:creator>
    <meta refines="#creator-2" property="role" scheme="marc:relators">aut</meta>
    <meta refines="#creator-2" property="file-as">Beta, B</meta>
    <meta property="dcterms:modified">2026-01-01T00:00:00Z</meta>
    <!-- Converter metadata comments are valid OPF children. -->
    <meta name="calibre:title_sort" content="Original title"/>
  </metadata>
  <manifest/>
  <spine/>
</package>
""",
        encoding="utf-8",
    )

    _builder(tmp_path)._update_content_opf(
        tmp_path,
        {
            "translated_title": "Translated title",
            "target_language_code": "zh",
            "translated_author": "作者一, 作者二",
            "translated_author_file_as": "作者一, 作者二",
            "translated_title_sort": "Translated title",
        },
    )

    root = etree.parse(str(opf_path)).getroot()
    namespaces = {"opf": OPF_NS, "dc": DC_NS}
    creators = root.findall(".//dc:creator", namespaces)
    assert [(creator.get("id"), creator.text) for creator in creators] == [
        ("creator-1", "Author One"),
        ("creator-2", "Author Two"),
    ]

    refinements = root.findall(".//opf:meta[@refines]", namespaces)
    assert {meta.get("refines") for meta in refinements} >= {"#creator-1", "#creator-2"}
    first_creator_refinements = {
        meta.get("property"): meta.text
        for meta in refinements
        if meta.get("refines") == "#creator-1"
    }
    assert first_creator_refinements == {"role": "aut", "file-as": "One, Author"}
    title_sort = root.find(".//opf:meta[@name='calibre:title_sort']", namespaces)
    assert title_sort is not None
    assert title_sort.get("content") == "Translated title"
    title_file_as = root.find(
        ".//opf:meta[@refines='#title-1'][@property='file-as']", namespaces
    )
    assert title_file_as is not None
    assert title_file_as.text == "Translated title"


def test_epub_metadata_paths_cannot_escape_extracted_tree(tmp_path: Path) -> None:
    extract_dir = tmp_path / "extract"
    meta_inf = extract_dir / "META-INF"
    meta_inf.mkdir(parents=True)

    outside_opf = tmp_path / "outside.opf"
    outside_opf.write_text("<package/>", encoding="utf-8")
    (meta_inf / "container.xml").write_text(
        f"""<?xml version="1.0" encoding="UTF-8"?>
<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
  <rootfiles><rootfile full-path="../{outside_opf.name}"
    media-type="application/oebps-package+xml"/></rootfiles>
</container>
""",
        encoding="utf-8",
    )

    builder = _builder(tmp_path)

    assert builder._find_opf_path(extract_dir) is None
    assert outside_opf.read_text(encoding="utf-8") == "<package/>"


def test_epub_navigation_paths_cannot_escape_extracted_tree(tmp_path: Path) -> None:
    extract_dir = tmp_path / "extract"
    oebps = extract_dir / "OEBPS"
    oebps.mkdir(parents=True)
    opf_path = oebps / "content.opf"
    opf_path.write_text(
        f"""<?xml version="1.0" encoding="UTF-8"?>
<package xmlns="{OPF_NS}" version="2.0">
  <manifest>
    <item id="ncx" href="../../outside.ncx"
      media-type="application/x-dtbncx+xml"/>
  </manifest>
  <spine toc="ncx"/>
</package>
""",
        encoding="utf-8",
    )
    outside_ncx = tmp_path / "outside.ncx"
    outside_ncx.write_text("<ncx/>", encoding="utf-8")

    result = _builder(tmp_path)._find_toc_files(extract_dir, opf_path)

    assert result == {"ncx": None, "nav": None}
    assert outside_ncx.read_text(encoding="utf-8") == "<ncx/>"


def test_update_content_opf_derives_and_creates_library_sort_metadata(
    tmp_path: Path,
) -> None:
    oebps = tmp_path / "OEBPS"
    oebps.mkdir()
    opf_path = oebps / "content.opf"
    opf_path.write_text(
        f"""<?xml version="1.0" encoding="UTF-8"?>
<package xmlns="{OPF_NS}" xmlns:dc="{DC_NS}" xmlns:opf="{OPF_NS}"
         unique-identifier="book-id" version="2.0">
  <metadata>
    <dc:title>The Class Matrix</dc:title>
    <dc:creator opf:role="aut">Vivek Chibber</dc:creator>
    <dc:language>en</dc:language>
  </metadata>
  <manifest/>
  <spine/>
</package>
""",
        encoding="utf-8",
    )

    _builder(tmp_path, book_title="The Class Matrix")._update_content_opf(
        tmp_path,
        {"translated_title": "阶级矩阵", "target_language_code": "zh"},
    )

    root = etree.parse(str(opf_path)).getroot()
    namespaces = {"opf": OPF_NS, "dc": DC_NS}
    creator = root.find(".//dc:creator", namespaces)
    title_sort = root.find(".//opf:meta[@name='calibre:title_sort']", namespaces)
    assert creator is not None
    assert creator.get(f"{{{OPF_NS}}}file-as") == "Chibber, Vivek"
    assert title_sort is not None
    assert title_sort.get("content") == "阶级矩阵"
    assert builder_module.sort_title_for_library("The Class Matrix") == "Class Matrix, The"


def test_update_toc_ncx_translates_epub2_navigation(
    tmp_path: Path,
) -> None:
    meta_inf = tmp_path / "META-INF"
    oebps = tmp_path / "OEBPS"
    meta_inf.mkdir()
    oebps.mkdir()
    (meta_inf / "container.xml").write_text(
        f"""<?xml version="1.0" encoding="UTF-8"?>
<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
  <rootfiles>
    <rootfile full-path="OEBPS/content.opf"
              media-type="application/oebps-package+xml"/>
  </rootfiles>
</container>
""",
        encoding="utf-8",
    )
    (oebps / "content.opf").write_text(
        f"""<?xml version="1.0" encoding="UTF-8"?>
<package xmlns="{OPF_NS}" xmlns:dc="{DC_NS}" version="2.0">
  <metadata><dc:title>Original title</dc:title></metadata>
  <manifest>
    <item id="ncx" href="toc.ncx" media-type="application/x-dtbncx+xml"/>
  </manifest>
  <spine toc="ncx"/>
</package>
""",
        encoding="utf-8",
    )
    ncx_path = oebps / "toc.ncx"
    ncx_path.write_text(
        """<?xml version="1.0" encoding="UTF-8"?>
<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/" version="2005-1">
  <docTitle><text>Original title</text></docTitle>
  <navMap>
    <navPoint id="chapter-1" playOrder="1">
      <navLabel><text>Original chapter</text></navLabel>
      <content src="Text/chapter.xhtml"/>
    </navPoint>
  </navMap>
</ncx>
""",
        encoding="utf-8",
    )

    builder = _builder(tmp_path)
    builder._update_toc_ncx(
        tmp_path,
        {
            "translated_title": "译文书名",
            "toc": [
                {
                    "original": "Original chapter",
                    "translated": "译文章节",
                    "href": "Text/chapter.xhtml",
                    "anchor": None,
                    "level": 1,
                }
            ],
        },
    )

    content = ncx_path.read_text(encoding="utf-8")
    assert "译文书名" in content
    assert "译文章节" in content
    assert "Original title" not in content
    assert "Original chapter" not in content
    assert builder.navigation_report["ncx"] == {
        "status": "updated",
        "path": "OEBPS/toc.ncx",
        "updated_entries": 1,
    }


def test_update_toc_ncx_records_warning_without_aborting_build(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ncx_path = tmp_path / "toc.ncx"
    ncx_path.write_text("<ncx>", encoding="utf-8")
    builder = _builder(tmp_path)
    monkeypatch.setattr(builder, "_find_opf_path", lambda _extract_dir: tmp_path / "book.opf")
    monkeypatch.setattr(
        builder,
        "_find_toc_files",
        lambda _extract_dir, _opf_path: {"ncx": ncx_path, "nav": None},
    )

    builder._update_toc_ncx(
        tmp_path,
        {"toc": [{"href": "chapter.xhtml", "translated": "译文"}]},
    )

    result = builder.navigation_report["ncx"]
    assert result["status"] == "warning"
    assert result["path"] == "toc.ncx"
    assert "error" in result


def test_translation_report_includes_navigation_diagnostics(tmp_path: Path) -> None:
    pipeline = object.__new__(HTMLEpubPipeline)
    pipeline.output_dir = tmp_path
    pipeline.compressed_units_dir = tmp_path / "compressed_units"
    pipeline.translated_dir = tmp_path / "translated_compressed"
    pipeline.final_dir = tmp_path / "final_xhtml"
    pipeline.compressed_units_dir.mkdir()
    pipeline.translated_dir.mkdir()
    pipeline.final_dir.mkdir()
    pipeline.epub_path = tmp_path / "input.epub"
    pipeline.book_title = "Book"
    pipeline.navigation_report = {
        "ncx": {
            "status": "warning",
            "path": "OEBPS/toc.ncx",
            "updated_entries": 0,
            "error": "test failure",
        },
        "nav": {"status": "not_found", "path": None, "updated_entries": 0},
    }

    report_path = pipeline.write_translation_report()
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["navigation"]["ncx"]["status"] == "warning"
    assert report["navigation_warnings"] == ["ncx"]


def test_epubcheck_warn_mode_keeps_failed_build(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    builder = _builder(
        tmp_path,
        epubcheck_mode="warn",
        epubcheck_path="/tools/epubcheck",
    )
    monkeypatch.setattr(
        builder_module.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=1,
            stdout="ERROR: invalid package",
            stderr="",
        ),
    )

    builder._validate_output_epub()

def test_epubcheck_strict_mode_rejects_failed_build(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    builder = _builder(
        tmp_path,
        epubcheck_mode="strict",
        epubcheck_path="/tools/epubcheck",
    )
    monkeypatch.setattr(
        builder_module.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=1,
            stdout="ERROR: invalid package",
            stderr="",
        ),
    )

    with pytest.raises(ValueError, match="EPUBCheck failed"):
        builder._validate_output_epub()


def test_epubcheck_strict_mode_requires_executable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    builder = _builder(tmp_path, epubcheck_mode="strict")
    monkeypatch.setattr(builder_module.shutil, "which", lambda name: None)

    with pytest.raises(RuntimeError, match="not installed"):
        builder._validate_output_epub()
