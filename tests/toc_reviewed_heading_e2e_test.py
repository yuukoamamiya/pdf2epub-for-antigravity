"""Synthetic review-to-EPUB contract for reviewed PDF headings and numbers."""
import json
import re
import shutil
import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path
from posixpath import dirname, join

from pdf2epub.build_epub import BuildEpubConfig, build_epub
from pdf2epub.refine.main import RefinedBreakdown
from pdf2epub.toc_model import TOC_TREE_SCHEMA_VERSION
from pdf2epub.toc_model import normalize_toc_source_label
from tests.toc_review_test_support import install_toc_review_contract


def _node(title: str, level: int, start_page: int, end_page: int, line: int, children=None):
    return {
        "title": title,
        "level": level,
        "start_page": start_page,
        "end_page": end_page,
        "children": children or [],
        "boundary_info": {"start_line": line, "heading_end_line": line + 1},
    }


def test_reviewed_pdf_structure_flows_through_refine_ownership_and_epub_zip(tmp_path: Path):
    output = tmp_path / "output"
    pages = output / "pages"
    pages.mkdir(parents=True)
    (tmp_path / "input.pdf").write_bytes(b"synthetic page source")

    page_text = {
        1: "# Part Alpha\n\n## Theory\n\n## <a id=\"author-anchor\"></a> Chapter Alpha [^1]\n\nIntroduction.\n\n1\nSection A1.\n",
        2: "2\nSection A2 uses 2 in the argument.\n\n2\nThis standalone numeral belongs to body text.\n\n2\n[^1]: Footnote attached to the chapter heading.\n",
        3: "# Part Beta\n\n## Chapter Beta\n\nIntroduction.\n\n1\nSection B1.\n",
        4: "2\nSection B2.\n\n4\n",
    }
    for page, content in page_text.items():
        (pages / f"page_{page:03d}.md").write_text(content, encoding="utf-8")
    # Include current visual sidecar evidence in the review snapshot.
    for page, label in ((1, "Part Alpha"), (2, "2"), (3, "Part Beta"), (4, "4")):
        sidecar = {
            "page_number": page,
            "coordinate_system": "normalized",
            "page_box": [0, 0, 1, 1],
            "blocks": [{"text": label, "label": "Page-Footer", "bbox": [0.1, 0.92, 0.9, 0.96]}],
        }
        (pages / f"page_{page:03d}.ocr.json").write_text(json.dumps(sidecar), encoding="utf-8")

    tree = {"schema_version": TOC_TREE_SCHEMA_VERSION, "chapters": [
        _node("Part Alpha Theory", 1, 1, 2, 1, [
            _node("Chapter Alpha", 2, 1, 2, 5, [
                _node("1", 3, 1, 1, 9),
                _node("2", 3, 2, 2, 1),
            ]),
        ]),
        _node("Part Beta", 1, 3, 4, 1, [
            _node("Chapter Beta", 2, 3, 4, 3, [
                _node("1", 3, 3, 3, 7),
                _node("2", 3, 4, 4, 1),
            ]),
        ]),
    ]}
    (output / "toc_tree.json").write_text(json.dumps(tree, ensure_ascii=False), encoding="utf-8")
    install_toc_review_contract(output)
    # The canonical text candidate is only the first line. The explicit
    # reviewed range binds both visible lines as one heading.
    tree_path = output / "toc_tree.json"
    reviewed_tree = json.loads(tree_path.read_text(encoding="utf-8"))
    reviewed_tree["chapters"][0]["boundary_info"]["heading_end_line"] = 4
    tree_path.write_text(json.dumps(reviewed_tree, ensure_ascii=False), encoding="utf-8")
    inventory = json.loads((output / "toc_heading_candidates.json").read_text(encoding="utf-8"))
    decisions_path = output / "toc_structure_decisions.json"
    decisions = json.loads(decisions_path.read_text(encoding="utf-8"))
    second_heading = next(
        row for row in inventory["candidates"]
        if row.get("page") == 1 and row.get("line") == 3
    )
    second_decision = next(
        row for row in decisions["candidate_decisions"]
        if row["candidate_id"] == second_heading["candidate_id"]
    )
    second_decision.update({
        "disposition": "toc_node",
        "node_id": reviewed_tree["chapters"][0]["node_id"],
        "binding_mode": "visible",
        "source_range": {"start_line": 1, "heading_end_line": 4},
    })
    decisions_path.write_text(json.dumps(decisions, ensure_ascii=False), encoding="utf-8")

    inventory = json.loads((output / "toc_heading_candidates.json").read_text(encoding="utf-8"))
    decisions_path = output / "toc_structure_decisions.json"
    decisions = json.loads(decisions_path.read_text(encoding="utf-8"))
    page_number_locations = {(2, 7), (4, 4)}
    for decision in decisions["candidate_decisions"]:
        candidate = next(row for row in inventory["candidates"] if row["candidate_id"] == decision["candidate_id"])
        if (candidate.get("page"), candidate.get("line")) in page_number_locations:
            decision["disposition"] = "page_number"
            decision.pop("node_id", None)
    decisions_path.write_text(json.dumps(decisions, ensure_ascii=False), encoding="utf-8")
    assert sum(row["disposition"] == "page_number" for row in decisions["candidate_decisions"]) == 2

    units = RefinedBreakdown(config={}, max_tokens=100_000).process_from_toc(
        tmp_path / "input.pdf", output, "Synthetic", pages_dir=pages
    )
    progress = json.loads((output / "ocr_markdown" / "tree_progress.json").read_text(encoding="utf-8"))
    assert progress["units"]
    refined_text = "\n".join(path.read_text(encoding="utf-8") for path in (output / "ocr_markdown").glob("*.md") if path.name != "tree_progress.json")
    assert refined_text.count("# Part Alpha Theory") == 1
    assert "### 1" in refined_text and "### 2" in refined_text
    assert "Section A2 uses 2 in the argument." in refined_text
    assert "This standalone numeral belongs to body text." in refined_text
    assert "\n2\nThis standalone numeral belongs to body text." in refined_text
    assert "[^1]: Footnote attached" in refined_text
    assert "### 2\n\nThis standalone numeral belongs to body text." not in refined_text
    assert "[^1]" in refined_text and "[^1]: Footnote attached" in refined_text
    assert "author-anchor" in refined_text

    polished = output / "polished_markdown"
    polished.mkdir()
    for source in (output / "ocr_markdown").glob("*.md"):
        if source.name != "tree_progress.json":
            shutil.copyfile(source, polished / source.name)
    epub = build_epub(BuildEpubConfig(
        book_title="Synthetic Reviewed Structure",
        output_dir=output,
        markdown_dir=polished,
        toc_tree_path=output / "toc_tree.json",
        cover_image=output / "missing-cover.jpg",
        config={"footnotes": {"mode": "footnotes"}},
    ))
    assert epub.is_file()
    with zipfile.ZipFile(epub) as archive:
        names = archive.namelist()
        ncx_name = next(name for name in names if name.endswith(".ncx"))
        ncx_root = ET.fromstring(archive.read(ncx_name))
        ns = {"n": "http://www.daisy.org/z3986/2005/ncx/"}
        nav_map = ncx_root.find("n:navMap", ns)
        assert nav_map is not None
        top_points = nav_map.findall("n:navPoint", ns)

        def label(point):
            node = point.find("n:navLabel/n:text", ns)
            return "".join(node.itertext()).strip() if node is not None else ""

        def children(point):
            return point.findall("n:navPoint", ns)

        assert [label(point) for point in top_points] == ["Part Alpha Theory", "Part Beta"]
        assert [[label(point) for point in children(part)] for part in top_points] == [
            ["Chapter Alpha"], ["Chapter Beta"]
        ]
        assert [[label(section) for chapter in children(part) for section in children(chapter)]
                for part in top_points] == [["1", "2"], ["1", "2"]]
        assert sum(1 for _ in nav_map.iter("{%s}navPoint" % ns["n"])) == 8

        expected_tags = {1: "h1", 2: "h2", 3: "h3"}
        def verify_targets(points, level):
            for point in points:
                content = point.find("n:content", ns)
                assert content is not None and content.get("src")
                source = content.get("src")
                path, _, fragment = source.partition("#")
                assert fragment, f"visible NCX node lacks a fragment target: {label(point)}"
                xhtml_path = join(dirname(ncx_name), path)
                root = ET.fromstring(archive.read(xhtml_path))
                targets = [element for element in root.iter() if element.get("id") == fragment]
                assert len(targets) == 1, f"NCX fragment is missing or duplicated: {source}"
                target = targets[0]
                parent_map = {child: parent for parent in root.iter() for child in parent}
                heading = target
                while (heading is not None
                       and heading.tag.rsplit("}", 1)[-1] not in {"h1", "h2", "h3", "h4", "h5", "h6"}):
                    heading = parent_map.get(heading)
                assert heading is not None
                assert heading.tag.rsplit("}", 1)[-1] == expected_tags[level]
                heading_label = re.sub(r"\s+\[\d+\]$", "", " ".join("".join(heading.itertext()).split()) )
                assert normalize_toc_source_label(
                    heading_label, markdown_source=False
                ) == normalize_toc_source_label(label(point), markdown_source=False)
                verify_targets(children(point), min(level + 1, 3))
        verify_targets(top_points, 1)

        xhtml_roots = [ET.fromstring(archive.read(name)) for name in names if name.endswith((".xhtml", ".html")) and not name.endswith("toc.html")]
        ids = [element.get("id") for root in xhtml_roots for element in root.iter() if element.get("id")]
        assert "author-anchor" in ids
        xhtml_text = " ".join(" ".join("".join(element.itertext()).split())
                               for root in xhtml_roots for element in root.iter())
        assert "Footnote attached to the chapter heading." in xhtml_text
        body_numeral_paragraphs = [
            element for root in xhtml_roots for element in root.iter()
            if element.tag.rsplit("}", 1)[-1] == "p"
            and " ".join("".join(element.itertext()).split())
            == "2 This standalone numeral belongs to body text."
        ]
        assert len(body_numeral_paragraphs) == 1
        assert body_numeral_paragraphs[0].tag.rsplit("}", 1)[-1] == "p"
        paragraphs = [
            " ".join("".join(element.itertext()).split())
            for root in xhtml_roots for element in root.iter()
            if element.tag.rsplit("}", 1)[-1] == "p"
        ]
        assert "2" not in paragraphs and "4" not in paragraphs

        backrefs = [
            element.get("href") for root in xhtml_roots for element in root.iter()
            if element.tag.rsplit("}", 1)[-1] == "a"
            and "footnote-backref" in element.get("class", "")
        ]
        forward_refs = [
            element.get("href") for root in xhtml_roots for element in root.iter()
            if element.tag.rsplit("}", 1)[-1] == "a"
            and "footnote-ref" in element.get("class", "")
        ]
        all_ids = set(ids)
        assert len(backrefs) == 1 and backrefs[0] and backrefs[0].split("#")[-1] in all_ids
        assert len(forward_refs) == 1 and forward_refs[0] and forward_refs[0].split("#")[-1] in all_ids

        html_toc = ET.fromstring(archive.read("text/toc.html"))
        def toc_outline(listing):
            rows = []
            for item in listing.findall("./{*}li"):
                link = item.find("./{*}a")
                assert link is not None
                nested = item.find("./{*}ul")
                rows.append((" ".join("".join(link.itertext()).split()), link.get("href"),
                             toc_outline(nested) if nested is not None else []))
            return rows
        root_list = html_toc.find(".//{*}ul")
        assert root_list is not None
        html_outline = toc_outline(root_list)
        assert [row[0] for row in html_outline] == ["Part Alpha Theory", "Part Beta"]
        assert [[chapter[0] for chapter in part[2]] for part in html_outline] == [
            ["Chapter Alpha"], ["Chapter Beta"]
        ]
        assert [[section[0] for chapter in part[2] for section in chapter[2]]
                for part in html_outline] == [["1", "2"], ["1", "2"]]
        def href_tree(points):
            return [(point.find("n:content", ns).get("src").rsplit("/", 1)[-1], href_tree(children(point))) for point in points]
        def html_href_tree(rows):
            return [(row[1].rsplit("/", 1)[-1], html_href_tree(row[2])) for row in rows]
        assert html_href_tree(html_outline) == href_tree(top_points)
