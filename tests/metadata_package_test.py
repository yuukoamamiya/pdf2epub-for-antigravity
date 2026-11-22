from pathlib import Path

import pytest
from lxml import etree
from PIL import Image

from pdf2epub.metadata_package import apply_package_metadata


@pytest.mark.parametrize("version", ["2.0", "3.0"])
def test_verified_metadata_fills_missing_fields_and_adds_non_linear_cover(tmp_path: Path, version: str):
    root = tmp_path / "EPUB"
    root.mkdir()
    opf = root / "content.opf"
    opf.write_text(
        f'''<?xml version="1.0"?><package xmlns="http://www.idpf.org/2007/opf" version="{version}" unique-identifier="uid">
        <metadata xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:title>Book</dc:title><dc:description>Existing description</dc:description><dc:identifier id="uid">uuid:keep</dc:identifier></metadata>
        <manifest><item id="chapter" href="chapter.xhtml" media-type="application/xhtml+xml"/></manifest>
        <spine><itemref idref="chapter"/></spine></package>''', encoding="utf-8")
    cover = tmp_path / "cover.jpg"
    (root / "text").mkdir()
    existing_note = root / "text" / "source-edition-note.xhtml"
    existing_note.write_text("<html>Original body resource</html>", encoding="utf-8")
    Image.new("RGB", (32, 48), color=(180, 80, 50)).save(cover, format="JPEG")
    cover_bytes = cover.read_bytes()
    plan = {
        "metadata": {
                "authors": [{"name": "A. Writer", "role": "aut"}, {"name": "B. Editor", "role": "edt"}, {"name": "C. Coauthor", "role": "aut"}],
            "publisher": "Press", "date": "2001", "description": "Candidate description",
            "subjects": ["History"], "identifiers": {"doi": "10.1234/example"},
            "source_isbn": "9780000000000", "isbn_role": "translation_source",
            "relationship_note": "Source language edition", "cover_path": str(cover),
        },
        "verified_fields": ["authors", "publishing", "description", "subjects", "identifiers", "cover"],
    }
    apply_package_metadata(opf, plan, cover_policy="fill_missing")
    apply_package_metadata(opf, plan, cover_policy="fill_missing")
    parser = etree.XMLParser()
    tree = etree.parse(str(opf), parser)
    ns = {"opf": "http://www.idpf.org/2007/opf", "dc": "http://purl.org/dc/elements/1.1/"}
    md = tree.find("opf:metadata", ns)
    assert md.findtext("dc:description", namespaces=ns) == "Existing description"
    assert md.findtext("dc:publisher", namespaces=ns) == "Press"
    assert md.findtext("dc:date", namespaces=ns) == "2001"
    assert md.findtext("dc:title", namespaces=ns) == "Book"
    assert md.findtext("dc:source", namespaces=ns) == "urn:isbn:9780000000000"
    assert "9780000000000" not in [x.text for x in md.findall("dc:identifier", ns)]
    if version == "2.0":
        assert any(x.get("{http://www.idpf.org/2007/opf}scheme") == "DOI" for x in md.findall("dc:identifier", ns))
    else:
        assert any(x.get("property") == "identifier-type" and x.text == "06" for x in md.findall("opf:meta", ns))
    creators = md.findall("dc:creator", ns)
    assert [x.text for x in creators] == ["A. Writer", "B. Editor", "C. Coauthor"]
    if version == "2.0":
        assert [x.get("{http://www.idpf.org/2007/opf}role") for x in creators] == ["aut", "edt", "aut"]
        assert md.find("opf:meta[@name='cover']", ns) is not None
    else:
        assert [x.text for x in md.findall("opf:meta[@property='role']", ns)] == ["aut", "edt", "aut"]
    spine = tree.find("opf:spine", ns)
    assert spine[0].get("linear") == "no"
    manifest = tree.find("opf:manifest", ns)
    if version == "3.0":
        assert any("cover-image" in (x.get("properties") or "") for x in manifest)
    else:
        assert md.find("opf:meta[@name='cover']", ns) is not None
    assets = list((root / "images").glob("verified-cover-*.jpg"))
    assert len(assets) == 1 and assets[0].read_bytes() == cover_bytes
    cover_page = tree.find("opf:manifest/opf:item[@id='metadata-cover-page-" + assets[0].stem.removeprefix("verified-cover-") + "']", ns)
    assert cover_page is not None
    assert (root / cover_page.get("href")).is_file()
    assert len(md.findall("dc:source", ns)) == 1
    assert existing_note.read_text(encoding="utf-8") == "<html>Original body resource</html>"
    assert (root / "text" / "source-edition-note-2.xhtml").is_file()
    assert sum(1 for ref in spine.findall("opf:itemref", ns) if "metadata-cover-page-" in (ref.get("idref") or "")) == 1


def test_cover_policy_preserves_existing_and_verified_preference_replaces_in_place(tmp_path: Path):
    root = tmp_path / "EPUB"
    (root / "images").mkdir(parents=True)
    (root / "text").mkdir()
    old_image = root / "images" / "old.jpg"
    old_image.write_bytes(b"source-cover")
    (root / "text" / "cover.xhtml").write_text("<html xmlns='http://www.w3.org/1999/xhtml'><body><img src='../images/old.jpg' alt='cover'/></body></html>", encoding="utf-8")
    uncovering = root / "text" / "Uncovering_history.xhtml"
    uncovering.write_text("<html xmlns='http://www.w3.org/1999/xhtml'><body><p>Uncovering history of a field.</p></body></html>", encoding="utf-8")
    opf = root / "content.opf"
    opf.write_text('''<package xmlns="http://www.idpf.org/2007/opf" version="3.0"><metadata xmlns:dc="http://purl.org/dc/elements/1.1/"/><manifest>
    <item id="cover-image" href="images/old.jpg" media-type="image/jpeg" properties="cover-image"/><item id="cover" href="text/cover.xhtml" media-type="application/xhtml+xml"/><item id="chapter" href="chapter.xhtml" media-type="application/xhtml+xml"/><item id="uncovering" href="text/Uncovering_history.xhtml" media-type="application/xhtml+xml"/>
    </manifest><spine><itemref idref="cover" linear="no"/><itemref idref="chapter"/><itemref idref="uncovering"/></spine></package>''', encoding="utf-8")
    cover = tmp_path / "new.jpg"
    Image.new("RGB", (16, 16), color=(20, 30, 40)).save(cover, format="JPEG")
    plan = {"metadata": {"cover_path": str(cover)}, "verified_fields": ["cover"]}
    apply_package_metadata(opf, plan, cover_policy="fill_missing")
    assert old_image.read_bytes() == b"source-cover"
    assert not list((root / "images").glob("verified-cover-*.jpg"))
    apply_package_metadata(opf, plan, cover_policy="prefer_verified")
    apply_package_metadata(opf, plan, cover_policy="prefer_verified")
    tree = etree.parse(str(opf))
    ns = {"opf": "http://www.idpf.org/2007/opf"}
    manifest = tree.find("opf:manifest", ns)
    image_items = [x for x in manifest if "cover-image" in (x.get("properties") or "")]
    assert len(image_items) == 1
    page_items = [x for x in manifest if x.get("id") == "cover"]
    assert len(page_items) == 1
    spine = tree.find("opf:spine", ns)
    assert sum(1 for x in spine if "metadata-cover-page-" in (x.get("idref") or "")) == 1
    assert sum(1 for x in spine if x.get("idref") == "cover") == 0
    assert sum(1 for x in spine if x.get("idref") == "uncovering") == 1
    assert "Uncovering history" in uncovering.read_text(encoding="utf-8")
    assert "old.jpg" in (root / "text" / "cover.xhtml").read_text(encoding="utf-8")
