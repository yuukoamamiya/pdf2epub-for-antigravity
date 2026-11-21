"""Coverage for the pure ChapterIdentity filename model."""

from pathlib import Path

from pdf2epub.chapter_identity import (
    ChapterIdentity,
    discover_parts,
    group_by_base,
    is_valid_chapter_file,
    part_path,
    strip_part_suffix,
)


def test_parse_numbered_and_part_names():
    assert ChapterIdentity.parse("chapter_5.md") == ChapterIdentity("chapter", "5", None)
    assert ChapterIdentity.parse("chapter_7.1.1.part2") == ChapterIdentity(
        "chapter", "7.1.1", 2
    )
    assert ChapterIdentity.parse("unit_001") == ChapterIdentity("unit", "001", None)
    assert ChapterIdentity.parse("front_matter") == ChapterIdentity(
        "front_matter", None, None
    )
    assert ChapterIdentity.parse("back_matter.part1.md") == ChapterIdentity(
        "back_matter", None, 1
    )


def test_parse_rejects_unrecognized_names():
    assert ChapterIdentity.parse("random.txt") is None
    assert ChapterIdentity.parse("chapter_") is None
    assert ChapterIdentity.parse("preface.md") is None
    assert is_valid_chapter_file("chapter_3.md") is True
    assert is_valid_chapter_file("preface.md") is False


def test_names_and_flags():
    identity = ChapterIdentity.parse("chapter_7.1.1.part2")
    assert identity.base_name == "chapter_7.1.1"
    assert identity.full_name == "chapter_7.1.1.part2"
    assert identity.html_name == "chapter_7.1.1_part2.html"
    assert identity.is_part is True
    assert identity.is_special_matter is False
    assert identity.index_path == [7, 1, 1]

    front = ChapterIdentity.parse("front_matter")
    assert front.base_name == "front_matter"
    assert front.full_name == "front_matter"
    assert front.html_name == "front_matter.html"
    assert front.is_part is False
    assert front.is_front_matter is True
    assert front.is_back_matter is False
    assert front.index_path == []


def test_sort_order_places_matter_around_chapters():
    identities = [
        ChapterIdentity.parse("back_matter"),
        ChapterIdentity.parse("chapter_10"),
        ChapterIdentity.parse("front_matter"),
        ChapterIdentity.parse("chapter_2"),
    ]
    ordered = [item.full_name for item in sorted(identities)]
    assert ordered == ["front_matter", "chapter_2", "chapter_10", "back_matter"]


def test_make_part_name_and_get_base_from_part():
    assert ChapterIdentity.make_part_name("chapter_5", 1) == "chapter_5.part1"
    assert ChapterIdentity.make_part_name("front_matter", 2) == "front_matter.part2"
    assert ChapterIdentity.get_base_from_part("chapter_5.part1") == "chapter_5"
    assert ChapterIdentity.get_base_from_part("chapter_5") == "chapter_5"


def test_strip_part_suffix_and_part_path_handle_nested_parts():
    assert strip_part_suffix("chapter_7.4.part3.part1") == "chapter_7.4"
    assert strip_part_suffix("chapter_5.part2.md") == "chapter_5"
    assert strip_part_suffix("chapter_5") == "chapter_5"
    assert part_path("chapter_7.4.part3.part1") == (3, 1)
    assert part_path("chapter_5.part2.md") == (2,)
    assert part_path("chapter_5") == ()


def test_group_by_base_sorts_parts():
    grouped = group_by_base(
        [
            ChapterIdentity.parse("chapter_5.part2"),
            ChapterIdentity.parse("chapter_5.part1"),
            ChapterIdentity.parse("chapter_6"),
        ]
    )
    assert [item.part for item in grouped["chapter_5"]] == [1, 2]
    assert len(grouped["chapter_6"]) == 1


def test_discover_parts_reads_only_part_files(tmp_path: Path):
    for name in ("chapter_5.part2.md", "chapter_5.part1.md", "chapter_5.md"):
        (tmp_path / name).write_text("x", encoding="utf-8")
    parts = discover_parts(tmp_path, "chapter_5")
    assert [item.full_name for item in parts] == ["chapter_5.part1", "chapter_5.part2"]
