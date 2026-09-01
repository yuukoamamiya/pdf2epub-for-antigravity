from pathlib import Path

import pytest

from pdf2epub.build_epub import process_chapter_content
from pdf2epub.epub.footnotes import (
    FootnoteGraphError,
    FootnoteManager,
    FootnoteStyle,
    inspect_footnote_graph,
    validate_footnote_graph,
)
from pdf2epub.epub.footnotes.content_index import ContentAddressIndex
from pdf2epub.markdown_to_html import convert_markdown_to_html


def _entry(unit_id: str, *part_files: Path, children=None, entry_type=None) -> dict:
    result = {"unit_id": unit_id}
    if part_files:
        result["file_path"] = part_files[0]
        result["part_files"] = list(part_files)
    if children is not None:
        result["children"] = children
    if entry_type:
        result["type"] = entry_type
    return result


def _render(structure: list[dict], manager: FootnoteManager) -> dict[str, str]:
    html_files: dict[str, str] = {}

    def walk(entries: list[dict]) -> None:
        for entry in entries:
            part_files = entry.get("part_files") or []
            for part_index, part_file in enumerate(part_files, 1):
                content = Path(part_file).read_text(encoding="utf-8")
                processed = process_chapter_content(
                    entry.get("title", entry["unit_id"]),
                    entry.get("level", 1),
                    content,
                    is_first_part=(part_index == 1),
                )
                html = convert_markdown_to_html(
                    processed,
                    "Book",
                    standalone=False,
                    footnote_manager=manager,
                    source_chapter=Path(part_file).stem,
                )
                html_name = (
                    f"{entry['unit_id']}_part{part_index}.html"
                    if len(part_files) > 1
                    else f"{entry['unit_id']}.html"
                )
                html_files[html_name] = html
            walk(entry.get("children", []))

    walk(structure)
    return html_files


def test_content_address_index_uses_structure_for_leaf_ancestry_and_html_names(
    tmp_path: Path,
) -> None:
    first = tmp_path / "chapter_9.part3.part1.md"
    second = tmp_path / "chapter_9.part3.part2.md"
    structure = [
        _entry(
            "chapter_1",
            children=[
                _entry(
                    "chapter_1.1",
                    children=[_entry("chapter_1.1.1", first, second)],
                )
            ],
        )
    ]

    index = ContentAddressIndex.from_structure(structure)

    assert index.ancestry_for_source(first.stem) == (
        "chapter_1",
        "chapter_1.1",
        "chapter_1.1.1",
    )
    assert index.nearest_scope(first.stem, {"chapter_1", "chapter_1.1"}) == "chapter_1.1"
    assert index.html_for_source(first.stem) == "chapter_1.1.1_part1.html"
    assert index.html_for_source(second.stem) == "chapter_1.1.1_part2.html"


def test_structure_limits_scanning_to_files_that_will_be_built(tmp_path: Path) -> None:
    included = tmp_path / "chapter_1.md"
    stale = tmp_path / "chapter_99.md"
    included.write_text("Body [^1].\n\n[^1]: Definition.\n", encoding="utf-8")
    stale.write_text("Stale marker [^99].\n", encoding="utf-8")
    structure = [_entry("chapter_1", included)]

    manager = FootnoteManager(tmp_path, epub_structure=structure)

    assert "1" in manager.references
    assert "99" not in manager.references


def test_local_notes_cross_refined_siblings_form_a_valid_graph(tmp_path: Path) -> None:
    early = tmp_path / "chapter_1.1.md"
    late = tmp_path / "chapter_1.2.md"
    early.write_text("Early note [^1].\n", encoding="utf-8")
    late.write_text(
        "Late note [^2].\n\n[^1]: Early definition.\n\n[^2]: Late definition.\n",
        encoding="utf-8",
    )
    structure = [
        _entry(
            "chapter_1",
            children=[_entry("chapter_1.1", early), _entry("chapter_1.2", late)],
        )
    ]

    manager = FootnoteManager(tmp_path, epub_structure=structure)
    html_files = _render(structure, manager)
    report = validate_footnote_graph(html_files)

    assert manager.get_style() == FootnoteStyle.LOCAL
    assert manager.get_local_group_id(early.stem) == "chapter_1"
    assert report["forward_hrefs"] == 2
    assert report["backref_hrefs"] == 2


def test_unbalanced_local_occurrences_degrade_to_visible_unlinked_refs(
    tmp_path: Path,
) -> None:
    first = tmp_path / "chapter_1.part1.md"
    second = tmp_path / "chapter_1.part2.md"
    first.write_text("One [^1]. Two [^1].\n", encoding="utf-8")
    second.write_text("[^1]: Only one definition.\n", encoding="utf-8")
    structure = [_entry("chapter_1", first, second)]

    manager = FootnoteManager(tmp_path, epub_structure=structure)
    report = validate_footnote_graph(_render(structure, manager))

    assert report["forward_hrefs"] == 1
    assert report["unlinked_sup_count"] == 1


def test_global_occurrence_mapping_without_semantic_sections_is_valid(
    tmp_path: Path,
) -> None:
    body = tmp_path / "chapter_1.md"
    notes = tmp_path / "chapter_2.md"
    body.write_text("One [^1]. Two [^1].\n", encoding="utf-8")
    notes.write_text(
        "[^1]: First definition.\n\n[^1]: Second definition.\n",
        encoding="utf-8",
    )
    structure = [_entry("chapter_1", body), _entry("chapter_2", notes)]

    manager = FootnoteManager(tmp_path, auto_global=True, epub_structure=structure)
    report = validate_footnote_graph(_render(structure, manager))

    assert report["forward_hrefs"] == 2
    assert report["backref_hrefs"] == 0


def test_no_colon_definitions_are_limited_to_structural_notes_scope(
    tmp_path: Path,
) -> None:
    body = tmp_path / "chapter_1.md"
    notes = tmp_path / "chapter_2.md"
    unrelated = tmp_path / "chapter_3.md"
    body.write_text("Body [^1].\n", encoding="utf-8")
    notes.write_text("[^1] Legacy definition.\n", encoding="utf-8")
    unrelated.write_text("[^2] This remains a reference.\n", encoding="utf-8")
    structure = [
        _entry("chapter_1", body),
        _entry("chapter_2", notes, entry_type="notes"),
        _entry("chapter_3", unrelated),
    ]

    manager = FootnoteManager(tmp_path, auto_global=True, epub_structure=structure)

    assert manager.definitions["1"][0].chapter == notes.stem
    assert "2" not in manager.definitions
    assert manager.references["2"][0].chapter == unrelated.stem


def test_page_note_keys_preserve_display_while_using_normalized_definition(
    tmp_path: Path,
) -> None:
    body = tmp_path / "chapter_1.md"
    notes = tmp_path / "chapter_2.md"
    body.write_text("Page note [^197n67].\n", encoding="utf-8")
    notes.write_text("[^67]: Definition.\n", encoding="utf-8")
    structure = [_entry("chapter_1", body), _entry("chapter_2", notes)]

    manager = FootnoteManager(tmp_path, auto_global=True, epub_structure=structure)
    html_files = _render(structure, manager)
    report = validate_footnote_graph(html_files)

    assert "[197n67]" in html_files["chapter_1.html"]
    assert report["forward_hrefs"] == 1


def test_replaced_source_heading_markers_do_not_create_phantom_backlinks(
    tmp_path: Path,
) -> None:
    body = tmp_path / "chapter_1.part1.md"
    notes = tmp_path / "chapter_1.part2.md"
    body.write_text(
        "# Raw title [^1] [^2]\n\n"
        "Body [^1]. Local [^9].\n\n[^9]: Local definition.\n",
        encoding="utf-8",
    )
    notes.write_text(
        "[^1]: Body definition.\n\n[^2]: Replaced-heading definition.\n",
        encoding="utf-8",
    )
    structure = [
        {
            **_entry("chapter_1", body, notes),
            "title": "TOC title",
            "level": 1,
        }
    ]

    manager = FootnoteManager(tmp_path, epub_structure=structure)
    report = validate_footnote_graph(_render(structure, manager))

    assert report["forward_hrefs"] == 2
    assert report["backref_hrefs"] == 2
    assert report["unlinked_sup_count"] == 0


def test_definition_text_markers_are_not_scanned_as_body_references(
    tmp_path: Path,
) -> None:
    chapter = tmp_path / "chapter_1.md"
    chapter.write_text(
        "Body [^1].\n\n"
        "[^1]: *Styled* $\\frac{x}{y}$ mentions literal [^2] and <Book>.\n\n"
        "[^2]: Orphan definition.\n",
        encoding="utf-8",
    )
    structure = [_entry("chapter_1", chapter)]

    manager = FootnoteManager(tmp_path, epub_structure=structure)
    html_files = _render(structure, manager)
    report = validate_footnote_graph(html_files)

    assert "2" not in manager.references
    assert "<em>Styled</em>" in html_files["chapter_1.html"]
    assert "<math" in html_files["chapter_1.html"]
    assert "<mfrac>" in html_files["chapter_1.html"]
    assert "&lt;Book&gt;" in html_files["chapter_1.html"]
    assert report["forward_hrefs"] == 1
    assert report["backref_hrefs"] == 1


def test_reconfiguration_rescans_markdown_and_rebuilds_structural_state(
    tmp_path: Path,
) -> None:
    chapter = tmp_path / "chapter_1.md"
    chapter.write_text("Body [^1].\n\n[^1]: Definition.\n", encoding="utf-8")
    structure = [_entry("chapter_1", chapter)]
    manager = FootnoteManager(tmp_path, epub_structure=structure)

    chapter.write_text("[^1]: Definition without a reference.\n", encoding="utf-8")
    manager.configure_from_structure(structure)
    report = validate_footnote_graph(_render(structure, manager))

    assert manager.references == {}
    assert report["backref_hrefs"] == 0


@pytest.mark.parametrize(
    "html_files",
    [
        {
            "one.html": '<sup id="fnref-one-1"><a href="two.html#fn-1">[1]</a></sup>',
            "two.html": "<p>No target</p>",
        },
        {
            "one.html": '<div id="fn-1"></div><div id="fn-1"></div>',
        },
        {
            "one.html": '<div id="fn:legacy"></div>',
        },
    ],
)
def test_graph_validator_rejects_broken_targets_and_duplicate_ids(
    html_files: dict[str, str],
) -> None:
    with pytest.raises(FootnoteGraphError):
        validate_footnote_graph(html_files)


def test_graph_inspection_allows_explicit_unlinked_reference() -> None:
    report = inspect_footnote_graph(
        {"one.html": '<p><sup id="fnref-one-1">[1]</sup></p>'}
    )

    assert report["unlinked_sup_count"] == 1
    assert report["forward_broken_count"] == 0
    assert report["backref_broken_count"] == 0
