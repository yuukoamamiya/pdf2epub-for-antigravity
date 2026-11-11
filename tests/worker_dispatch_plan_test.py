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
    task: str = "translate",
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
        task,
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


def test_html_dispatch_packs_alternating_kinds_without_changing_group_identity():
    groups = [
        {"worker_id": f"worker_{index:03d}", "files": [f"u{index}.md"]}
        for index in range(1, 25)
    ]
    stats = {
        f"u{index}.md": {"estimated_tokens": 50 if index % 2 else 150}
        for index in range(1, 25)
    }
    original = [dict(group) for group in groups]
    planned, waves, cap = _plan_dispatch_waves(
        groups,
        stats,
        {
            "max_concurrency": 4,
            "large_file_token_threshold": 100,
            "extreme_file_token_threshold": 400,
        },
        strategy="ordinary_before_large",
    )
    _, source_order_waves, _ = _plan_dispatch_waves(
        groups,
        stats,
        {
            "max_concurrency": 4,
            "large_file_token_threshold": 100,
            "extreme_file_token_threshold": 400,
        },
    )

    assert [(item["worker_id"], item["files"]) for item in planned] == [
        (item["worker_id"], item["files"]) for item in original
    ]
    assert cap == 4
    assert {worker for wave in waves for worker in wave["worker_ids"]} == {
        worker for wave in source_order_waves for worker in wave["worker_ids"]
    }
    assert len(source_order_waves) == 24
    assert [wave["reason"] for wave in source_order_waves] == [
        "ordinary" if index % 2 else "large_units" for index in range(1, 25)
    ]
    assert len(waves) == 9
    assert not any(len(wave["worker_ids"]) == 1 for wave in waves)
    assert [wave["reason"] for wave in waves] == ["ordinary"] * 3 + [
        "large_units"
    ] * 6
    assert [worker for wave in waves for worker in wave["worker_ids"]] == [
        f"worker_{index:03d}" for index in range(1, 25, 2)
    ] + [f"worker_{index:03d}" for index in range(2, 25, 2)]


def test_write_worker_handoffs_applies_repacking_only_to_html_and_keeps_assignments(
    tmp_path: Path,
):
    def build_inventory(root: Path, task: str):
        names = {f"unit_{index:02d}.md": 20 for index in range(1, 25)}
        paths = _prepared(root, names, task=task)
        manifest = json.loads(paths["manifest"].read_text(encoding="utf-8"))
        manifest["file_stats"] = {
            name: {
                **(manifest["file_stats"].get(name, {}) or {}),
                "estimated_tokens": 50 if index % 2 else 150,
            }
            for index, name in enumerate(names, start=1)
        }
        paths["manifest"].write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return paths

    html_root = tmp_path / "html"
    markdown_root = tmp_path / "markdown"
    html_root.mkdir()
    markdown_root.mkdir()
    html_paths = build_inventory(html_root, "translate-html")
    html_handoffs = write_worker_handoffs(
        html_root, html_paths["manifest"], html_paths["prompt"]
    )
    html_manifest = json.loads(html_paths["manifest"].read_text(encoding="utf-8"))
    html_plan = html_manifest["dispatch_plan"]

    assert len(html_handoffs) == 24
    assert len(html_plan) == 9
    assert [wave["reason"] for wave in html_plan] == ["ordinary"] * 3 + [
        "large_units"
    ] * 6
    assert not any(len(wave["worker_ids"]) == 1 for wave in html_plan)
    assert [item["worker_id"] for item in html_handoffs] == [
        f"chapter_unit_{index:02d}_part_001" for index in range(1, 25)
    ]
    assert [item["assigned_files"] for item in html_handoffs] == [
        [f"unit_{index:02d}.md"] for index in range(1, 25)
    ]
    wave_by_worker = {
        worker_id: wave
        for wave in html_plan
        for worker_id in wave["worker_ids"]
    }
    for item in html_handoffs:
        wave = wave_by_worker[item["worker_id"]]
        scoped = json.loads((html_root / item["manifest"]).read_text(encoding="utf-8"))
        assert scoped["dispatch_wave"] == wave["wave"] == item["dispatch_wave"]
        assert scoped["dispatch_wave_max_concurrency"] == wave["max_concurrency"]
        assert scoped["dispatch_wave_reason"] == wave["reason"]
    guide = (html_root / html_manifest["dispatch_operator_prompt"]).read_text(
        encoding="utf-8"
    )
    for item in html_handoffs:
        assert item["prompt"] in guide
        assert item["manifest"] in guide

    html_assignments = {
        item["worker_id"]: (item["assigned_files"], item["assignment_sha256"])
        for item in html_handoffs
    }
    regenerated_html_handoffs = write_worker_handoffs(
        html_root, html_paths["manifest"], html_paths["prompt"]
    )
    assert {
        item["worker_id"]: (item["assigned_files"], item["assignment_sha256"])
        for item in regenerated_html_handoffs
    } == html_assignments

    markdown_paths = build_inventory(markdown_root, "translate")
    markdown_handoffs = write_worker_handoffs(
        markdown_root, markdown_paths["manifest"], markdown_paths["prompt"]
    )
    markdown_manifest = json.loads(
        markdown_paths["manifest"].read_text(encoding="utf-8")
    )
    assert [wave["reason"] for wave in markdown_manifest["dispatch_plan"]] == [
        "ordinary" if index % 2 else "large_units" for index in range(1, 25)
    ]
    assert len(markdown_manifest["dispatch_plan"]) == 24
    assert [item["worker_id"] for item in markdown_handoffs] == [
        f"chapter_unit_{index:02d}_part_001" for index in range(1, 25)
    ]
    assert [item["assigned_files"] for item in markdown_handoffs] == [
        [f"unit_{index:02d}.md"] for index in range(1, 25)
    ]


def test_html_dispatch_keeps_extreme_barriers_and_cap_one_honest():
    groups = [
        {"worker_id": f"worker_{index:03d}", "files": [f"u{index}.md"]}
        for index in range(1, 7)
    ]
    stats = {
        "u1.md": {"estimated_tokens": 50},
        "u2.md": {"estimated_tokens": 150},
        "u3.md": {"estimated_tokens": 500},
        "u4.md": {"estimated_tokens": 50},
        "u5.md": {"estimated_tokens": 150},
        "u6.md": {"estimated_tokens": 50},
    }
    _, waves, _ = _plan_dispatch_waves(
        groups,
        stats,
        {"max_concurrency": 4, "large_file_token_threshold": 100, "extreme_file_token_threshold": 400},
        strategy="ordinary_before_large",
    )
    assert [wave["worker_ids"] for wave in waves] == [
        ["worker_001"], ["worker_002"], ["worker_003"], ["worker_004", "worker_006"], ["worker_005"]
    ]
    assert waves[2]["reason"] == "extreme_unit"

    _, cap_one_waves, cap = _plan_dispatch_waves(
        groups[:3], stats,
        {"max_concurrency": 1, "large_file_token_threshold": 100, "extreme_file_token_threshold": 400},
        strategy="ordinary_before_large",
    )
    assert cap == 1
    assert all(wave["max_concurrency"] == 1 for wave in cap_one_waves)


def test_html_dispatch_guide_does_not_mark_active_lease_as_fresh(tmp_path: Path):
    from pdf2epub.subagent_dispatch import claim_dispatch_lease
    from pdf2epub.subagent_runtime import dispatch_lease_path

    paths = _prepared(tmp_path, {f"unit_{index}.md": 20 for index in range(4)})
    manifest = json.loads(paths["manifest"].read_text(encoding="utf-8"))
    manifest["task"] = "translate-html"
    paths["manifest"].write_text(json.dumps(manifest), encoding="utf-8")
    handoffs = write_worker_handoffs(tmp_path, paths["manifest"], paths["prompt"])
    assert handoffs
    lease_path = dispatch_lease_path(tmp_path, "translate-html")
    first = handoffs[0]
    claim_dispatch_lease(
        lease_path,
        task="translate-html",
        assignment_sha256=first["assignment_sha256"],
        worker_id=first["worker_id"],
        assigned_files=first["assigned_files"],
        owner_id="fixture-owner",
        conversation_id="fixture-active-conversation",
    )
    second = handoffs[1]
    claim_dispatch_lease(
        lease_path,
        task="other-task",
        assignment_sha256="conflicting-assignment",
        worker_id="other-worker",
        assigned_files=second["assigned_files"],
        owner_id="other-owner",
        conversation_id="fixture-conflicting-conversation",
    )

    refreshed = write_worker_handoffs(tmp_path, paths["manifest"], paths["prompt"])
    updated = next(item for item in refreshed if item["worker_id"] == first["worker_id"])
    parent = json.loads(paths["manifest"].read_text(encoding="utf-8"))
    guide = (tmp_path / parent["dispatch_operator_prompt"]).read_text(encoding="utf-8")
    scoped = json.loads((tmp_path / updated["manifest"]).read_text(encoding="utf-8"))

    assert updated["assignment_sha256"] == first["assignment_sha256"]
    assert updated["dispatch_status"] == "active_same_assignment"
    conflicting = next(item for item in refreshed if item["worker_id"] == second["worker_id"])
    assert conflicting["dispatch_status"] == "conflict"
    assert "lease `active_same_assignment`" in guide
    assert "lease `conflict`" in guide
    assert "A conflict blocks opening a new worker" in guide
    assert "Potentially start these 2 pending assignments" in guide
    assert "No Wave 1 assignment is currently both pending and lease-available" not in guide
    assert "dispatch_plan" not in scoped
    assert "dispatch_operator_prompt" not in scoped
    assert "translate-html_dispatch_prompt.md" not in (tmp_path / updated["prompt"]).read_text(encoding="utf-8")


def test_html_dispatch_guide_waits_when_every_first_wave_assignment_is_active(
    tmp_path: Path,
):
    from pdf2epub.subagent_dispatch import claim_dispatch_lease
    from pdf2epub.subagent_runtime import dispatch_lease_path

    paths = _prepared(tmp_path, {f"unit_{index}.md": 20 for index in range(4)})
    manifest = json.loads(paths["manifest"].read_text(encoding="utf-8"))
    manifest["task"] = "translate-html"
    paths["manifest"].write_text(json.dumps(manifest), encoding="utf-8")
    handoffs = write_worker_handoffs(tmp_path, paths["manifest"], paths["prompt"])
    lease_path = dispatch_lease_path(tmp_path, "translate-html")
    for index, item in enumerate(handoffs):
        claim_dispatch_lease(
            lease_path,
            task="translate-html",
            assignment_sha256=item["assignment_sha256"],
            worker_id=item["worker_id"],
            assigned_files=item["assigned_files"],
            owner_id=f"fixture-owner-{index}",
            conversation_id=f"fixture-conversation-{index}",
        )

    write_worker_handoffs(tmp_path, paths["manifest"], paths["prompt"])
    parent = json.loads(paths["manifest"].read_text(encoding="utf-8"))
    guide = (tmp_path / parent["dispatch_operator_prompt"]).read_text(encoding="utf-8")

    assert "No Wave 1 assignment is currently both pending and lease-available" in guide
    assert "Review or wait on assigned/running/active_same_assignment work" in guide
    assert "No pending body workers" not in guide
    assert "Potentially start these" not in guide


def test_html_dispatch_guide_handles_one_or_zero_pending_workers(tmp_path: Path):
    paths = _prepared(tmp_path, {f"unit_{index}.md": 20 for index in range(3)})
    manifest = json.loads(paths["manifest"].read_text(encoding="utf-8"))
    manifest["task"] = "translate-html"
    manifest["pending_files"] = ["unit_1.md"]
    manifest["batch_queue"] = [
        batch for batch in manifest["batch_queue"] if "unit_1.md" in batch["files"]
    ]
    paths["manifest"].write_text(json.dumps(manifest), encoding="utf-8")

    handoffs = write_worker_handoffs(tmp_path, paths["manifest"], paths["prompt"])
    parent = json.loads(paths["manifest"].read_text(encoding="utf-8"))
    guide = (tmp_path / parent["dispatch_operator_prompt"]).read_text(encoding="utf-8")
    assert len(handoffs) == 1
    assert "Potentially start these 1 pending assignments" in guide

    parent["pending_files"] = []
    parent["batch_queue"] = []
    paths["manifest"].write_text(json.dumps(parent), encoding="utf-8")
    assert write_worker_handoffs(tmp_path, paths["manifest"], paths["prompt"]) == []
    zero_manifest = json.loads(paths["manifest"].read_text(encoding="utf-8"))
    zero_guide = (tmp_path / zero_manifest["dispatch_operator_prompt"]).read_text(
        encoding="utf-8"
    )
    assert "No pending body workers" in zero_guide
    assert "Process only metadata that is still pending" in zero_guide


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


def test_pdf_chapter_mode_keeps_every_special_role_out_of_ordinary_workers():
    from pdf2epub.worker_grouping import _chapter_worker_groups

    manifest = {
        "pending_files": ["ordinary.md", "toc.md", "notes.md"],
        "file_stats": {
            "ordinary.md": {"estimated_tokens": 100, "size_bytes": 100, "nonempty_line_count": 2},
            "toc.md": {"estimated_tokens": 100, "size_bytes": 100, "nonempty_line_count": 2},
            "notes.md": {"estimated_tokens": 100, "size_bytes": 100, "nonempty_line_count": 2},
        },
        "batching": {
            "pack_adjacent_chapters": True,
            "max_chapters_per_worker": 3,
            "max_tiny_chapters_per_worker": 6,
            "tiny_chapter_max_tokens": 2000,
        },
        "chapter_groups": {
            "ordinary": ["ordinary.md"],
            "toc": ["toc.md"],
            "notes": ["notes.md"],
        },
        "file_roles": {"toc.md": "toc", "notes.md": "notes"},
    }

    groups = _chapter_worker_groups(manifest)
    groups_by_file = {
        name: group["files"]
        for group in groups
        for name in group["files"]
    }

    assert groups_by_file["ordinary.md"] == ["ordinary.md"]
    assert "ordinary.md" not in groups_by_file["toc.md"]
    assert "ordinary.md" not in groups_by_file["notes.md"]


def _special_manifest(pack_tiny_special_units: bool):
    return {
        "pending_files": ["toc.md", "notes.md"],
        "file_stats": {
            "toc.md": {"estimated_tokens": 100, "size_bytes": 100, "nonempty_line_count": 2},
            "notes.md": {"estimated_tokens": 100, "size_bytes": 100, "nonempty_line_count": 2},
        },
        "batching": {
            "pack_adjacent_chapters": True,
            "pack_tiny_special_units": pack_tiny_special_units,
            "max_chapters_per_worker": 3,
            "max_tiny_chapters_per_worker": 6,
            "tiny_chapter_max_tokens": 2000,
        },
        "chapter_groups": {"toc": ["toc.md"], "notes": ["notes.md"]},
        "file_roles": {"toc.md": "toc", "notes.md": "notes"},
    }


def test_tiny_special_units_stay_separate_by_default():
    from pdf2epub.worker_grouping import _chapter_worker_groups

    groups = _chapter_worker_groups(_special_manifest(False))
    assert [group["files"] for group in groups] == [["toc.md"], ["notes.md"]]


def test_tiny_special_units_can_share_one_worker_when_enabled():
    from pdf2epub.worker_grouping import _chapter_worker_groups

    groups = _chapter_worker_groups(_special_manifest(True))
    assert [group["files"] for group in groups] == [["toc.md", "notes.md"]]


def test_continuation_worker_receives_read_only_preceding_source_context(tmp_path):
    source = tmp_path / "source"
    target = tmp_path / "target"
    source.mkdir()
    target.mkdir()
    (source / "chapter_1.part1.md").write_text(
        "First paragraph.\n\nSecond paragraph.\n\n[^1]: Footnote text.\n",
        encoding="utf-8",
    )
    (source / "chapter_1.part2.md").write_text(
        "Continued paragraph.\n", encoding="utf-8"
    )
    paths = prepare_markdown_subagent(
        tmp_path,
        "translate",
        source,
        target,
        "English",
        "Chinese",
        continuation_files={
            "chapter_1.part2.md": {
                "is_continuation": True,
                "part_number": 2,
                "part_count": 2,
                "previous_file": "chapter_1.part1.md",
            }
        },
        config={"subagent": {"batching": {"max_concurrency": 1}}},
    )
    manifest = json.loads(paths["manifest"].read_text(encoding="utf-8"))
    context = manifest["preceding_context"]["chapter_1.part2.md"]
    assert context["source_file"] == "chapter_1.part1.md"
    assert "Second paragraph." in context["text"]
    assert "Footnote text." not in context["text"]
    assert "chapter_1.part1.md" not in manifest["preceding_context"]

    handoffs = write_worker_handoffs(tmp_path, paths["manifest"], paths["prompt"])
    part2 = next(item for item in handoffs if "chapter_1.part2.md" in item["files"])
    prompt = (tmp_path / part2["prompt"]).read_text(encoding="utf-8")
    assert "Preceding source context (read-only)" in prompt
    assert "Second paragraph." in prompt
    assert "do not translate, copy" in prompt.lower()


def test_non_tiny_special_unit_never_packs_with_a_tiny_special_unit():
    from pdf2epub.worker_grouping import _chapter_worker_groups

    manifest = _special_manifest(True)
    manifest["file_stats"]["toc.md"]["estimated_tokens"] = 5000
    groups = _chapter_worker_groups(manifest)
    assert [group["files"] for group in groups] == [["toc.md"], ["notes.md"]]
