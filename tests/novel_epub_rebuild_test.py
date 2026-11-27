from pathlib import Path
from types import SimpleNamespace

from lxml import etree
import pytest

from pdf2epub.commands.novel import _convert_txt_to_xhtml


class _ParserStub:
    def __init__(self, content: str) -> None:
        self.content = content

    def get_raw_content(self, href: str) -> bytes:
        return self.content.encode("utf-8")


def test_novel_xhtml_rebuild_preserves_original_document_structure(
    tmp_path: Path,
) -> None:
    original = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE html>
<html xmlns="http://www.w3.org/1999/xhtml"
      xmlns:epub="http://www.idpf.org/2007/ops"
      xml:lang="ja" class="vrtl">
<head><meta charset="UTF-8"/><title>Original</title></head>
<body class="p-text">
  <section id="chapter-1" class="main">
    <p id="toc-001" class="indent">原文<ruby>一<rt>いち</rt></ruby><span class="em">！</span></p>
    <div class="illustration"><img src="../image/ill.jpg" alt="插图"/></div>
    <p class="indent">原文二</p>
  </section>
</body>
</html>
"""
    source_dir = tmp_path / "novel_units"
    translated_dir = tmp_path / "translated_novel"
    xhtml_dir = tmp_path / "final_xhtml"
    source_dir.mkdir()
    translated_dir.mkdir()
    xhtml_dir.mkdir()

    source_path = source_dir / "008_p-001.txt"
    source_path.write_text(
        "原文一(いち)！\n[Image: ill.jpg]\n原文二",
        encoding="utf-8",
    )
    (translated_dir / source_path.name).write_text(
        "译文一\n[Image: ill.jpg]\n译文二",
        encoding="utf-8",
    )
    unit = SimpleNamespace(
        text_path=source_path,
        has_content=True,
        source_href="item/xhtml/p-001.xhtml",
        file_name="p-001",
    )

    _convert_txt_to_xhtml(
        units=[unit],
        translated_dir=translated_dir,
        xhtml_dir=xhtml_dir,
        parser=_ParserStub(original),
    )

    rebuilt_text = (xhtml_dir / "p-001.xhtml").read_text(encoding="utf-8")
    assert rebuilt_text.startswith(
        '<?xml version="1.0" encoding="UTF-8"?>\n<!DOCTYPE html>\n'
    )

    root = etree.fromstring(rebuilt_text.encode("utf-8"))
    ns = {"x": "http://www.w3.org/1999/xhtml"}
    body = root.find("x:body", ns)
    assert root.get("{http://www.w3.org/XML/1998/namespace}lang") == "ja"
    assert root.get("class") == "vrtl"
    assert body is not None
    assert body.get("class") == "p-text"

    section = body.find("x:section", ns)
    assert section is not None
    assert section.get("id") == "chapter-1"
    assert section.get("class") == "main"

    paragraphs = section.findall("x:p", ns)
    assert ["".join(p.itertext()) for p in paragraphs] == ["译文一", "译文二"]
    assert paragraphs[0].get("id") == "toc-001"
    assert paragraphs[0].get("class") == "indent"
    assert paragraphs[0].find("x:ruby", ns) is not None
    assert paragraphs[0].find("x:ruby/x:rt", ns) is not None
    emphasis = paragraphs[0].find("x:span", ns)
    assert emphasis is not None
    assert emphasis.get("class") == "em"

    image = section.find("x:div/x:img", ns)
    assert image is not None
    assert image.get("src") == "../image/ill.jpg"
    assert image.get("alt") == "插图"


def test_novel_xhtml_rebuild_regroups_inline_source_formatting_lines(
    tmp_path: Path,
) -> None:
    """Formatting newlines inside one paragraph must not become extra XHTML units."""
    from pdf2epub.html_translation.novel_extractor import NovelExtractor

    original = """<?xml version="1.0" encoding="UTF-8"?>
<html xmlns="http://www.w3.org/1999/xhtml">
<head><title>Original</title></head>
<body><p class="indent">原文前
<ruby>一<rt>いち</rt></ruby>
<span><span>中</span></span>尾</p></body>
</html>
"""
    source_dir = tmp_path / "novel_units"
    translated_dir = tmp_path / "translated_novel"
    xhtml_dir = tmp_path / "final_xhtml"
    source_dir.mkdir()
    translated_dir.mkdir()
    xhtml_dir.mkdir()

    source_text, _ = NovelExtractor(SimpleNamespace())._convert_xhtml_to_text(original)
    assert [line for line in source_text.splitlines() if line.strip()] == [
        "原文前",
        "一(いち)",
        "中尾",
    ]

    source_path = source_dir / "015_p-015.txt"
    source_path.write_text(source_text, encoding="utf-8")
    (translated_dir / source_path.name).write_text(
        "译文前\n译文一\n译文中尾",
        encoding="utf-8",
    )
    unit = SimpleNamespace(
        text_path=source_path,
        has_content=True,
        source_href="item/xhtml/p-015.xhtml",
        file_name="p-015",
    )

    _convert_txt_to_xhtml(
        units=[unit],
        translated_dir=translated_dir,
        xhtml_dir=xhtml_dir,
        parser=_ParserStub(original),
    )

    root = etree.fromstring((xhtml_dir / "p-015.xhtml").read_bytes())
    ns = {"x": "http://www.w3.org/1999/xhtml"}
    paragraph = root.find(".//x:p", ns)
    assert paragraph is not None
    assert "".join(paragraph.itertext()) == "译文前译文一译文中尾"
    assert paragraph.get("class") == "indent"
    assert paragraph.find("x:ruby", ns) is not None
    assert paragraph.find("x:span/x:span", ns) is not None


def test_novel_xhtml_rebuild_refuses_missing_translation_file(tmp_path: Path):
    source_dir = tmp_path / "novel_units"
    translated_dir = tmp_path / "translated_novel"
    xhtml_dir = tmp_path / "final_xhtml"
    source_dir.mkdir()
    translated_dir.mkdir()
    xhtml_dir.mkdir()
    source_path = source_dir / "001_page.txt"
    source_path.write_text("原文", encoding="utf-8")
    unit = SimpleNamespace(
        text_path=source_path,
        has_content=True,
        source_href="item/xhtml/page.xhtml",
        file_name="page",
    )

    with pytest.raises(FileNotFoundError, match="Missing translated novel unit"):
        _convert_txt_to_xhtml(
            units=[unit],
            translated_dir=translated_dir,
            xhtml_dir=xhtml_dir,
            parser=_ParserStub("<html><body><p>原文</p></body></html>"),
        )


def test_build_novel_epub_auto_enrichment_without_unbound_local(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Default auto metadata enrichment must not fail with UnboundLocalError on enrichment_outcome."""
    from pdf2epub.commands import novel as novel_commands

    output_dir = tmp_path / "output" / "Novel Book"
    (output_dir / "novel_units").mkdir(parents=True)
    (output_dir / "translated_novel").mkdir(parents=True)
    (output_dir / "input.epub").write_bytes(b"PK\x03\x04")
    (output_dir / "translated_metadata.json").write_text(
        '{"translated_title": "Translated Novel"}', encoding="utf-8"
    )
    context = SimpleNamespace(
        config={
            "title": "Novel Book",
            "metadata_enrichment": {"mode": "auto", "cover_policy": "preserve"},
        },
        book_title="Novel Book",
        output_dir=output_dir,
        config_path=tmp_path / "config.yaml",
    )
    monkeypatch.setattr(novel_commands, "load_book_context", lambda *_a, **_kw: context)
    monkeypatch.setattr(novel_commands, "translate_novel_validate_command", lambda *_a, **_kw: 0)
    monkeypatch.setattr(novel_commands, "_convert_txt_to_xhtml", lambda *_a, **_kw: 1)

    class _FakeParser:
        def __init__(self, _path: str) -> None:
            pass

    class _FakeExtractor:
        def __init__(self, _parser) -> None:
            pass

        def extract_all(self, _dir: Path):
            return [SimpleNamespace(has_content=True)]

    captured: dict[str, object] = {}

    class _FakeBuilder:
        def __init__(self, cfg) -> None:
            self.config = cfg
            captured["enrichment_outcome"] = cfg.enrichment_outcome

        def build(self) -> None:
            self.config.output_path.parent.mkdir(parents=True, exist_ok=True)
            self.config.output_path.write_bytes(b"PK\x03\x04")

    import pdf2epub.build_receipt as build_receipt_mod
    import pdf2epub.html_translation.builder as html_builder_mod
    import pdf2epub.html_translation.epub_parser as epub_parser_mod
    import pdf2epub.html_translation.novel_extractor as novel_extractor_mod
    import pdf2epub.metadata_enrichment.service as enrichment_service_mod

    monkeypatch.setattr(epub_parser_mod, "EPUBParser", _FakeParser)
    monkeypatch.setattr(novel_extractor_mod, "NovelExtractor", _FakeExtractor)
    monkeypatch.setattr(html_builder_mod, "HTMLEpubBuilder", _FakeBuilder)
    monkeypatch.setattr(
        enrichment_service_mod,
        "resolve_enrichment",
        lambda *_a, **_kw: {"status": "verified", "metadata": {"author": "Author"}, "verified_fields": ["author"]},
    )
    monkeypatch.setattr(
        build_receipt_mod,
        "write_build_receipt",
        lambda *args, **kwargs: captured.setdefault("receipt_outcome", kwargs.get("metadata_outcome")),
    )

    rc = novel_commands.build_novel_epub_command(SimpleNamespace(config=str(tmp_path / "config.yaml")))
    assert rc == 0
    assert captured["enrichment_outcome"] == {"status": "applied"}
    assert captured["receipt_outcome"] == {"status": "applied"}
