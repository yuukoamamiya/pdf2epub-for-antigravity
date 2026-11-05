import json
from pathlib import Path

import pytest

from pdf2epub.markdown_handoff import prepare_markdown_subagent
from pdf2epub.subagent_runtime import write_worker_handoffs


def _prepare(tmp_path: Path, task: str, files: dict[str, dict]):
    output = tmp_path / "output"
    source = output / "source"
    target = output / "target"
    contexts = output / "translation_glossaries" / "unit_contexts"
    source.mkdir(parents=True)
    target.mkdir(parents=True)
    contexts.mkdir(parents=True)
    unit_context_files = {}
    for filename, context in files.items():
        (source / filename).write_text(f"Source for {filename}\n", encoding="utf-8")
        context_path = contexts / f"{Path(filename).stem}.json"
        context_path.write_text(json.dumps(context), encoding="utf-8")
        unit_context_files[filename] = context_path

    paths = prepare_markdown_subagent(
        output,
        task,
        source,
        target,
        "English",
        "Chinese",
        unit_context_files=unit_context_files,
        config={"subagent": {"batching": {"max_files": 1, "max_concurrency": 4}}},
    )
    return output, paths


def _handoffs(output: Path, paths):
    return write_worker_handoffs(output, paths["manifest"], paths["prompt"])


def test_polish_footnote_context_is_scoped_but_not_projected_as_glossary(tmp_path: Path):
    footnote_context = {"unit_context_state": "reviewed", "footnotes": [{"id": "1"}]}
    output, paths = _prepare(tmp_path, "polish", {"chapter.md": footnote_context})

    handoff = _handoffs(output, paths)[0]
    scoped = json.loads((output / handoff["manifest"]).read_text(encoding="utf-8"))

    assert set(scoped["unit_context_files"]) == {"chapter.md"}
    context_path = output / scoped["unit_context_files"]["chapter.md"]
    assert context_path.is_file()
    assert "unit_context" in context_path.as_posix()
    assert "worker_context_files" not in scoped


def test_translate_html_projects_entries_and_limits_scoped_context_to_assignment(tmp_path: Path):
    contexts = {
        "one.md": {"entries": [{"source": "Aufhebung", "target": "扬弃"}]},
        "two.md": {"entries": [{"source": "Geist", "target": "精神"}]},
    }
    output, paths = _prepare(tmp_path, "translate-html", contexts)

    handoffs = _handoffs(output, paths)
    assert len(handoffs) == 2
    for handoff in handoffs:
        scoped = json.loads((output / handoff["manifest"]).read_text(encoding="utf-8"))
        filename = handoff["files"][0]
        assert set(scoped["unit_context_files"]) == {filename}
        assert filename not in scoped["unit_context_files"].get("other.md", "")
        assert set(scoped["worker_context_files"]) == {handoff["worker_id"]}
        payload_path = output / scoped["worker_context_files"][handoff["worker_id"]]
        payload = json.loads(payload_path.read_text(encoding="utf-8"))
        assert filename in json.dumps(payload, ensure_ascii=False)
        other = "two.md" if filename == "one.md" else "one.md"
        assert other not in json.dumps(payload, ensure_ascii=False)


def test_translate_novel_is_treated_as_translation_task(tmp_path: Path):
    output, paths = _prepare(
        tmp_path,
        "translate-novel",
        {"chapter.md": {"entries": [{"source": "Welt", "target": "世界"}]}},
    )

    handoff = _handoffs(output, paths)[0]
    scoped = json.loads((output / handoff["manifest"]).read_text(encoding="utf-8"))
    assert scoped["worker_context_files"]


def test_malformed_translation_entries_still_fail(tmp_path: Path):
    output, paths = _prepare(tmp_path, "translate-html", {"chapter.md": {"entries": "invalid"}})

    with pytest.raises(ValueError, match="malformed unit terminology context"):
        _handoffs(output, paths)
