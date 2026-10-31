import json
from pathlib import Path

from pdf2epub.markdown_handoff import prepare_markdown_subagent
from pdf2epub.subagent_runtime import write_worker_handoffs
from pdf2epub.worker_grouping import _plan_dispatch_waves


def _prepared(
    tmp_path: Path,
    names_and_sizes: dict[str, int],
    *,
    chapters: bool = True,
    max_source_tokens: int = 10_000,
):
    source = tmp_path / "source"
    target = tmp_path / "target"
    source.mkdir()
    target.mkdir()
    for name, words in names_and_sizes.items():
        (source / name).write_text("word " * words, encoding="utf-8")
    chapter_groups = (
        {Path(name).stem: [name] for name in names_and_sizes}
        if chapters
        else None
    )
    paths = prepare_markdown_subagent(
        tmp_path,
        "translate",
        source,
        target,
        "English",
        "Chinese",
        chapter_groups=chapter_groups,
        config={
            "subagent": {
                "batching": {
                    "max_concurrency": 4,
                    "max_chapters_per_worker": 1,
                    "max_tiny_chapters_per_worker": 1,
                    "max_source_tokens": max_source_tokens,
                    "large_file_token_threshold": 100,
                    "extreme_file_token_threshold": 400,
                }
            }
        },
    )
    return paths


def test_mixed_pending_inventory_dispatches_ordinary_wave_before_exclusive_extreme(
    tmp_path: Path,
):
    paths = _prepared(
        tmp_path,
        {
            "ordinary_1.md": 20,
            "ordinary_2.md": 24,
            "ordinary_3.md": 28,
            "ordinary_4.md": 32,
            "extreme.md": 500,
        },
    )

    handoffs = write_worker_handoffs(tmp_path, paths["manifest"], paths["prompt"])
    manifest = json.loads(paths["manifest"].read_text(encoding="utf-8"))

    assert [wave["max_concurrency"] for wave in manifest["dispatch_plan"]] == [4, 1]
    assert manifest["dispatch_plan"][0]["reason"] == "ordinary"
    assert manifest["dispatch_plan"][1]["reason"] == "extreme_unit"
    extreme_handoff = next(item for item in handoffs if item["files"] == ["extreme.md"])
    assert extreme_handoff["dispatch_wave_max_concurrency"] == 1
    assert all(item["dispatch_wave"] == 1 for item in handoffs if "ordinary" in item["files"][0])
    assert manifest["effective_max_concurrency"] == 4
    assert "operator scheduling" in manifest["dispatch_contract"].lower()
    assert "separate metadata workers" in manifest["dispatch_contract"]
    assert "never run metadata during an extreme_unit wave" in manifest["dispatch_contract"]
    parent_prompt = paths["prompt"].read_text(encoding="utf-8")
    assert "separate metadata workers" in parent_prompt
    assert "must never run during an" in parent_prompt

    scoped = json.loads((tmp_path / extreme_handoff["manifest"]).read_text(encoding="utf-8"))
    prompt = (tmp_path / extreme_handoff["prompt"]).read_text(encoding="utf-8")
    assert scoped["dispatch_wave"] == 2
    assert "wave cap is 1 active workers" in prompt


def test_nonchapter_inventory_uses_configured_workers_despite_extreme_unit(
    tmp_path: Path,
):
    paths = _prepared(
        tmp_path,
        {
            "ordinary_1.md": 20,
            "ordinary_2.md": 24,
            "ordinary_3.md": 28,
            "ordinary_4.md": 32,
            "extreme.md": 500,
        },
        chapters=False,
        max_source_tokens=100,
    )

    handoffs = write_worker_handoffs(tmp_path, paths["manifest"], paths["prompt"])
    ordinary_handoffs = [
        item
        for item in handoffs
        if all(name.startswith("ordinary_") for name in item["files"])
    ]

    assert len(ordinary_handoffs) > 1
    assert all(item["dispatch_wave_max_concurrency"] == 4 for item in ordinary_handoffs)
    assert any("extreme.md" in item["files"] for item in handoffs)


def test_resume_dispatch_plan_uses_only_pending_units_and_assigns_each_once(
    tmp_path: Path,
):
    paths = _prepared(
        tmp_path,
        {"ordinary.md": 20, "completed_extreme.md": 500, "pending_large.md": 160},
    )
    manifest = json.loads(paths["manifest"].read_text(encoding="utf-8"))
    manifest["pending_files"] = ["ordinary.md", "pending_large.md"]
    manifest["batch_queue"] = [
        batch
        for batch in manifest["batch_queue"]
        if "completed_extreme.md" not in batch["files"]
    ]
    paths["manifest"].write_text(json.dumps(manifest), encoding="utf-8")

    handoffs = write_worker_handoffs(tmp_path, paths["manifest"], paths["prompt"])
    updated = json.loads(paths["manifest"].read_text(encoding="utf-8"))
    assigned = [name for item in handoffs for name in item["assigned_files"]]

    assert sorted(assigned) == ["ordinary.md", "pending_large.md"]
    assert len(assigned) == len(set(assigned))
    assert all("completed_extreme.md" not in item["files"] for item in handoffs)
    assert all("completed_extreme.md" not in wave["worker_ids"] for wave in updated["dispatch_plan"])


def test_dispatch_severity_uses_largest_file_not_worker_aggregate():
    groups, plan, configured = _plan_dispatch_waves(
        [
            {
                "worker_id": "worker_001",
                "files": ["a.md", "b.md"],
                "estimated_tokens": 120,
            }
        ],
        {
            "a.md": {"estimated_tokens": 60},
            "b.md": {"estimated_tokens": 60},
        },
        {
            "max_concurrency": 4,
            "large_file_token_threshold": 100,
            "extreme_file_token_threshold": 400,
        },
    )

    assert configured == 4
    assert plan == [
        {
            "wave": 1,
            "worker_ids": ["worker_001"],
            "max_concurrency": 4,
            "reason": "ordinary",
        }
    ]
    assert groups[0]["dispatch_wave_max_concurrency"] == 4


def test_small_title_and_chapter_remain_packable_under_existing_budgets(tmp_path: Path):
    paths = _prepared(
        tmp_path,
        {"title.md": 8, "chapter.md": 12},
    )
    manifest = json.loads(paths["manifest"].read_text(encoding="utf-8"))
    manifest["chapter_groups"] = {"frontmatter": ["title.md"], "chapter": ["chapter.md"]}
    manifest["batching"]["max_tiny_chapters_per_worker"] = 2
    paths["manifest"].write_text(json.dumps(manifest), encoding="utf-8")

    handoffs = write_worker_handoffs(tmp_path, paths["manifest"], paths["prompt"])

    assert len(handoffs) == 1
    assert handoffs[0]["files"] == ["title.md", "chapter.md"]


def test_title_stays_separate_from_first_chapter_over_30000_bytes(tmp_path: Path):
    paths = _prepared(
        tmp_path,
        {"title.md": 8, "chapter_1.md": 7_000},
    )
    assert (tmp_path / "source" / "chapter_1.md").stat().st_size > 30_000
    handoffs = write_worker_handoffs(tmp_path, paths["manifest"], paths["prompt"])

    assert len(handoffs) == 2
    assert [item["files"] for item in handoffs] == [["title.md"], ["chapter_1.md"]]
