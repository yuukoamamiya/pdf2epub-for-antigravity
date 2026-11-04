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


def test_validate_existing_outputs_before_second_resume_avoids_full_redelegation(tmp_path):
    source_dir = tmp_path / "source"
    target_dir = tmp_path / "target"
    source_dir.mkdir()
    target_dir.mkdir()
    source = source_dir / "unit.md"
    target = target_dir / "unit.md"
    source.write_text("Source paragraph.", encoding="utf-8")
    target.write_text("Existing translated paragraph.", encoding="utf-8")
    refreshed_context = {
        "projection_version": 3, "node_id": "n1", "anchor": "a1"
    }
    request = MarkdownHandoffRequest(
        output_dir=tmp_path, task="translate", source_dir=source_dir,
        target_dir=target_dir, source_language="English", target_language="Chinese",
        resume=True, heading_contexts={"unit.md": refreshed_context},
    )
    plan = plan_markdown_handoff(source_dir, "translate")
    context = normalize_handoff_context(request, ["unit.md"])
    manifest_path = tmp_path / "translate_subagent_manifest.json"
    manifest_path.write_text(
        __import__("json").dumps({
            "source_language": "English", "target_language": "Chinese",
            "context_sha256": {}, "unit_context_sha256": {},
            "toc_heading_contexts": {"unit.md": {"projection_version": 2}}, "special_unit_modes": {},
        }),
        encoding="utf-8",
    )
    validation_path = tmp_path / "translate_validation.json"
    validation_path.write_text(
        __import__("json").dumps({
            "schema_version": MARKDOWN_VALIDATION_SCHEMA_VERSION - 1,
            "task": "translate", "scope": "full", "all_passed": True,
            "valid_files": ["unit.md"],
            "source_sha256": {"unit.md": sha256_file(source)},
            "target_sha256": {"unit.md": sha256_file(target)},
        }),
        encoding="utf-8",
    )

    first_resume = decide_resume(request, plan, context)
    assert first_resume.completed_files == ()
    assert first_resume.pending_files == ("unit.md",)

    # Preparation refreshes the v3 projection without dispatch. Existing outputs
    # are then revalidated against it, producing a current schema-4 receipt.
    manifest = __import__("json").loads(manifest_path.read_text(encoding="utf-8"))
    manifest["toc_heading_contexts"] = {"unit.md": refreshed_context}
    manifest_path.write_text(__import__("json").dumps(manifest), encoding="utf-8")
    validation_path.write_text(
        __import__("json").dumps({
            "schema_version": MARKDOWN_VALIDATION_SCHEMA_VERSION,
            "task": "translate", "scope": "full", "all_passed": True,
            "valid_files": ["unit.md"],
            "source_sha256": {"unit.md": sha256_file(source)},
            "target_sha256": {"unit.md": sha256_file(target)},
        }),
        encoding="utf-8",
    )

    second_resume = decide_resume(request, plan, context)
    assert second_resume.completed_files == ("unit.md",)
    assert second_resume.pending_files == ()
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
