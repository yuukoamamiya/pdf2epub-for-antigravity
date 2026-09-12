import hashlib
import json
from pathlib import Path

import pytest

from pdf2epub.glossary import (
    GlossaryError,
    load_selected_glossaries,
    load_glossary,
    validate_translation_context,
)
from pdf2epub.subagent_workflow import prepare_markdown_subagent


def _write_glossary(path: Path, entries=None, name="domain"):
    entries = entries or [
        {
            "source": "Aufhebung",
            "variants": ["aufheben"],
            "target": "扬弃",
            "policy": "fixed",
        }
    ]
    path.write_text(
        "schema_version: 1\n"
        "metadata:\n"
        f"  name: {name}\n"
        "  source_language: German\n"
        "  target_language: Chinese\n"
        "entries:\n"
        + "\n".join(
            f"  - source: {entry['source']}\n"
            + "    target: " + entry["target"] + "\n"
            + "    policy: " + entry.get("policy", "preferred") + "\n"
            + ("    variants: [" + ", ".join(entry.get("variants", [])) + "]\n" if entry.get("variants") else "")
            for entry in entries
        ),
        encoding="utf-8",
    )


def test_load_glossary_normalizes_yaml(tmp_path: Path):
    source = tmp_path / "terms.yaml"
    _write_glossary(source)

    glossary = load_glossary(source)

    assert glossary["metadata"]["name"] == "domain"
    assert glossary["entries"][0]["target"] == "扬弃"
    assert glossary["entries"][0]["variants"] == ["aufheben"]


def test_selected_glossary_is_snapshotted_and_hashable(tmp_path: Path):
    source = tmp_path / "terms.yaml"
    output = tmp_path / "output"
    _write_glossary(source, name="german-classical-philosophy")

    bundle = load_selected_glossaries(
        {"translation": {"glossaries": [{"path": str(source)}]}},
        output,
        "German",
        "Chinese",
    )

    snapshot = bundle.context_files["domain_glossary_001"]
    assert snapshot.is_relative_to(output)
    assert snapshot.suffix == ".json"
    assert bundle.entries == 1
    assert bundle.context_sha256["domain_glossary_001"] == hashlib.sha256(
        snapshot.read_bytes()
    ).hexdigest()


def test_selected_glossaries_reject_conflicting_source_forms(tmp_path: Path):
    first = tmp_path / "first.yaml"
    second = tmp_path / "second.yaml"
    _write_glossary(first, name="first")
    _write_glossary(
        second,
        name="second",
        entries=[{"source": "Aufhebung", "target": "保留", "policy": "fixed"}],
    )

    with pytest.raises(GlossaryError, match="conflict"):
        load_selected_glossaries(
            {"translation": {"glossaries": [str(first), str(second)]}},
            tmp_path / "output",
            "German",
            "Chinese",
        )


def test_translation_context_validates_snapshot_hash(tmp_path: Path):
    source = tmp_path / "terms.yaml"
    output = tmp_path / "output"
    _write_glossary(source)
    bundle = load_selected_glossaries(
        {"translation": {"glossaries": [str(source),], "require_entities": False}},
        output,
        "German",
        "Chinese",
    )
    manifest = {
        "context_files": {
            name: str(path.relative_to(output)).replace("\\", "/")
            for name, path in bundle.context_files.items()
        },
        "context_sha256": bundle.context_sha256,
        "skipped_context_files": ["translation_entities"],
    }
    (output / "translate-html_subagent_manifest.json").write_text(
        json.dumps(manifest), encoding="utf-8"
    )

    report = validate_translation_context(
        output,
        "translate-html_subagent_manifest.json",
        {"title": "Book", "translation": {"require_entities": False, "glossaries": [str(source)]}},
    )

    assert report["valid"] is True
    assert report["glossary_entries"] == 1


def test_context_change_invalidates_resume_checkpoint(tmp_path: Path):
    source_dir = tmp_path / "source"
    target_dir = tmp_path / "target"
    source_dir.mkdir()
    target_dir.mkdir()
    (source_dir / "unit.md").write_text("source", encoding="utf-8")
    (target_dir / "unit.md").write_text("complete", encoding="utf-8")
    old_context = tmp_path / "context.json"
    old_context.write_text('{"version": 1}', encoding="utf-8")
    (tmp_path / "translate_subagent_manifest.json").write_text(
        json.dumps(
            {
                "context_sha256": {
                    "domain_glossary_001": hashlib.sha256(old_context.read_bytes()).hexdigest()
                }
            }
        ),
        encoding="utf-8",
    )
    (tmp_path / "translate_validation.json").write_text(
        json.dumps(
            {
                "valid_files": ["unit.md"],
                "source_sha256": {
                    "unit.md": hashlib.sha256(b"source").hexdigest()
                },
            }
        ),
        encoding="utf-8",
    )
    old_context.write_text('{"version": 2}', encoding="utf-8")

    paths = prepare_markdown_subagent(
        tmp_path,
        "translate",
        source_dir,
        target_dir,
        "German",
        "Chinese",
        resume=True,
        context_files={"domain_glossary_001": old_context},
    )
    manifest = json.loads(paths["manifest"].read_text(encoding="utf-8"))

    assert manifest["completed_files"] == []
    assert manifest["pending_files"] == ["unit.md"]
