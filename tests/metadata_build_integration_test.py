import json
import zipfile
from pathlib import Path
import pytest

from pdf2epub.build_epub import BuildEpubConfig, build_epub
from pdf2epub.build_receipt import PDF_SOURCE, HTML_TRANSLATED, receipt_status
from pdf2epub.html_translation.builder import build_html_epub
from pdf2epub.html_translation.builder import BuildConfig, HTMLEpubBuilder
from pdf2epub.metadata_enrichment.service import prepare_enrichment, validate_enrichment
from pdf2epub.metadata_enrichment.service import MetadataEnrichmentRequiredError
from pdf2epub.toc_model import TOC_TREE_SCHEMA_VERSION


def _verified_metadata(output: Path, config: dict) -> None:
    (output / "pages").mkdir(parents=True, exist_ok=True)
    (output / "pages" / "page_0001.md").write_text("Title page: Integration Book\nAuthor: Ada Writer", encoding="utf-8")
    prepare_enrichment(output, config)
    root = output / "metadata_enrichment"
    evidence = json.loads((root / "evidence.json").read_text(encoding="utf-8"))
    ref = {"location": evidence["evidence"][0]["location"], "sha256": evidence["evidence"][0]["sha256"]}
    fields = {
        "description": {"status": "verified", "value": "<p>Reviewed description.</p>", "evidence_refs": [ref]},
        "publishing": {"status": "verified", "value": {"publisher": "Example Press", "date": "2001"}, "evidence_refs": [ref]},
        "authors": {"status": "verified", "value": [{"name": "Ada Writer", "role": "author"}], "evidence_refs": [ref]},
    }
    decision = {
        "schema_version": 1, "evidence_sha256": evidence["evidence_sha256"],
        "applicability": {"status": "publication", "rationale": "synthetic fixture", "evidence_refs": [ref]},
        "identity": {"status": "verified", "document_type": "book", "work_title": "Integration Book", "authors": [{"name": "Ada Writer", "role": "author"}], "evidence_refs": [ref]},
        "fields": fields,
    }
    (root / "decision.json").write_text(json.dumps(decision), encoding="utf-8")
    report = validate_enrichment(output, config)
    assert report["state"] in {"valid", "incomplete"}, report.get("errors")


def _source_epub(path: Path, version: str = "3.0") -> Path:
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("mimetype", "application/epub+zip", compress_type=zipfile.ZIP_STORED)
        archive.writestr("META-INF/container.xml", '<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container"><rootfiles><rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/></rootfiles></container>')
        archive.writestr("OEBPS/content.opf", f'''<package xmlns="http://www.idpf.org/2007/opf" version="{version}"><metadata xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:title>Integration Book</dc:title><dc:identifier id="uid">urn:uuid:existing</dc:identifier><dc:language>en</dc:language></metadata><manifest><item id="chapter" href="chapter.xhtml" media-type="application/xhtml+xml"/></manifest><spine><itemref idref="chapter"/></spine></package>''')
        archive.writestr("OEBPS/chapter.xhtml", '<html xmlns="http://www.w3.org/1999/xhtml"><body><p>Source prose stays.</p></body></html>')
    return path


def test_pdf_builder_consumes_verified_enrichment_without_changing_prose(tmp_path: Path):
    output = tmp_path / "pdf-output"
    markdown = output / "polished_markdown"
    markdown.mkdir(parents=True)
    prose = markdown / "chapter_1.md"
    prose.write_text("A paragraph whose bytes must stay fixed.\n", encoding="utf-8")
    (output / "ocr_markdown").mkdir()
    (output / "ocr_markdown" / "chapter_1.md").write_text(prose.read_text(encoding="utf-8"), encoding="utf-8")
    (output / "ocr_markdown" / "tree_progress.json").write_text(json.dumps({"units": [{"node_id": "c1", "index_path": [1], "file": "chapter_1.md"}]}), encoding="utf-8")
    (output / "toc_tree.json").write_text(json.dumps({"schema_version": TOC_TREE_SCHEMA_VERSION, "chapters": [{"node_id": "c1", "unit_id": "c1", "title": "Chapter", "level": 1, "binding_mode": "container_only", "file_path": "chapter_1.md", "part_files": ["chapter_1.md"], "children": []}]}), encoding="utf-8")
    config = {"pipeline": "pdf_conversion", "translation": {"target_language": "English"}, "footnotes": {"mode": "disabled"}, "metadata_enrichment": {"mode": "auto", "fields": ["description", "publishing", "authors"]}}
    _verified_metadata(output, config)
    prose_hash = prose.read_bytes()
    epub = build_epub(BuildEpubConfig("Integration Book", output, markdown, output / "toc_tree.json", config=config))
    assert prose.read_bytes() == prose_hash
    with zipfile.ZipFile(epub) as archive:
        opf = archive.read("content.opf").decode("utf-8")
        body = archive.read("text/chapter_1.html").decode("utf-8")
    assert "Reviewed description" in opf and "Example Press" in opf and "2001" in opf
    assert "Ada Writer" in opf and "A paragraph whose bytes must stay fixed" in body
    assert receipt_status(output, PDF_SOURCE, config=config)[0] == "current"


def test_html_builder_consumes_verified_enrichment_and_receipt_binds_it(tmp_path: Path):
    output = tmp_path / "html-output"
    translated = output / "translated_compressed"
    translated.mkdir(parents=True)
    source = tmp_path / "source.epub"
    _source_epub(source)
    source_hash = source.read_bytes()
    config = {"pipeline": "html", "translation": {"target_language": "English"}, "metadata_enrichment": {"mode": "auto", "fields": ["description", "publishing", "authors", "subjects"]}}
    _verified_metadata(output, config)
    epub = build_html_epub(source, translated, output_path=output / "translated.epub", book_title="Integration Book", epubcheck_mode="off", config=config, output_dir=output)
    with zipfile.ZipFile(epub) as archive:
        opf = archive.read("OEBPS/content.opf").decode("utf-8")
        body = archive.read("OEBPS/chapter.xhtml").decode("utf-8")
    assert "Reviewed description" in opf and "Example Press" in opf and "2001" in opf and "Ada Writer" in opf
    assert "dc:subject" not in opf
    assert "Source prose stays." in body
    assert receipt_status(output, HTML_TRANSLATED, config=config)[0] == "current"
    decision_path = output / "metadata_enrichment" / "decision.json"
    decision = json.loads(decision_path.read_text(encoding="utf-8"))
    decision["fields"]["description"]["value"] = "<p>A changed reviewed description.</p>"
    decision_path.write_text(json.dumps(decision), encoding="utf-8")
    validate_enrichment(output, config)
    assert receipt_status(output, HTML_TRANSLATED, config=config)[0] == "stale"
    assert source.read_bytes() == source_hash


def test_html_auto_fallback_and_required_gate_use_real_builder(tmp_path: Path):
    source = _source_epub(tmp_path / "source.epub")
    translated = tmp_path / "translated"
    translated.mkdir()
    auto_output = tmp_path / "auto.epub"
    build_html_epub(source, translated, output_path=auto_output, epubcheck_mode="off",
                    config={"pipeline": "html", "metadata_enrichment": {"mode": "auto"}},
                    output_dir=tmp_path / "auto-output")
    assert auto_output.is_file()
    receipt = json.loads((tmp_path / "auto-output" / "epub_build_receipt.json").read_text(encoding="utf-8"))
    assert receipt["builds"][HTML_TRANSLATED]["metadata_enrichment_outcome"]["status"] == "skipped"
    required_output = tmp_path / "required.epub"
    with pytest.raises(MetadataEnrichmentRequiredError):
        build_html_epub(source, translated, output_path=required_output, epubcheck_mode="off",
                        config={"pipeline": "html", "metadata_enrichment": {"mode": "required", "fields": ["description"], "required_fields": ["description"]}},
                        output_dir=tmp_path / "required-output")
    assert not required_output.exists()


def test_disabled_build_is_not_recorded_as_enrichment_success(tmp_path):
    source = _source_epub(tmp_path / "source.epub")
    translated = tmp_path / "translated"
    translated.mkdir()
    output = tmp_path / "output"
    output.mkdir()
    build_html_epub(source, translated, output_path=output / "book.epub", epubcheck_mode="off",
                    config={"pipeline": "html", "metadata_enrichment": {"mode": "disabled"}}, output_dir=output)
    receipt = json.loads((output / "epub_build_receipt.json").read_text(encoding="utf-8"))
    outcome = receipt["builds"][HTML_TRANSLATED]["metadata_enrichment_outcome"]
    assert outcome == {"status": "disabled", "requested_fields": []}


@pytest.mark.parametrize("version", ["2.0", "3.0"])
def test_verified_cover_is_embedded_in_renderable_final_epub(tmp_path: Path, version: str):
    import pymupdf
    from PIL import Image, ImageDraw

    source = _source_epub(tmp_path / f"source-{version}.epub", version)
    translated = tmp_path / "translated"
    translated.mkdir()
    cover = tmp_path / "verified.jpg"
    art = Image.new("RGB", (650, 1000), color=(175, 55, 80))
    draw = ImageDraw.Draw(art)
    marker = 90
    draw.rectangle((0, 0, marker - 1, marker - 1), fill=(250, 30, 30))
    draw.rectangle((650 - marker, 0, 649, marker - 1), fill=(30, 220, 30))
    draw.rectangle((0, 1000 - marker, marker - 1, 999), fill=(30, 30, 250))
    draw.rectangle((650 - marker, 1000 - marker, 649, 999), fill=(245, 220, 25))
    art.save(cover, format="JPEG", quality=100, subsampling=0)
    final = tmp_path / f"cover-{version}.epub"
    build = HTMLEpubBuilder(BuildConfig(
        original_epub=source,
        translated_dir=translated,
        output_path=final,
        book_title="Integration Book",
        epubcheck_mode="off",
        enrichment_plan={"metadata": {"cover_path": str(cover)}, "verified_fields": ["cover"]},
        cover_policy="fill_missing",
    ))
    build.build()
    with zipfile.ZipFile(final) as archive:
        opf = archive.read("OEBPS/content.opf").decode("utf-8")
        image_name = next(name for name in archive.namelist() if "verified-cover-" in name and name.endswith(".jpg"))
        assert archive.read(image_name) == cover.read_bytes()
        assert "metadata-cover-page" in opf
        cover_page = next(name for name in archive.namelist() if name.endswith(".xhtml") and b"pdf2epub-generated-cover" in archive.read(name))
        cover_xhtml = archive.read(cover_page).decode("utf-8")
        assert "max-width:100%" in cover_xhtml and "max-height:100%" in cover_xhtml
        assert "align-items:center" in cover_xhtml and "justify-content:center" in cover_xhtml
        assert "page-break-inside:avoid" in cover_xhtml
        if version == "3.0":
            assert "properties=\"cover-image\"" in opf
        else:
            assert "name=\"cover\"" in opf
    with pymupdf.open(final) as document:
        assert document.page_count >= 2
        cover_pix = document[0].get_pixmap(alpha=False)
        from io import BytesIO
        rendered_cover = Image.open(BytesIO(cover_pix.tobytes("png"))).convert("RGB")
        # The cover's red field should form a centered 0.65-aspect image area.
        red_pixels = []
        for y in range(rendered_cover.height):
            for x in range(rendered_cover.width):
                r, g, b = rendered_cover.getpixel((x, y))
                if r > 125 and r > g * 1.6 and r > b * 1.35:
                    red_pixels.append((x, y))
        assert red_pixels
        left = min(x for x, _ in red_pixels)
        right = max(x for x, _ in red_pixels)
        top = min(y for _, y in red_pixels)
        bottom = max(y for _, y in red_pixels)
        art_width, art_height = right - left + 1, bottom - top + 1
        assert abs(art_width / art_height - 0.65) < 0.04
        assert abs((left + right) / 2 - (rendered_cover.width - 1) / 2) < rendered_cover.width * 0.04
        assert top < rendered_cover.height * 0.07 and bottom > rendered_cover.height * 0.93
        # Four distinct corner swatches prove that the whole source cover remains visible.
        for point, expected in [((left + 20, top + 20), (250, 30, 30)),
                                ((right - 20, top + 20), (30, 220, 30)),
                                ((left + 20, bottom - 20), (30, 30, 250)),
                                ((right - 20, bottom - 20), (245, 220, 25))]:
            actual = rendered_cover.getpixel(point)
            assert sum(abs(a - e) for a, e in zip(actual, expected)) < 180, (point, actual, expected)
        assert "Source prose stays." in document[1].get_text()


def test_auto_apply_failure_restores_staging_and_receipt_does_not_claim_metadata(tmp_path: Path, monkeypatch):
    import pdf2epub.metadata_package as package_writer
    from PIL import Image

    source = _source_epub(tmp_path / "failure-source.epub")
    translated = tmp_path / "translated"
    translated.mkdir()
    cover = tmp_path / "failure-cover.jpg"
    Image.new("RGB", (80, 100), color=(25, 80, 140)).save(cover, format="JPEG")
    output = tmp_path / "fallback.epub"
    real_replace = package_writer.os.replace

    def fail_opf_publish(source_path, destination_path):
        if Path(destination_path).name == "content.opf" and Path(source_path).name.startswith(".metadata-opf-"):
            raise OSError("synthetic OPF publish failure")
        return real_replace(source_path, destination_path)

    monkeypatch.setattr(package_writer.os, "replace", fail_opf_publish)
    builder = HTMLEpubBuilder(BuildConfig(
        original_epub=source, translated_dir=translated, output_path=output,
        book_title="Integration Book", epubcheck_mode="off",
        enrichment_plan={"metadata": {"cover_path": str(cover), "description": "<p>Must not be claimed.</p>"}, "verified_fields": ["cover", "description"]},
        cover_policy="fill_missing", enrichment_mode="auto",
    ))
    builder.build()
    assert builder.config.enrichment_applied_plan is None
    assert builder.config.enrichment_outcome["status"] == "fallback"
    with zipfile.ZipFile(output) as archive:
        opf = archive.read("OEBPS/content.opf").decode("utf-8")
        assert "Must not be claimed" not in opf
        assert not any("metadata-cover" in name for name in archive.namelist())
    receipt = __import__("pdf2epub.build_receipt", fromlist=["write_build_receipt"])
    receipt.write_build_receipt(tmp_path, output, kind=HTML_TRANSLATED,
                                metadata_plan=builder.config.enrichment_applied_plan,
                                metadata_outcome=builder.config.enrichment_outcome)
    value = json.loads((tmp_path / "epub_build_receipt.json").read_text(encoding="utf-8"))
    entry = value["builds"][HTML_TRANSLATED]
    assert "metadata_enrichment_effective" not in entry["inputs"]
    assert entry["metadata_enrichment_outcome"]["status"] == "fallback"
    assert receipt_status(tmp_path, HTML_TRANSLATED, config={"pipeline": "html", "metadata_enrichment": {"mode": "auto"}})[0] == "current"
