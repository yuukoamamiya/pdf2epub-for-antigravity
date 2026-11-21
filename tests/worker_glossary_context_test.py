"""Coverage for the pure worker glossary context projection."""

from pdf2epub.worker_glossary_context import (
    build_worker_glossary_context,
    compact_glossary_entry,
)


def test_compact_entry_keeps_worker_fields_and_drops_audit_noise():
    compact = compact_glossary_entry(
        {
            "source": "Geist",
            "target": "精神",
            "kind": "term",
            "policy": "fixed",
            "note": "  Hegelian sense  ",
            "audit": {"origin": "external"},
            "empty_list": [],
        }
    )

    assert compact == {
        "source": "Geist",
        "target": "精神",
        "kind": "term",
        "policy": "fixed",
        "note": "Hegelian sense",
    }


def test_compact_entry_drops_overlong_note():
    compact = compact_glossary_entry({"source": "A", "note": "x" * 400})
    assert "note" not in compact


def test_file_mode_builds_direct_file_contexts():
    context = build_worker_glossary_context(
        {
            "first.md": [{"source": "A", "target": "甲"}],
            "second.md": [{"source": "B", "target": "乙"}],
        },
        manifest={},
        group={},
        worker_id="w1",
        files=["first.md", "second.md"],
        chapter_mode=False,
        chapter_pack=False,
    )

    assert context is not None
    assert context["schema_version"] == 1
    assert context["selection"] == "worker_sparse_direct_file_contexts"
    assert set(context["files"]) == {"first.md", "second.md"}
    assert context["files"]["first.md"][0]["target"] == "甲"


def test_chapter_mode_aggregates_one_entry_list():
    context = build_worker_glossary_context(
        {
            "part1.md": [{"source": "A", "target": "甲"}],
            "part2.md": [{"source": "A", "target": "甲"}, {"source": "B", "target": "乙"}],
        },
        manifest={},
        group={"chapter_id": "c1", "chapter_file_count": 2},
        worker_id="w2",
        files=["part1.md", "part2.md"],
        chapter_mode=True,
        chapter_pack=False,
    )

    assert context is not None
    assert context["schema_version"] == 2
    assert context["chapter_id"] == "c1"
    assert {entry["source"] for entry in context["entries"]} == {"A", "B"}
    assert "files" not in context


def test_split_chapter_separates_shared_and_local_entries():
    context = build_worker_glossary_context(
        {
            "part1.md": [{"source": "Shared", "target": "共"}],
            "part2.md": [{"source": "Shared", "target": "共"}, {"source": "Local", "target": "本"}],
        },
        manifest={},
        group={"chapter_id": "c2", "chapter_file_count": 2, "chapter_split": True},
        worker_id="w3",
        files=["part1.md", "part2.md"],
        chapter_mode=True,
        chapter_pack=False,
    )

    assert context is not None
    assert context["schema_version"] == 3
    assert [entry["source"] for entry in context["shared_entries"]] == ["Shared"]
    assert [entry["source"] for entry in context["local_entries"]] == ["Local"]


def test_chapter_pack_keeps_per_file_maps():
    context = build_worker_glossary_context(
        {
            "chapter_1.md": [{"source": "A", "target": "甲"}],
            "chapter_2.md": [{"source": "B", "target": "乙"}],
        },
        manifest={},
        group={"chapter_ids": ["c1", "c2"], "chapter_file_counts": {"c1": 1, "c2": 1}},
        worker_id="w4",
        files=["chapter_1.md", "chapter_2.md"],
        chapter_mode=True,
        chapter_pack=True,
    )

    assert context is not None
    assert context["schema_version"] == 4
    assert context["chapter_ids"] == ["c1", "c2"]
    assert set(context["files"]) == {"chapter_1.md", "chapter_2.md"}


def test_empty_or_invalid_context_returns_none():
    assert (
        build_worker_glossary_context(
            {"first.md": []},
            manifest={},
            group={},
            worker_id="w5",
            files=["first.md"],
            chapter_mode=False,
            chapter_pack=False,
        )
        is None
    )
    assert (
        build_worker_glossary_context(
            {"first.md": ["not a mapping"]},
            manifest={},
            group={},
            worker_id="w6",
            files=["first.md"],
            chapter_mode=False,
            chapter_pack=False,
        )
        is None
    )


def test_duplicate_entries_are_deduplicated_per_file():
    entry = {"source": "A", "target": "甲"}
    context = build_worker_glossary_context(
        {"first.md": [entry, dict(entry)]},
        manifest={},
        group={},
        worker_id="w7",
        files=["first.md"],
        chapter_mode=False,
        chapter_pack=False,
    )

    assert context is not None
    assert len(context["files"]["first.md"]) == 1
