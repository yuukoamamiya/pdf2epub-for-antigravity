import hashlib
import json
from pathlib import Path

import pytest

from pdf2epub.glossary import (
    GlossaryError,
    build_metadata_glossary_context,
    build_unit_glossary_contexts,
    discover_glossary_candidates,
    load_selected_glossaries,
    load_glossary,
    validate_translation_context,
)
from pdf2epub.entity_extractor import validate_entities
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
        "  domain: test domain\n"
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


def test_glossary_candidates_report_language_and_metadata_eligibility(tmp_path: Path):
    glossary_dir = tmp_path / "glossaries"
    glossary_dir.mkdir()
    valid = glossary_dir / "valid.yaml"
    _write_glossary(valid, name="valid")
    invalid = glossary_dir / "wrong-language.yaml"
    _write_glossary(invalid, name="wrong-language")
    invalid.write_text(
        invalid.read_text(encoding="utf-8").replace(
            "source_language: German", "source_language: English"
        ),
        encoding="utf-8",
    )

    candidates = discover_glossary_candidates(glossary_dir, "German", "Chinese")

    by_name = {item.get("name"): item for item in candidates}
    assert by_name["valid"]["eligible"] is True
    assert by_name["wrong-language"]["eligible"] is False
    assert any("source language mismatch" in error for error in by_name["wrong-language"]["errors"])


def test_glossary_candidates_mark_cross_language_reference_candidate(tmp_path: Path):
    glossary_dir = tmp_path / "glossaries"
    glossary_dir.mkdir()
    source = glossary_dir / "german.yaml"
    _write_glossary(source, name="german-classical-philosophy")

    candidates = discover_glossary_candidates(glossary_dir, "English", "Chinese")

    candidate = candidates[0]
    assert candidate["eligible"] is False
    assert candidate["reference_eligible"] is True
    assert candidate["reference_errors"] == []
    assert "source-language mismatch" in candidate["reference_reason"]


def test_selected_glossary_writes_provenance_record(tmp_path: Path):
    source = tmp_path / "terms.yaml"
    output = tmp_path / "output"
    _write_glossary(source, name="domain")

    load_selected_glossaries(
        {"translation": {"glossaries": [{"path": str(source), "id": "domain"}]}},
        output,
        "German",
        "Chinese",
    )

    record = json.loads((output / "glossary_selection.json").read_text(encoding="utf-8"))
    assert record["mode"] == "explicit"
    assert record["selected"][0]["source_sha256"] == hashlib.sha256(
        source.read_bytes()
    ).hexdigest()
    assert record["selected"][0]["snapshot_sha256"]


def test_reference_glossary_is_read_only_and_allows_source_language_mismatch(tmp_path: Path):
    source = tmp_path / "german-classical-philosophy.yaml"
    output = tmp_path / "output"
    _write_glossary(source, name="german-classical-philosophy")
    original_hash = hashlib.sha256(source.read_bytes()).hexdigest()

    bundle = load_selected_glossaries(
        {
            "translation": {
                "reference_glossaries": [
                    {"path": str(source), "id": "german-classical-philosophy"}
                ]
            }
        },
        output,
        "English",
        "Chinese",
    )

    reference_path = bundle.context_files["reference_glossary_001"]
    assert reference_path.name == "reference_german-classical-philosophy.json"
    assert hashlib.sha256(source.read_bytes()).hexdigest() == original_hash
    assert any("reference-only" in rule for rule in bundle.rules)
    record = json.loads((output / "glossary_selection.json").read_text(encoding="utf-8"))
    assert record["selected"][0]["kind"] == "reference"


def test_reference_glossary_context_validates_without_authoritative_language_match(tmp_path: Path):
    source = tmp_path / "german.yaml"
    output = tmp_path / "output"
    _write_glossary(source, name="german")
    bundle = load_selected_glossaries(
        {
            "translation": {
                "reference_glossaries": [str(source)],
                "require_entities": False,
            }
        },
        output,
        "English",
        "Chinese",
    )
    manifest = {
        "context_files": {
            name: path.relative_to(output).as_posix()
            for name, path in bundle.context_files.items()
        },
        "context_sha256": bundle.context_sha256,
        "skipped_context_files": ["translation_entities"],
    }
    manifest_path = output / "translate_subagent_manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    report = validate_translation_context(
        output,
        manifest_path.name,
        {
            "title": "Book",
            "translation": {
                "source_language": "English",
                "target_language": "Chinese",
                "require_entities": False,
                "reference_glossaries": [str(source)],
            },
        },
    )

    assert report["valid"] is True
    assert report["reference_glossaries"] == ["german"]
    assert report["reference_glossary_entries"] == 1


def test_unit_glossary_context_contains_only_matching_entries(tmp_path: Path):
    source = tmp_path / "terms.yaml"
    output = tmp_path / "output"
    _write_glossary(
        source,
        name="domain",
        entries=[
            {"source": "Aufhebung", "target": "扬弃", "policy": "fixed"},
            {"source": "Dasein", "target": "定在", "policy": "fixed"},
        ],
    )
    bundle = load_selected_glossaries(
        {"translation": {"glossaries": [str(source)]}},
        output,
        "German",
        "Chinese",
    )
    source_dir = tmp_path / "source"
    source_dir.mkdir()
    (source_dir / "unit.md").write_text("Das Dasein ist bestimmt.\n", encoding="utf-8")

    contexts = build_unit_glossary_contexts(
        output, source_dir, bundle.context_files
    )
    context = json.loads(contexts["unit.md"].read_text(encoding="utf-8"))
    assert [entry["source"] for entry in context["entries"]] == ["Dasein"]


def test_unit_glossary_context_is_compact_and_omits_empty_entity_notes(tmp_path: Path):
    output = tmp_path / "output"
    source_dir = tmp_path / "source"
    source_dir.mkdir()
    (source_dir / "unit.md").write_text("Hegel appears here.\n", encoding="utf-8")
    entity_path = output / "translation_entities.json"
    entity_path.parent.mkdir(parents=True)
    entity_path.write_text(
        json.dumps(
            {
                "characters": [{"original": "Hegel", "suggested_translation": "黑格尔"}],
                "places": [],
                "organizations": [],
                "terms": [],
                "races": [],
                "items": [],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    contexts = build_unit_glossary_contexts(output, source_dir, {}, entity_path)

    raw = contexts["unit.md"].read_text(encoding="utf-8")
    assert raw.count("\n") == 1
    context = json.loads(raw)
    assert context["entries"] == [
        {
            "kind": "book_entity",
            "category": "characters",
            "original": "Hegel",
            "target": "黑格尔",
        }
    ]


def test_metadata_glossary_context_selects_only_matching_entries(tmp_path: Path):
    output = tmp_path / "output"
    glossary = tmp_path / "terms.yaml"
    _write_glossary(
        glossary,
        name="domain",
        entries=[
            {"source": "Aufhebung", "target": "扬弃", "policy": "fixed"},
            {"source": "Dasein", "target": "定在", "policy": "fixed"},
        ],
    )
    bundle = load_selected_glossaries(
        {"translation": {"glossaries": [str(glossary)]}},
        output,
        "German",
        "Chinese",
    )

    context_path = build_metadata_glossary_context(
        output,
        bundle.context_files,
        "Aufhebung in the title",
    )

    assert context_path is not None
    context = json.loads(context_path.read_text(encoding="utf-8"))
    assert [entry["source"] for entry in context["entries"]] == ["Aufhebung"]


def test_entity_validation_rejects_conflicting_duplicate_originals():
    data = {
        "schema_version": 1,
        "metadata": {
            "book_title": "Book",
            "source_language": "German",
            "target_language": "Chinese",
            "extraction_complete": True,
        },
        "characters": [{"original": "Hegel", "suggested_translation": "黑格尔"}],
        "places": [{"original": "Hegel", "suggested_translation": "黑格尔（另一译法）"}],
        "organizations": [],
        "terms": [],
        "races": [],
        "items": [],
    }

    errors = validate_entities(data, "Book", "German", "Chinese")

    assert any("conflicts with characters[0]" in error for error in errors)


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
