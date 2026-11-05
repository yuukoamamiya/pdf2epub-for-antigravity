from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from pdf2epub.html_translation.extraction import _balanced_line_parts
from pdf2epub.html_translation.extraction import (
    ExtractionWorkspace,
    HTMLExtractionPreparer,
)
from pdf2epub.html_translation.translation_validation import (
    HTMLTranslationValidator,
    HTMLValidationWorkspace,
)
from pdf2epub.markdown_handoff import prepare_markdown_subagent


def _workspace(tmp_path: Path) -> tuple[Path, Path, Path]:
    output = tmp_path / "output"
    source = output / "compressed_units"
    target = output / "translated_compressed"
    source.mkdir(parents=True)
    target.mkdir()
    return output, source, target


def _prepare(
    output: Path,
    source: Path,
    target: Path,
    *,
    source_language: str = "English",
    target_language: str = "Chinese",
    **kwargs,
):
    return prepare_markdown_subagent(
        output,
        "translate-html",
        source,
        target,
        source_language,
        target_language,
        config={"subagent": {"batching": {"max_concurrency": 1}}},
        declared_files=["chapter.md"],
        resume=True,
        **kwargs,
    )


def test_html_validated_receipt_resumes_completed_and_invalidates_context_or_safety(
    tmp_path: Path,
) -> None:
    output, source_dir, target_dir = _workspace(tmp_path)
    source = source_dir / "chapter.md"
    target = target_dir / "chapter.md"
    source.write_text("<i>Hello</i>\n", encoding="utf-8")
    (source_dir / "chapter.mapping.json").write_text(
        json.dumps({"original_extension": ".xhtml"}), encoding="utf-8"
    )
    (output / "input.epub").write_bytes(b"synthetic epub")
    prepare_paths = _prepare(output, source_dir, target_dir)
    manifest_path = prepare_paths["manifest"]
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    epub_hash = hashlib.sha256((output / "input.epub").read_bytes()).hexdigest()
    manifest["input_epub"] = "input.epub"
    manifest["input_epub_sha256"] = epub_hash
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    target.write_text("<i>你好</i>\n", encoding="utf-8")
    validator = HTMLTranslationValidator(
        HTMLValidationWorkspace(output, source_dir, target_dir, {"translation": {"target_language": "Chinese"}})
    )
    report = validator.validate(file_name="chapter.md")
    assert report["valid_files"] == ["chapter.md"]
    validator.persist_checkpoint(report)
    ledger_path = output / "translate-html_file_validation.json"
    ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
    assert ledger["schema_version"] == 3
    # Model a checkpoint from the old writer: no context projection or schema.
    ledger.pop("schema_version")
    ledger["files"]["chapter.md"].pop("resume_context")
    ledger_path.write_text(json.dumps(ledger), encoding="utf-8")

    paths = _prepare(output, source_dir, target_dir)
    resumed = json.loads(paths["manifest"].read_text(encoding="utf-8"))
    assert resumed["completed_files"] == ["chapter.md"]
    assert resumed["pending_files"] == []

    paths = _prepare(
        output,
        source_dir,
        target_dir,
        heading_contexts={"chapter.md": {"binding_mode": "visible", "titles": ["Changed"]}},
    )
    resumed = json.loads(paths["manifest"].read_text(encoding="utf-8"))
    assert resumed["pending_files"] == ["chapter.md"]
    assert resumed["html_invalidated_checkpoints"]["chapter.md"]

    paths = _prepare(
        output,
        source_dir,
        target_dir,
        heading_contexts={"chapter.md": {"binding_mode": "visible", "titles": ["Changed"]}},
    )
    resumed = json.loads(paths["manifest"].read_text(encoding="utf-8"))
    assert resumed["pending_files"] == ["chapter.md"]
    assert resumed["html_invalidated_checkpoints"]["chapter.md"]

    # Revalidation under the new context replaces the legacy receipt binding.
    manifest = json.loads(paths["manifest"].read_text(encoding="utf-8"))
    manifest["input_epub"] = "input.epub"
    manifest["input_epub_sha256"] = epub_hash
    paths["manifest"].write_text(json.dumps(manifest), encoding="utf-8")
    validator.persist_checkpoint(validator.validate(file_name="chapter.md"))
    paths = _prepare(
        output,
        source_dir,
        target_dir,
        heading_contexts={"chapter.md": {"binding_mode": "visible", "titles": ["Changed"]}},
    )
    resumed = json.loads(paths["manifest"].read_text(encoding="utf-8"))
    assert resumed["completed_files"] == ["chapter.md"]
    assert "html_invalidated_checkpoints" not in resumed

    ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
    ledger["files"]["chapter.md"]["safety_blocked"] = True
    ledger_path.write_text(json.dumps(ledger), encoding="utf-8")
    paths = _prepare(
        output,
        source_dir,
        target_dir,
        heading_contexts={"chapter.md": {"binding_mode": "visible", "titles": ["Changed"]}},
    )
    resumed = json.loads(paths["manifest"].read_text(encoding="utf-8"))
    assert resumed["pending_files"] == ["chapter.md"]


def test_html_special_role_receipt_is_persisted_and_reused_after_validation(tmp_path):
    output, source_dir, target_dir = _workspace(tmp_path)
    source_name = "bibliography.md"
    source = source_dir / source_name
    target = target_dir / source_name
    source.write_text("<p>References 1999</p>\n", encoding="utf-8")
    (source_dir / "bibliography.mapping.json").write_text(
        json.dumps({"original_extension": ".xhtml", "translation_parts": [source_name]}),
        encoding="utf-8",
    )
    input_epub = output / "input.epub"
    input_epub.write_bytes(b"synthetic epub")
    roles = {source_name: "bibliography"}
    role_sources = {source_name: {"source": "filename_inference", "reason": "bibliography filename"}}
    paths = prepare_markdown_subagent(
        output, "translate-html", source_dir, target_dir, "English", "Chinese",
        config={"subagent": {"batching": {"max_concurrency": 1}}},
        declared_files=[source_name], file_roles=roles,
        file_role_sources=role_sources,
    )
    manifest = json.loads(paths["manifest"].read_text(encoding="utf-8"))
    manifest["input_epub"] = "input.epub"
    manifest["input_epub_sha256"] = hashlib.sha256(input_epub.read_bytes()).hexdigest()
    paths["manifest"].write_text(json.dumps(manifest), encoding="utf-8")
    target.write_text("<p>参考文献 1999</p>\n", encoding="utf-8")
    validator = HTMLTranslationValidator(HTMLValidationWorkspace(
        output, source_dir, target_dir, {"translation": {"target_language": "Chinese"}}
    ))
    report = validator.validate(file_name=source_name)
    assert report["valid_files"] == [source_name]
    assert report["resume_contexts"][source_name]["file_roles"] == roles
    validator.persist_checkpoint(report)

    resumed_paths = prepare_markdown_subagent(
        output, "translate-html", source_dir, target_dir, "English", "Chinese",
        config={"subagent": {"batching": {"max_concurrency": 1}}},
        declared_files=[source_name], resume=True, file_roles=roles,
        file_role_sources=role_sources,
    )
    resumed = json.loads(resumed_paths["manifest"].read_text(encoding="utf-8"))
    assert resumed["completed_files"] == [source_name]
    assert resumed["pending_files"] == []

@pytest.mark.parametrize("missing_hash", ["source_sha256", "target_sha256"])
def test_versionless_html_ledger_requires_both_current_hashes_and_valid_record(
    tmp_path: Path, missing_hash: str,
) -> None:
    output, source_dir, target_dir = _workspace(tmp_path)
    source = source_dir / "chapter.md"
    target = target_dir / "chapter.md"
    source.write_text("<i>Hello</i>\n", encoding="utf-8")
    (source_dir / "chapter.mapping.json").write_text(
        json.dumps({"original_extension": ".xhtml"}), encoding="utf-8"
    )
    (output / "input.epub").write_bytes(b"synthetic epub")
    paths = _prepare(output, source_dir, target_dir)
    manifest = json.loads(paths["manifest"].read_text(encoding="utf-8"))
    manifest["input_epub"] = "input.epub"
    manifest["input_epub_sha256"] = hashlib.sha256((output / "input.epub").read_bytes()).hexdigest()
    paths["manifest"].write_text(json.dumps(manifest), encoding="utf-8")
    target.write_text("<i>你好</i>\n", encoding="utf-8")
    validator = HTMLTranslationValidator(
        HTMLValidationWorkspace(output, source_dir, target_dir, {"translation": {"target_language": "Chinese"}})
    )
    report = validator.validate(file_name="chapter.md")
    validator.persist_checkpoint(report)
    ledger_path = output / "translate-html_file_validation.json"
    ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
    ledger.pop("schema_version")  # legacy HTML receipt shape
    ledger_path.write_text(json.dumps(ledger), encoding="utf-8")
    paths = _prepare(output, source_dir, target_dir)
    resumed = json.loads(paths["manifest"].read_text(encoding="utf-8"))
    assert resumed["completed_files"] == ["chapter.md"]

    ledger["files"]["chapter.md"].pop(missing_hash)
    ledger_path.write_text(json.dumps(ledger), encoding="utf-8")
    paths = _prepare(output, source_dir, target_dir)
    resumed = json.loads(paths["manifest"].read_text(encoding="utf-8"))
    assert resumed["pending_files"] == ["chapter.md"]


def test_legacy_html_receipt_stays_invalid_after_target_language_switch(
    tmp_path: Path,
) -> None:
    output, source_dir, target_dir = _workspace(tmp_path)
    source = source_dir / "chapter.md"
    target = target_dir / "chapter.md"
    source.write_text("<i>Hello</i>\n", encoding="utf-8")
    (source_dir / "chapter.mapping.json").write_text(
        json.dumps({"original_extension": ".xhtml"}), encoding="utf-8"
    )
    (output / "input.epub").write_bytes(b"synthetic epub")
    paths = _prepare(output, source_dir, target_dir)
    manifest = json.loads(paths["manifest"].read_text(encoding="utf-8"))
    manifest["input_epub"] = "input.epub"
    manifest["input_epub_sha256"] = hashlib.sha256((output / "input.epub").read_bytes()).hexdigest()
    paths["manifest"].write_text(json.dumps(manifest), encoding="utf-8")
    target.write_text("<i>你好</i>\n", encoding="utf-8")
    validator = HTMLTranslationValidator(
        HTMLValidationWorkspace(output, source_dir, target_dir, {"translation": {"target_language": "Chinese"}})
    )
    validator.persist_checkpoint(validator.validate(file_name="chapter.md"))
    ledger_path = output / "translate-html_file_validation.json"
    ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
    ledger.pop("schema_version")
    ledger["files"]["chapter.md"].pop("resume_context")
    ledger_path.write_text(json.dumps(ledger), encoding="utf-8")

    for _ in range(2):
        paths = _prepare(
            output,
            source_dir,
            target_dir,
            target_language="Japanese",
        )
        resumed = json.loads(paths["manifest"].read_text(encoding="utf-8"))
        assert resumed["pending_files"] == ["chapter.md"]


@pytest.mark.parametrize("mutation", ["source", "target"])
def test_html_receipt_is_invalidated_when_source_or_target_changes(
    tmp_path: Path, mutation: str
) -> None:
    output, source_dir, target_dir = _workspace(tmp_path)
    source = source_dir / "chapter.md"
    target = target_dir / "chapter.md"
    source.write_text("<i>Hello</i>\n", encoding="utf-8")
    (source_dir / "chapter.mapping.json").write_text(
        json.dumps({"original_extension": ".xhtml"}), encoding="utf-8"
    )
    (output / "input.epub").write_bytes(b"synthetic epub")
    paths = _prepare(output, source_dir, target_dir)
    manifest = json.loads(paths["manifest"].read_text(encoding="utf-8"))
    manifest["input_epub"] = "input.epub"
    manifest["input_epub_sha256"] = hashlib.sha256((output / "input.epub").read_bytes()).hexdigest()
    paths["manifest"].write_text(json.dumps(manifest), encoding="utf-8")
    target.write_text("<i>你好</i>\n", encoding="utf-8")
    validator = HTMLTranslationValidator(
        HTMLValidationWorkspace(output, source_dir, target_dir, {"translation": {"target_language": "Chinese"}})
    )
    validator.persist_checkpoint(validator.validate(file_name="chapter.md"))

    changed_file = source if mutation == "source" else target
    changed_file.write_text(
        "<i>Changed</i>\n" if mutation == "source" else "<i>改过</i>\n",
        encoding="utf-8",
    )
    paths = _prepare(output, source_dir, target_dir)
    resumed = json.loads(paths["manifest"].read_text(encoding="utf-8"))
    assert resumed["pending_files"] == ["chapter.md"]


def test_blocked_html_ledger_overrides_older_full_validation_receipt(
    tmp_path: Path,
) -> None:
    output, source_dir, target_dir = _workspace(tmp_path)
    source = source_dir / "chapter.md"
    target = target_dir / "chapter.md"
    source.write_text("<i>Hello</i>\n", encoding="utf-8")
    (source_dir / "chapter.mapping.json").write_text(
        json.dumps({"original_extension": ".xhtml"}), encoding="utf-8"
    )
    target.write_text("<i>你好</i>\n", encoding="utf-8")
    _prepare(output, source_dir, target_dir)
    source_hash = hashlib.sha256(source.read_bytes()).hexdigest()
    target_hash = hashlib.sha256(target.read_bytes()).hexdigest()
    (output / "translate-html_validation.json").write_text(
        json.dumps({
            "schema_version": 3,
            "task": "translate-html",
            "scope": "book",
            "all_passed": True,
            "source_snapshot": {"valid": True},
            "valid_files": ["chapter.md"],
            "source_sha256": {"chapter.md": source_hash},
            "target_sha256": {"chapter.md": target_hash},
        }),
        encoding="utf-8",
    )
    (output / "translate-html_file_validation.json").write_text(
        json.dumps({
            "schema_version": 3,
            "task": "translate-html",
            "scope": "file-checkpoints",
            "files": {
                "chapter.md": {
                    "valid": False,
                    "safety_blocked": True,
                    "source_sha256": source_hash,
                    "target_sha256": target_hash,
                }
            },
        }),
        encoding="utf-8",
    )

    paths = _prepare(output, source_dir, target_dir)
    resumed = json.loads(paths["manifest"].read_text(encoding="utf-8"))
    assert resumed["pending_files"] == ["chapter.md"]


def test_partial_html_full_report_keeps_valid_unit_resumeable_but_stale_snapshot_does_not(
    tmp_path: Path,
) -> None:
    output, source_dir, target_dir = _workspace(tmp_path)
    source = source_dir / "chapter.md"
    target = target_dir / "chapter.md"
    source.write_text("<i>Hello</i>\n", encoding="utf-8")
    (source_dir / "chapter.mapping.json").write_text(
        json.dumps({"original_extension": ".xhtml"}), encoding="utf-8"
    )
    target.write_text("<i>你好</i>\n", encoding="utf-8")
    _prepare(output, source_dir, target_dir)
    validation_path = output / "translate-html_validation.json"
    receipt = {
        "schema_version": 3,
        "task": "translate-html",
        "scope": "book",
        "all_passed": False,
        "source_snapshot": {"valid": True},
        "metadata": {"valid": False},
        "valid_files": ["chapter.md"],
        "source_sha256": {"chapter.md": hashlib.sha256(source.read_bytes()).hexdigest()},
        "target_sha256": {"chapter.md": hashlib.sha256(target.read_bytes()).hexdigest()},
    }
    validation_path.write_text(json.dumps(receipt), encoding="utf-8")

    paths = _prepare(output, source_dir, target_dir)
    resumed = json.loads(paths["manifest"].read_text(encoding="utf-8"))
    assert resumed["completed_files"] == ["chapter.md"]

    receipt["source_snapshot"] = {"valid": False}
    validation_path.write_text(json.dumps(receipt), encoding="utf-8")
    paths = _prepare(output, source_dir, target_dir)
    resumed = json.loads(paths["manifest"].read_text(encoding="utf-8"))
    assert resumed["pending_files"] == ["chapter.md"]


@pytest.mark.parametrize(
    "blocked_fields",
    [
        {"safety_blocked": ["chapter.md"]},
        {"source_snapshot": {"valid": False}},
    ],
)
def test_checkpoint_writer_never_marks_safety_or_snapshot_failure_valid(
    tmp_path: Path, blocked_fields: dict,
) -> None:
    output, source_dir, target_dir = _workspace(tmp_path)
    validator = HTMLTranslationValidator(
        HTMLValidationWorkspace(output, source_dir, target_dir, {})
    )
    report = {
        "file": "chapter.md",
        "all_passed": True,
        "valid_files": ["chapter.md"],
        "missing": [],
        "source_sha256": {"chapter.md": "a" * 64},
        "target_sha256": {"chapter.md": "b" * 64},
        "source_snapshot": {"valid": True},
        "safety_blocked": [],
        **blocked_fields,
    }

    validator.persist_checkpoint(report)

    ledger = json.loads((output / "translate-html_file_validation.json").read_text(encoding="utf-8"))
    assert ledger["files"]["chapter.md"]["valid"] is False


def test_balanced_split_keeps_whole_lines_ordered_and_within_line_cap() -> None:
    lines = [f"<p>Entry {index:03d}</p>" for index in range(428)]

    parts = _balanced_line_parts(lines, 350)

    assert [len(part) for part in parts] == [214, 214]
    assert [line for part in parts for line in part] == lines
    assert all(len(part) <= 350 for part in parts)


def test_new_extraction_balances_parts_and_current_cache_preserves_translation(
    tmp_path: Path,
) -> None:
    class Parser:
        resources = {"css": []}
        spine = [{"href": "text/Index.xhtml", "full_href": "OEBPS/text/Index.xhtml"}]

        def get_raw_content(self, _href: str) -> str:
            return "<html><body>" + "".join(
                f"<p>Entry {index:03d}</p>" for index in range(428)
            ) + "</body></html>"

    output = tmp_path / "output"
    units = output / "compressed_units"
    output.mkdir()
    units.mkdir()
    epub = output / "input.epub"
    epub.write_bytes(b"synthetic epub")
    config = {"subagent": {"batching": {"max_nonempty_lines": 350}}}
    preparer = HTMLExtractionPreparer(
        ExtractionWorkspace(
            epub,
            output,
            units,
            Parser(),
            config,
            {},
            "English",
            "Book",
            lambda **_kwargs: None,
        )
    )

    assert preparer.prepare() == 1
    mapping_path = next(units.glob("*.mapping.json"))
    mapping = json.loads(mapping_path.read_text(encoding="utf-8"))
    parts = [units / name for name in mapping["translation_parts"]]
    assert [len(path.read_text(encoding="utf-8").splitlines()) for path in parts] == [214, 214]
    lines = parts[0].read_text(encoding="utf-8").splitlines() + parts[1].read_text(encoding="utf-8").splitlines()
    parts[0].write_text("\n".join(lines[:350]) + "\n", encoding="utf-8")
    parts[1].write_text("\n".join(lines[350:]) + "\n", encoding="utf-8")
    preparer._write_compressed_cache(max_nonempty_lines=350)
    source_parts = [path.read_bytes() for path in parts]
    translated = output / "translated_compressed"
    translated.mkdir()
    in_progress = translated / parts[0].name
    in_progress.write_text("<p>已有译文</p>\n", encoding="utf-8")

    assert preparer.prepare() == 1
    assert [len(path.read_text(encoding="utf-8").splitlines()) for path in parts] == [350, 78]
    assert [path.read_bytes() for path in parts] == source_parts
    assert in_progress.read_text(encoding="utf-8") == "<p>已有译文</p>\n"


def test_balanced_split_respects_byte_load_and_never_splits_markup_lines() -> None:
    long = "<p>" + "long &amp; tagged " * 30 + "</p>"
    short = "<p>短</p>"
    lines = [long, long, long, long, short, short]

    parts = _balanced_line_parts(lines, 4)

    assert len(parts) == 2
    assert [len(part) for part in parts] == [2, 4]
    assert [line for part in parts for line in part] == lines
    assert all(len(part) <= 4 for part in parts)
    sizes = [sum(len(line.encode("utf-8")) for line in part) for part in parts]
    assert max(sizes) - min(sizes) == 2 * len(short.encode("utf-8"))
    old_greedy_sizes = [
        sum(len(line.encode("utf-8")) for line in part)
        for part in (lines[:4], lines[4:])
    ]
    assert max(sizes) < max(old_greedy_sizes)


@pytest.mark.parametrize("max_lines", [0, -1])
def test_balanced_split_rejects_nonpositive_limit(max_lines: int) -> None:
    with pytest.raises(ValueError, match="positive"):
        _balanced_line_parts(["line"], max_lines)
