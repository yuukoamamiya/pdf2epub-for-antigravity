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
