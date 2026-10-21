import json

from pdf2epub.refine.main import RefinedBreakdown
from pdf2epub.refine.page_merger import PageMerger
from pdf2epub.refine.toc_heading_recovery import (
    collect_toc_heading_recoveries,
    toc_heading_recovery_fingerprint,
)
from pdf2epub.refine.toc_tree import TOCNode


def _write_page(pages_dir, number, markdown, html=None, sidecar=None):
    pages_dir.mkdir(parents=True, exist_ok=True)
    (pages_dir / f"page_{number:03d}.md").write_text(markdown, encoding="utf-8")
    if html is not None:
        (pages_dir / f"page_{number:03d}.html").write_text(html, encoding="utf-8")
    if sidecar is not None:
        (pages_dir / f"page_{number:03d}.ocr.json").write_text(
            json.dumps(sidecar, ensure_ascii=False),
            encoding="utf-8",
        )


def _split_header_html(body="Body paragraph."):
    return (
        '<div data-label="Page-Header">8</div>'
        '<div data-label="Page-Header">Mobilized</div>'
        f'<div data-label="Text"><p>{body}</p></div>'
    )


def _visual_sidecar(page_number=184):
    return {
        "page_number": page_number,
        "blocks": [
            {"label": "Page-Header"},
            {"label": "Page-Header"},
            {"label": "Text"},
        ],
    }


def test_recovers_title_split_across_page_header_blocks(tmp_path):
    pages_dir = tmp_path / "pages"
    _write_page(
        pages_dir,
        184,
        "Body paragraph.",
        html=_split_header_html(),
        sidecar=_visual_sidecar(),
    )
    node = TOCNode(title="8. Mobilized", level=2, start_page=184, end_page=184)

    recoveries, report = collect_toc_heading_recoveries(
        [node],
        page_markdown_dir=pages_dir,
        evidence_dir=pages_dir,
    )

    assert report["status"] == "validated"
    assert len(recoveries[184]) == 1
    insertion = recoveries[184][0]
    assert insertion["level"] == 2
    assert insertion["block_text"] == "8 Mobilized"
    assert insertion["evidence"]["file"] == "page_184.html"

    merger = PageMerger()
    merger.set_toc_heading_recoveries(recoveries)
    content = merger.merge_node_content(node, pages_dir)

    assert "## 8. Mobilized" in content
    assert "Body paragraph." in content
    assert "8. Mobilized" not in (pages_dir / "page_184.md").read_text(encoding="utf-8")


def test_skips_title_already_present_in_page_markdown(tmp_path):
    pages_dir = tmp_path / "pages"
    _write_page(
        pages_dir,
        184,
        "## 8. Mobilized\n\nBody paragraph.",
        html=_split_header_html(),
        sidecar=_visual_sidecar(),
    )
    node = TOCNode(title="8. Mobilized", level=2, start_page=184, end_page=184)

    recoveries, report = collect_toc_heading_recoveries(
        [node],
        page_markdown_dir=pages_dir,
        evidence_dir=pages_dir,
    )

    assert recoveries == {}
    assert report["status"] == "no_recovery"
    assert report["skipped"][0]["reason"] == "title_already_present"


def test_skips_when_no_html_evidence(tmp_path):
    pages_dir = tmp_path / "pages"
    _write_page(pages_dir, 184, "Body paragraph.", sidecar=_visual_sidecar())
    node = TOCNode(title="8. Mobilized", level=2, start_page=184, end_page=184)

    recoveries, report = collect_toc_heading_recoveries(
        [node],
        page_markdown_dir=pages_dir,
        evidence_dir=pages_dir,
    )

    assert recoveries == {}
    assert report["skipped"][0]["reason"] == "no_page_evidence"


def test_skips_native_text_sidecar(tmp_path):
    pages_dir = tmp_path / "pages"
    sidecar = _visual_sidecar()
    sidecar["source_kind"] = "native_text"
    _write_page(
        pages_dir,
        184,
        "Body paragraph.",
        html=_split_header_html(),
        sidecar=sidecar,
    )
    node = TOCNode(title="8. Mobilized", level=2, start_page=184, end_page=184)

    recoveries, report = collect_toc_heading_recoveries(
        [node],
        page_markdown_dir=pages_dir,
        evidence_dir=pages_dir,
    )

    assert recoveries == {}
    assert report["skipped"][0]["reason"] == "native_sidecar"


def test_start_line_inserts_heading_before_requested_line(tmp_path):
    pages_dir = tmp_path / "pages"
    _write_page(pages_dir, 1, "Intro line.\nBody line.\nTail line.")
    node = TOCNode(title="Recovered Heading", level=2, start_page=1, end_page=1)
    merger = PageMerger()
    merger.set_toc_heading_recoveries(
        {
            1: [
                {
                    "page": 1,
                    "title": "Recovered Heading",
                    "level": 2,
                    "start_line": 2,
                }
            ]
        }
    )

    content = merger.merge_node_content(node, pages_dir)

    assert content.index("Intro line.") < content.index("## Recovered Heading")
    assert content.index("## Recovered Heading") < content.index("Body line.")


def test_skips_recovered_heading_when_start_line_outside_fragment(tmp_path):
    pages_dir = tmp_path / "pages"
    _write_page(pages_dir, 1, "Intro line.\nBody line.\nTail line.")
    node = TOCNode(
        title="Fragment Heading",
        level=2,
        start_page=1,
        end_page=1,
        boundary_info={"start_line": 2},
    )
    merger = PageMerger()
    merger.set_toc_heading_recoveries(
        {
            1: [
                {
                    "page": 1,
                    "title": "Before Fragment",
                    "level": 2,
                    "start_line": 1,
                },
                {
                    "page": 1,
                    "title": "After Fragment",
                    "level": 2,
                    "start_line": 99,
                },
            ]
        }
    )

    content = merger.merge_node_content(node, pages_dir)

    assert "## Before Fragment" not in content
    assert "## After Fragment" not in content
    assert "Body line." in content
    assert "Tail line." in content


def test_does_not_duplicate_heading_already_in_fragment(tmp_path):
    pages_dir = tmp_path / "pages"
    _write_page(pages_dir, 1, "## 8 Mobilized\n\nBody line.")
    node = TOCNode(title="8. Mobilized", level=2, start_page=1, end_page=1)
    merger = PageMerger()
    merger.set_toc_heading_recoveries(
        {
            1: [
                {
                    "page": 1,
                    "title": "8. Mobilized",
                    "level": 2,
                    "start_line": None,
                }
            ]
        }
    )

    content = merger.merge_node_content(node, pages_dir)

    heading_lines = [
        line for line in content.splitlines() if line.lstrip().startswith("#")
    ]
    assert len(heading_lines) == 1
    assert "Body line." in content


def test_reads_body_from_corrected_pages_and_evidence_from_pages(tmp_path):
    corrected_dir = tmp_path / "ocr_corrected_pages" / "validated"
    evidence_dir = tmp_path / "pages"
    _write_page(corrected_dir, 184, "Corrected body paragraph.")
    _write_page(
        evidence_dir,
        184,
        "Raw body paragraph.",
        html=_split_header_html("Raw body paragraph."),
        sidecar=_visual_sidecar(),
    )
    node = TOCNode(title="8. Mobilized", level=2, start_page=184, end_page=184)

    recoveries, report = collect_toc_heading_recoveries(
        [node],
        page_markdown_dir=corrected_dir,
        evidence_dir=evidence_dir,
    )

    assert report["status"] == "validated"
    assert len(recoveries[184]) == 1

    merger = PageMerger()
    merger.set_toc_heading_recoveries(recoveries)
    content = merger.merge_node_content(node, corrected_dir)

    assert "## 8. Mobilized" in content
    assert "Corrected body paragraph." in content
    assert "Raw body paragraph." not in content


def test_report_structure_and_fingerprint_track_recoveries(tmp_path):
    pages_dir = tmp_path / "pages"
    _write_page(
        pages_dir,
        184,
        "Body paragraph.",
        html=_split_header_html(),
        sidecar=_visual_sidecar(),
    )
    node = TOCNode(title="8. Mobilized", level=2, start_page=184, end_page=184)

    recoveries, report = collect_toc_heading_recoveries(
        [node],
        page_markdown_dir=pages_dir,
        evidence_dir=pages_dir,
    )

    assert set(
        [
            "schema_version",
            "status",
            "recovered",
            "skipped",
            "page_markdown_fingerprint",
            "evidence_pages_fingerprint",
        ]
    ).issubset(report)
    assert report["recovered"][0]["title"] == "8. Mobilized"
    assert toc_heading_recovery_fingerprint(recoveries) != toc_heading_recovery_fingerprint({})


def test_refined_breakdown_writes_report_and_checkpoint_fingerprint(tmp_path):
    pages_dir = tmp_path / "pages"
    _write_page(
        pages_dir,
        184,
        "Body paragraph.",
        html=_split_header_html(),
        sidecar=_visual_sidecar(),
    )
    (tmp_path / "toc_tree.json").write_text(
        json.dumps(
            {
                "chapters": [
                    {
                        "title": "8. Mobilized",
                        "level": 2,
                        "start_page": 184,
                        "end_page": 184,
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    units = RefinedBreakdown(config={}, max_tokens=8000).process_from_toc(
        tmp_path / "input.pdf", tmp_path, "Book"
    )

    assert [unit["unit_id"] for unit in units] == ["chapter_1"]
    report = json.loads(
        (tmp_path / "toc_heading_recovery.json").read_text(encoding="utf-8")
    )
    assert report["status"] == "validated"
    assert report["toc_sha256"]
    progress = json.loads(
        (tmp_path / "ocr_markdown" / "tree_progress.json").read_text(encoding="utf-8")
    )
    assert "toc_heading_recovery_sha256" in progress["fingerprint"]
    assert progress["fingerprint"]["page_merger_policy"] == (
        "repeated-edge-header-folio-normalization-v2"
    )
    content = (tmp_path / "ocr_markdown" / "chapter_1.md").read_text(encoding="utf-8")
    assert "## 8. Mobilized" in content
