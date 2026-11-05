"""Focused contracts for the split Markdown hand-off services."""
from pathlib import Path
import ast

from pdf2epub.markdown_handoff_context import (
    MarkdownHandoffRequest,
    normalize_handoff_context,
)
from pdf2epub.markdown_handoff_planning import MarkdownHandoffPlan
from pdf2epub.markdown_handoff_prompt import (
    PromptBatching,
    PromptIdentity,
    PromptOperationalPaths,
    PromptRenderData,
    render_markdown_prompt,
)
from pdf2epub.markdown_handoff_resume import unit_context_is_current
from pdf2epub.markdown_handoff_resume import decide_resume
from pdf2epub.markdown_handoff_planning import plan_markdown_handoff
from pdf2epub.workflow_contracts import MARKDOWN_VALIDATION_SCHEMA_VERSION, sha256_file


def test_handoff_services_do_not_import_the_coordinator_or_cli_commands():
    root = Path(__file__).parents[1]
    services = (
        "markdown_handoff_context.py",
        "markdown_handoff_resume.py",
        "markdown_handoff_manifest.py",
        "markdown_handoff_prompt.py",
        "markdown_handoff_planning.py",
    )
    for name in services:
        tree = ast.parse((root / "pdf2epub" / name).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                assert node.module not in {"markdown_handoff", "pdf2epub.markdown_handoff"}
                assert not (node.module or "").startswith("pdf2epub.commands")
            elif isinstance(node, ast.Import):
                assert not any(alias.name == "pdf2epub.markdown_handoff" for alias in node.names)
                assert not any(alias.name.startswith("pdf2epub.commands") for alias in node.names)


def test_context_projection_hashes_only_owned_files_and_normalizes_chapter_groups(tmp_path):
    context_file = tmp_path / "glossary.json"
    context_file.write_text('{"entries": []}', encoding="utf-8")
    request = MarkdownHandoffRequest(
        output_dir=tmp_path,
        task="translate",
        source_dir=tmp_path / "source",
        target_dir=tmp_path / "target",
        source_language="English",
        target_language="Chinese",
        context_files={"glossary": context_file},
        unit_context_files={"chapter.md": context_file},
        chapter_groups={"chapter-1": iter(("chapter.md", "other.md", "chapter.md"))},
    )

    projection = normalize_handoff_context(request, iter(("chapter.md", "unmapped.md")))

    assert projection.context_files == {"glossary": "glossary.json"}
    assert projection.context_sha256["glossary"]
    assert projection.unit_context_sha256["chapter.md"] == projection.context_sha256["glossary"]
    assert projection.chapter_groups == {
        "chapter-1": ("chapter.md",),
        "unit:unmapped.md": ("unmapped.md",),
    }


def test_resume_context_decision_is_unit_scoped_and_legacy_safe():
    previous = {
        "context_sha256": {"snapshot": "old"},
        "unit_context_sha256": {"a.md": "a-old", "b.md": "b-old"},
        "toc_heading_contexts": {"a.md": {"title": "A"}},
        "special_unit_modes": {},
    }
    assert unit_context_is_current(
        "a.md", previous_manifest=previous, controlled_files={},
        context_sha256={"snapshot": "new"},
        unit_context_sha256={"a.md": "a-old", "b.md": "b-new"},
        heading_contexts={"a.md": {"title": "A"}}, resume=True,
    )
    assert not unit_context_is_current(
        "a.md", previous_manifest=previous, controlled_files={},
        context_sha256={"snapshot": "new"},
        unit_context_sha256={"a.md": "a-new"},
        heading_contexts={"a.md": {"title": "A"}}, resume=True,
    )


def test_role_source_change_invalidates_only_its_own_unit():
    previous = {
        "special_unit_modes": {},
        "file_roles": {"a.md": "bibliography", "b.md": "notes"},
        "file_role_sources": {
            "a.md": {"source": "toc_tree.type", "node_id": "a", "index_path": [1]},
            "b.md": {"source": "toc_tree.type", "node_id": "b", "index_path": [2]},
        },
        "context_sha256": {}, "unit_context_sha256": {}, "toc_heading_contexts": {},
    }
    current_sources = {
        "a.md": previous["file_role_sources"]["a.md"],
        "b.md": {"source": "tree_progress.type", "node_id": "b", "index_path": [2]},
    }
    for name in ("a.md", "b.md"):
        assert unit_context_is_current(
            name, previous_manifest=previous, controlled_files={}, context_sha256={},
            unit_context_sha256={}, heading_contexts={}, resume=True,
            file_roles=previous["file_roles"], file_role_sources=current_sources,
        ) is (name == "a.md")


def test_old_per_file_validation_ledger_is_not_reused_after_policy_version_bump(tmp_path):
    source_dir = tmp_path / "source"
    target_dir = tmp_path / "target"
    source_dir.mkdir()
    target_dir.mkdir()
    source = source_dir / "unit.md"
    target = target_dir / "unit.md"
    source.write_text("Source superscript.", encoding="utf-8")
    target.write_text("Target superscript.", encoding="utf-8")
    request = MarkdownHandoffRequest(
        output_dir=tmp_path, task="polish", source_dir=source_dir,
        target_dir=target_dir, source_language="English",
        target_language="English", resume=True,
    )
    plan = plan_markdown_handoff(source_dir, "polish")
    context = normalize_handoff_context(request, ["unit.md"])
    ledger_path = tmp_path / "polish_file_validation.json"
    old_record = {
        "schema_version": MARKDOWN_VALIDATION_SCHEMA_VERSION - 1,
        "valid": True,
        "source_sha256": sha256_file(source),
        "target_sha256": sha256_file(target),
    }
    ledger_path.write_text(
        __import__("json").dumps({
            "schema_version": MARKDOWN_VALIDATION_SCHEMA_VERSION - 1,
            "task": "polish", "scope": "file-checkpoints",
            "files": {"unit.md": old_record},
        }),
        encoding="utf-8",
    )

    old_decision = decide_resume(request, plan, context)
    assert old_decision.completed_files == ()
    assert old_decision.pending_files == ("unit.md",)

    old_record["schema_version"] = MARKDOWN_VALIDATION_SCHEMA_VERSION
    ledger_path.write_text(
        __import__("json").dumps({
            "schema_version": MARKDOWN_VALIDATION_SCHEMA_VERSION,
            "task": "polish", "scope": "file-checkpoints",
            "files": {"unit.md": old_record},
        }),
        encoding="utf-8",
    )
    current_decision = decide_resume(request, plan, context)
    assert current_decision.completed_files == ("unit.md",)
    assert current_decision.pending_files == ()


def test_existing_translate_html_schema_three_ledger_remains_reusable(tmp_path):
    source_dir = tmp_path / "source"
    target_dir = tmp_path / "target"
    source_dir.mkdir()
    target_dir.mkdir()
    source = source_dir / "unit.md"
    target = target_dir / "unit.md"
    source.write_text("Source.", encoding="utf-8")
    target.write_text("Target.", encoding="utf-8")
    request = MarkdownHandoffRequest(
        output_dir=tmp_path, task="translate-html", source_dir=source_dir,
        target_dir=target_dir, source_language="English", target_language="Chinese",
        resume=True,
    )
    plan = plan_markdown_handoff(source_dir, "translate-html")
    context = normalize_handoff_context(request, ["unit.md"])
    (tmp_path / "translate-html_subagent_manifest.json").write_text(
        __import__("json").dumps({
            "source_language": "English", "target_language": "Chinese",
            "context_sha256": {}, "unit_context_sha256": {},
            "toc_heading_contexts": {}, "special_unit_modes": {},
        }),
        encoding="utf-8",
    )
    (tmp_path / "translate-html_file_validation.json").write_text(
        __import__("json").dumps({
            "schema_version": 3, "task": "translate-html", "scope": "file-checkpoints",
            "files": {"unit.md": {
                "schema_version": 3, "valid": True,
                "source_sha256": sha256_file(source),
                "target_sha256": sha256_file(target),
            }},
        }),
        encoding="utf-8",
    )

    decision = decide_resume(request, plan, context)

    assert decision.completed_files == ("unit.md",)
    assert decision.pending_files == ()


def test_role_change_stays_pending_across_prepare_until_new_validation_receipt(tmp_path):
    from pdf2epub.markdown_handoff import prepare_markdown_subagent
    from pdf2epub.markdown_subagent_validation import validate_markdown_subagent
    source_dir, target_dir = tmp_path / "source", tmp_path / "target"
    source_dir.mkdir()
    target_dir.mkdir()
    source, target = source_dir / "unit.md", target_dir / "unit.md"
    source.write_text("# A title\n\nAn ordinary source paragraph.", encoding="utf-8")
    target.write_text("# A title\n\n一段普通译文。", encoding="utf-8")
    role = {"unit.md": "bibliography"}
    role_source = {"unit.md": {"source": "toc_tree.type", "node_id": "refs", "index_path": [1]}}
    prepare_markdown_subagent(
        tmp_path, "translate", source_dir, target_dir, "English", "Chinese"
    )
    assert validate_markdown_subagent(
        tmp_path, "translate", source_dir, target_dir
    )["all_passed"]
    request = MarkdownHandoffRequest(
        output_dir=tmp_path, task="translate", source_dir=source_dir, target_dir=target_dir,
        source_language="English", target_language="Chinese", resume=True,
        file_roles=role, file_role_sources=role_source,
    )
    plan = plan_markdown_handoff(source_dir, "translate", file_roles=role)
    context = normalize_handoff_context(request, ["unit.md"])
    first_prepare = prepare_markdown_subagent(
        tmp_path, "translate", source_dir, target_dir, "English", "Chinese",
        resume=True, file_roles=role, file_role_sources=role_source,
    )
    import json
    assert json.loads(first_prepare["manifest"].read_text(encoding="utf-8"))["pending_files"] == ["unit.md"]
    second_prepare = prepare_markdown_subagent(
        tmp_path, "translate", source_dir, target_dir, "English", "Chinese",
        resume=True, file_roles=role, file_role_sources=role_source,
    )
    assert json.loads(second_prepare["manifest"].read_text(encoding="utf-8"))["pending_files"] == ["unit.md"]
    assert decide_resume(request, plan, context).pending_files == ("unit.md",)
    assert validate_markdown_subagent(
        tmp_path, "translate", source_dir, target_dir,
        file_roles=role, file_role_sources=role_source,
    )["all_passed"]
    decision = decide_resume(request, plan, context)
    assert decision.completed_files == ("unit.md",)
    assert decision.pending_files == ()


@__import__("pytest").mark.parametrize("source_name,source_text,roles,sources", [
    ("unit.md", "# References\n\nAuthor. Book, 1999.", {"unit.md": "bibliography"},
     {"unit.md": {"source": "toc_tree.type", "node_id": "refs", "index_path": [1]}}),
    ("unit.md", "# 3. Primary literature\n\nAuthor. Book, 1999.", {}, {}),
])
def test_role_receipt_e2e_reuses_after_prepare_and_validation(tmp_path, source_name, source_text, roles, sources):
    import json
    from pdf2epub.markdown_handoff import prepare_markdown_subagent
    from pdf2epub.markdown_subagent_validation import validate_markdown_subagent

    source_dir, target_dir = tmp_path / "source", tmp_path / "translated"
    source_dir.mkdir()
    target_dir.mkdir()
    (source_dir / source_name).write_text(source_text, encoding="utf-8")
    target_heading = "# 3. 参考文献" if source_text.startswith("# 3.") else "# 参考文献"
    (target_dir / source_name).write_text(f"{target_heading}\n\n作者。书名，1999。", encoding="utf-8")
    prepare_markdown_subagent(
        tmp_path, "translate", source_dir, target_dir, "English", "Chinese",
        file_roles=roles, file_role_sources=sources,
    )
    report = validate_markdown_subagent(
        tmp_path, "translate", source_dir, target_dir,
        file_roles=roles, file_role_sources=sources,
    )
    assert report["all_passed"] is True, report["invalid"]
    manifest = json.loads((tmp_path / "translate_subagent_manifest.json").read_text(encoding="utf-8"))
    effective_role = manifest["file_roles"][source_name]
    assert effective_role == "bibliography"
    if not roles:
        assert manifest["file_role_sources"][source_name]["source"] == "text_inference"
    paths = prepare_markdown_subagent(
        tmp_path, "translate", source_dir, target_dir, "English", "Chinese",
        resume=True, file_roles=roles, file_role_sources=sources,
    )
    resumed = json.loads(paths["manifest"].read_text(encoding="utf-8"))
    assert resumed["completed_files"] == [source_name]
    assert resumed["pending_files"] == []


def test_inferred_short_bibliography_is_isolated_from_short_chapter_during_polish(tmp_path):
    source_dir = tmp_path / "source"
    source_dir.mkdir()
    (source_dir / "chapter.md").write_text("A short prose paragraph.", encoding="utf-8")
    (source_dir / "references.md").write_text(
        "# 3. Primary literature\n\nSmith, John. Book, 1999.", encoding="utf-8"
    )
    plan = plan_markdown_handoff(source_dir, "polish")
    assert plan.normalized_roles["references.md"] == "bibliography"
    assert plan.isolated_files == {"references.md"}
    assert plan.recommended_batches == [["chapter.md"], ["references.md"]]


def test_partial_role_receipt_change_to_ordinary_clears_old_full_role(tmp_path):
    import json
    from pdf2epub.markdown_handoff import prepare_markdown_subagent
    from pdf2epub.markdown_subagent_validation import validate_markdown_subagent

    source_dir, target_dir = tmp_path / "source", tmp_path / "target"
    source_dir.mkdir()
    target_dir.mkdir()
    (source_dir / "unit.md").write_text("# A title\n\nAn ordinary source paragraph.", encoding="utf-8")
    (target_dir / "unit.md").write_text("# A title\n\n一段普通译文。", encoding="utf-8")
    old_roles = {"unit.md": "bibliography"}
    old_sources = {"unit.md": {"source": "toc_tree.type", "node_id": "refs", "index_path": [1]}}
    prepare_markdown_subagent(
        tmp_path, "translate", source_dir, target_dir, "English", "Chinese",
        file_roles=old_roles, file_role_sources=old_sources,
    )
    assert validate_markdown_subagent(
        tmp_path, "translate", source_dir, target_dir,
        file_roles=old_roles, file_role_sources=old_sources,
    )["all_passed"]

    changed = prepare_markdown_subagent(
        tmp_path, "translate", source_dir, target_dir, "English", "Chinese",
        resume=True, file_roles={}, file_role_sources={},
    )
    assert json.loads(changed["manifest"].read_text(encoding="utf-8"))["pending_files"] == ["unit.md"]
    partial = validate_markdown_subagent(
        tmp_path, "translate", source_dir, target_dir,
        selected_files=["unit.md"], file_roles={}, file_role_sources={},
    )
    assert partial["all_passed"]
    ledger = json.loads((tmp_path / "translate_file_validation.json").read_text(encoding="utf-8"))
    assert ledger["files"]["unit.md"]["file_role"] is None
    resumed = prepare_markdown_subagent(
        tmp_path, "translate", source_dir, target_dir, "English", "Chinese",
        resume=True, file_roles={}, file_role_sources={},
    )
    manifest = json.loads(resumed["manifest"].read_text(encoding="utf-8"))
    assert manifest["completed_files"] == ["unit.md"]
    assert manifest["pending_files"] == []


def test_validate_existing_outputs_before_second_resume_avoids_full_redelegation(tmp_path):
    source_dir = tmp_path / "source"
    target_dir = tmp_path / "target"
    source_dir.mkdir()
    target_dir.mkdir()
    source = source_dir / "unit.md"
    target = target_dir / "unit.md"
    source.write_text("Source paragraph.", encoding="utf-8")
    target.write_text("Existing translated paragraph.", encoding="utf-8")
    old_context = {"projection_version": 2, "node_id": "n1", "anchor": "a1"}
    from pdf2epub.toc_model import TOC_HEADING_CONTEXT_PROJECTION_VERSION
    refreshed_context = {"projection_version": TOC_HEADING_CONTEXT_PROJECTION_VERSION, "node_id": "n1", "anchor": "a1"}
    from pdf2epub.markdown_handoff import prepare_markdown_subagent
    from pdf2epub.markdown_subagent_validation import validate_markdown_subagent

    prepare_markdown_subagent(
        tmp_path, "translate", source_dir, target_dir, "English", "Chinese",
        heading_contexts={"unit.md": old_context},
    )
    assert validate_markdown_subagent(
        tmp_path, "translate", source_dir, target_dir,
    )["all_passed"]
    request = MarkdownHandoffRequest(
        output_dir=tmp_path, task="translate", source_dir=source_dir,
        target_dir=target_dir, source_language="English", target_language="Chinese",
        resume=True, heading_contexts={"unit.md": refreshed_context},
    )
    plan = plan_markdown_handoff(source_dir, "translate")
    context = normalize_handoff_context(request, ["unit.md"])
    manifest_path = tmp_path / "translate_subagent_manifest.json"

    first_resume = decide_resume(request, plan, context)
    assert first_resume.completed_files == ()
    assert first_resume.pending_files == ("unit.md",)

    # Refreshing preparation context alone cannot replace the old validation
    # receipt. A second resume remains pending until formal validation runs.
    refreshed = prepare_markdown_subagent(
        tmp_path, "translate", source_dir, target_dir, "English", "Chinese",
        resume=True, heading_contexts={"unit.md": refreshed_context},
    )
    assert __import__("json").loads(refreshed["manifest"].read_text(encoding="utf-8"))["pending_files"] == ["unit.md"]

    second_resume = decide_resume(request, plan, context)
    assert second_resume.completed_files == ()
    assert second_resume.pending_files == ("unit.md",)
    assert validate_markdown_subagent(tmp_path, "translate", source_dir, target_dir)["all_passed"]
    final = prepare_markdown_subagent(
        tmp_path, "translate", source_dir, target_dir, "English", "Chinese",
        resume=True, heading_contexts={"unit.md": refreshed_context},
    )
    assert __import__("json").loads(final["manifest"].read_text(encoding="utf-8"))["completed_files"] == ["unit.md"]
    legacy = {"context_sha256": {"snapshot": "old"}}
    assert not unit_context_is_current(
        "a.md", previous_manifest=legacy, controlled_files={},
        context_sha256={"snapshot": "new"}, unit_context_sha256={},
        heading_contexts={}, resume=True,
    )


def test_prompt_renderer_is_pure_and_keeps_manifest_guidance(tmp_path):
    request = MarkdownHandoffRequest(
        output_dir=tmp_path,
        task="translate",
        source_dir=tmp_path / "source",
        target_dir=tmp_path / "target",
        source_language="English",
        target_language="Chinese",
    )
    batching = {
        "max_source_tokens": 12000,
        "single_file_max_bytes": 30000,
        "max_nonempty_lines": 350,
        "max_chapters_per_worker": 3,
        "max_packed_source_tokens": 24000,
        "tiny_chapter_max_tokens": 2000,
        "max_tiny_chapters_per_worker": 6,
        "max_concurrency": 3,
    }
    MarkdownHandoffPlan(
        sources=[tmp_path / "chapter.md"], batching=batching,
        file_stats={"chapter.md": {"estimated_tokens": 12}},
        source_sha256={"chapter.md": "source-hash"}, normalized_roles={},
        controlled_files={}, continuation_files={}, isolated_files=None,
        task_max_files=5, recommended_batches=[["chapter.md"]], oversized_files=[],
    )
    context = normalize_handoff_context(request, ("chapter.md",))
    prompt = render_markdown_prompt(
        PromptRenderData(
            identity=PromptIdentity(
                task="translate", model="test-model", source_language="English",
                target_language="Chinese", source_dir="source", target_dir="target",
                manifest_name="translate_subagent_manifest.json", scratch_dir="scratch/translate",
                effective_max_concurrency=1, concurrency_reason="small unit",
            ),
            batching=PromptBatching(batching, 5), context=context,
            operational_paths=PromptOperationalPaths(),
        )
    )

    assert "Recommended Antigravity model: `test-model`" in prompt
    assert "Source units, glossary files, entity files" in prompt
    assert list(tmp_path.iterdir()) == []
