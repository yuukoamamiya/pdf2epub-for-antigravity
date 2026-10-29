import json
import zipfile
import xml.etree.ElementTree as ET

import pytest

from pdf2epub.build_epub import BuildEpubConfig, build_epub
from pdf2epub.refine.main import RefinedBreakdown
from pdf2epub.markdown_handoff import prepare_markdown_subagent
from pdf2epub.toc_translation_workflow import (
    build_toc_heading_contexts,
    prepare_toc_translation_subagent,
    validate_toc_heading_bindings,
    validate_toc_translation_subagent,
)
from pdf2epub.toc_model import TOC_TREE_SCHEMA_VERSION
from tests.toc_review_test_support import install_toc_review_contract


def test_review_to_refine_to_translation_handoff_and_epub_roundtrip(tmp_path):
    output = tmp_path / "output"
    pages = output / "pages"
    pages.mkdir(parents=True)
    (output / "input.pdf").write_bytes(b"synthetic PDF bytes")
    page_texts = [
        "*Parent*\n\nParent body retained.\n",
        "## Child\n\nChild body retained.\n",
        "### Grandchild\n\nGrandchild body retained.\n",
    ]
    for index, text in enumerate(page_texts, 1):
        (pages / f"page_{index:03d}.md").write_text(text, encoding="utf-8")
    tree = {"schema_version": TOC_TREE_SCHEMA_VERSION, "chapters": [{
        "title": "Parent", "level": 1, "start_page": 1, "end_page": 3,
        "boundary_info": {"start_line": 1}, "children": [{
            "title": "Child", "level": 2, "start_page": 2, "end_page": 3,
            "boundary_info": {"start_line": 1}, "children": [{
                "title": "Grandchild", "level": 3, "start_page": 3, "end_page": 3,
                "boundary_info": {"start_line": 1},
            }],
        }],
    }]}
    (output / "toc_tree.json").write_text(json.dumps(tree), encoding="utf-8")
    install_toc_review_contract(output, pages)

    units = RefinedBreakdown(config={}, max_tokens=8000).process_from_toc(
        output / "input.pdf", output, "Synthetic Book"
    )
    assert units
    progress = json.loads((output / "ocr_markdown" / "tree_progress.json").read_text(encoding="utf-8"))
    assert all(unit.get("node_id") for unit in progress["units"])
    source_dir = output / "ocr_markdown"
    source_toc = json.loads((output / "toc_tree.json").read_text(encoding="utf-8"))
    toc_paths = prepare_toc_translation_subagent(output, "English", "Chinese")
    result = json.loads(toc_paths["template"].read_text(encoding="utf-8"))
    translated_titles = ["父章节", "子章节", "孙章节"]
    for entry, translated_title in zip(result["entries"], translated_titles):
        entry["translated"] = translated_title
    (output / "toc_translation_result.json").write_text(
        json.dumps(result, ensure_ascii=False), encoding="utf-8"
    )
    assert validate_toc_translation_subagent(output)["valid"]
    contexts = build_toc_heading_contexts(output, source_dir=source_dir)
    assert contexts
    markdown_dir = output / "translated"
    prepare_markdown_subagent(
        output, "translate", source_dir, markdown_dir, "English", "Chinese",
        heading_contexts=contexts,
    )
    validated_dir = markdown_dir / "validated"
    validated_dir.mkdir(parents=True)
    replacements = {"Parent": "父章节", "Child": "子章节", "Grandchild": "孙章节"}
    for unit in progress["units"]:
        filename = unit.get("file")
        if filename:
            text = (source_dir / filename).read_text(encoding="utf-8")
            for source_title, target_title in replacements.items():
                text = text.replace(f"# {source_title}\n", f"# {target_title}\n")
                text = text.replace(f"## {source_title}\n", f"## {target_title}\n")
                text = text.replace(f"### {source_title}\n", f"### {target_title}\n")
                if source_title == "Parent":
                    text = text.replace("*Parent*\n", "*父章节*\n")
            (validated_dir / filename).write_text(text, encoding="utf-8")
        for filename in unit.get("part_files", []):
            text = (source_dir / filename).read_text(encoding="utf-8")
            for source_title, target_title in replacements.items():
                text = text.replace(f"# {source_title}\n", f"# {target_title}\n")
                text = text.replace(f"## {source_title}\n", f"## {target_title}\n")
                text = text.replace(f"### {source_title}\n", f"### {target_title}\n")
                if source_title == "Parent":
                    text = text.replace("*Parent*\n", "*父章节*\n")
            (validated_dir / filename).write_text(text, encoding="utf-8")
    assert validate_toc_heading_bindings(output)["valid"]

    epub_path = build_epub(BuildEpubConfig(
        book_title="Synthetic Book", output_dir=output, markdown_dir=validated_dir,
        toc_tree_path=output / "toc_tree.json", translated=True,
        target_language="Chinese", config={"footnotes": {"mode": "disabled"}},
    ))
    assert epub_path.is_file()
    with zipfile.ZipFile(epub_path) as archive:
        names = archive.namelist()
        ncx_name = next(name for name in names if name.endswith("toc.ncx"))
        ncx = ET.fromstring(archive.read(ncx_name))
    rendered_dir = output / "epub_build" / "text"
    xhtml_names = list(rendered_dir.glob("*.html")) + list(rendered_dir.glob("*.xhtml"))
    rendered = "\n".join(path.read_text(encoding="utf-8") for path in xhtml_names)
    for body in ("Parent body retained.", "Child body retained.", "Grandchild body retained."):
        assert body in rendered
    assert "toc-" in rendered
    ns = {"n": "http://www.daisy.org/z3986/2005/ncx/"}
    root_points = ncx.findall("n:navMap/n:navPoint", ns)
    root_point = next(point for point in root_points if point.find("n:navLabel/n:text", ns).text != "目录")
    child_point = root_point.find("n:navPoint", ns)
    grandchild_point = child_point.find("n:navPoint", ns)
    points = [root_point, child_point, grandchild_point]
    labels = [node.find("n:navLabel/n:text", ns).text for node in points]
    assert labels == ["父章节", "子章节", "孙章节"]
    for point in points:
        source = point.find("n:content", ns).attrib["src"].split("#", 1)
        target_file, fragment = source
        page = output / "epub_build" / target_file
        if not page.is_file():
            page = output / "epub_build" / "text" / target_file
        assert fragment in page.read_text(encoding="utf-8")
    assert xhtml_names

    leaf_file = next((validated_dir / name for unit in progress["units"] for name in (unit.get("part_files") or [unit.get("file")]) if name and "孙章节" in (validated_dir / name).read_text(encoding="utf-8")), None)
    assert leaf_file is not None
    leaf_file.write_text("Only body retained; required heading removed.\n", encoding="utf-8")
    with pytest.raises(ValueError, match="Required visible TOC heading"):
        build_epub(BuildEpubConfig(
            book_title="Synthetic Book", output_dir=output, markdown_dir=validated_dir,
            toc_tree_path=output / "toc_tree.json", translated=True,
            target_language="Chinese", config={"footnotes": {"mode": "disabled"}},
        ))
