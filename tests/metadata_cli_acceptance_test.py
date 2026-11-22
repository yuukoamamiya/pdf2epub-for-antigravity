"""Exercise metadata commands with an external config and real source changes."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest

from pdf2epub.commands.metadata import (
    metadata_prepare_command,
    metadata_status_command,
    metadata_validate_command,
)
from pdf2epub.metadata_enrichment.service import enrichment_status, prepare_enrichment, resolve_enrichment, validate_enrichment
from pdf2epub.utils.configuration import load_config


def test_config_relative_input_review_resume_and_read_only_status(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    source = tmp_path / "source.pdf"
    source.write_bytes(b"%PDF-synthetic-source-v1")
    config_dir = tmp_path / "configs"
    config_dir.mkdir()
    config_path = config_dir / "book.yaml"
    config_path.write_text(
        "title: Acceptance Book\npipeline: epub_conversion\n"
        "input_pdf: ../source.pdf\n"
        "metadata_enrichment:\n  fields: [identifiers]\n  online: false\n",
        encoding="utf-8",
    )
    output = tmp_path / "output" / "Acceptance Book"
    (output / "pages").mkdir(parents=True)
    page = output / "pages" / "page_0001.md"
    page.write_text("Synthetic article. DOI: 10.1234/acceptance", encoding="utf-8")
    args = argparse.Namespace(config=str(config_path), json=True, resume=True)
    assert metadata_prepare_command(args) == 0
    root = output / "metadata_enrichment"
    evidence = json.loads((root / "evidence.json").read_text(encoding="utf-8"))
    assert Path(evidence["source"]["path"]).resolve() == source
    reference = {key: evidence["evidence"][0][key] for key in ("location", "sha256")}
    decision = {
        "schema_version": 1,
        "evidence_sha256": evidence["evidence_sha256"],
        "applicability": {"status": "publication", "rationale": "Synthetic journal article", "evidence_refs": [reference]},
        "identity": {
            "status": "verified", "document_type": "article", "work_title": "Acceptance Article",
            "authors": [{"name": "Ada Writer", "role": "author"}],
            "source_edition": {}, "artifact_relation": "source", "evidence_refs": [reference],
        },
        "fields": {"identifiers": {"status": "verified", "value": {"doi": "10.1234/acceptance"}, "evidence_refs": [reference]}},
    }
    (root / "decision.json").write_text(json.dumps(decision), encoding="utf-8")
    assert metadata_validate_command(args) == 0
    config = load_config(str(config_path))
    plan = resolve_enrichment(output, config, config_path=config_path)
    assert plan["metadata"]["identifiers"]["doi"] == "10.1234/acceptance"
    before = {p.relative_to(root): (p.read_bytes(), p.stat().st_mtime_ns) for p in root.rglob("*") if p.is_file()}
    assert metadata_status_command(args) == 0
    after = {p.relative_to(root): (p.read_bytes(), p.stat().st_mtime_ns) for p in root.rglob("*") if p.is_file()}
    assert before == after
    resumed = prepare_enrichment(output, config, config_path=config_path, resume=True)
    assert resumed["pending_fields"] == []
    assert before == {p.relative_to(root): (p.read_bytes(), p.stat().st_mtime_ns) for p in root.rglob("*") if p.is_file()}
    # Editing an unrelated chapter must not invalidate front-matter evidence.
    (output / "unrelated-chapter.md").write_text("Unrelated body update", encoding="utf-8")
    assert resolve_enrichment(output, config, config_path=config_path)["metadata"]["identifiers"]["doi"] == "10.1234/acceptance"
    source.write_bytes(b"%PDF-synthetic-source-v2")
    assert resolve_enrichment(output, config, config_path=config_path)["metadata"] == {}


def test_disabled_commands_do_not_create_a_book_workspace(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    config = tmp_path / "book.yaml"
    config.write_text(
        "title: Disabled Book\npipeline: epub_conversion\n"
        "metadata_enrichment:\n  mode: disabled\n", encoding="utf-8",
    )
    args = argparse.Namespace(config=str(config), json=True, resume=True)
    assert metadata_status_command(args) == 0
    assert not (tmp_path / "output").exists()


@pytest.mark.parametrize("source_code,accepted", [("de", True), ("fr", False)])
def test_source_isbn_language_matches_named_config_language(tmp_path, source_code, accepted):
    (tmp_path / "pages").mkdir()
    (tmp_path / "pages" / "page_0001.md").write_text("Synthetic German copyright page", encoding="utf-8")
    config = {
        "translation": {"source_language": "German", "target_language": "English"},
        "metadata_enrichment": {"fields": ["identifiers"], "online": False},
    }
    prepare_enrichment(tmp_path, config)
    root = tmp_path / "metadata_enrichment"
    evidence = json.loads((root / "evidence.json").read_text(encoding="utf-8"))
    reference = {key: evidence["evidence"][0][key] for key in ("location", "sha256")}
    decision = {
        "schema_version": 1,
        "evidence_sha256": evidence["evidence_sha256"],
        "applicability": {"status": "publication", "rationale": "Synthetic fixture", "evidence_refs": [reference]},
        "identity": {"status": "verified", "document_type": "book", "work_title": "Fixture",
                     "authors": [{"name": "Test Author", "role": "author"}],
                     "source_language": "de", "source_edition": "German edition", "artifact_relation": "translation",
                     "evidence_refs": [reference]},
        "fields": {"identifiers": {"status": "verified", "value": {
            "isbn": "9780306406157", "isbn_role": "translation_source", "source_language": source_code,
            "relationship_note": "The ISBN identifies the German source edition, not this translation.",
        }, "evidence_refs": [reference]}},
    }
    (root / "decision.json").write_text(json.dumps(decision), encoding="utf-8")
    report = validate_enrichment(tmp_path, config)
    assert report["fields"]["identifiers"]["verified"] is accepted
    plan = resolve_enrichment(tmp_path, config)
    if accepted:
        assert plan["metadata"]["source_isbn"] == "9780306406157"
    else:
        assert "identifiers" not in plan["metadata"]


@pytest.mark.parametrize("broken_key", ["applicability", "identity", "fields"])
def test_malformed_decision_reports_invalid_without_crashing_or_writing_status(tmp_path, broken_key):
    (tmp_path / "pages").mkdir()
    (tmp_path / "pages" / "page_0001.md").write_text("Synthetic evidence", encoding="utf-8")
    config = {"metadata_enrichment": {"fields": ["identifiers"], "online": False}}
    prepare_enrichment(tmp_path, config)
    root = tmp_path / "metadata_enrichment"
    evidence = json.loads((root / "evidence.json").read_text(encoding="utf-8"))
    reference = {key: evidence["evidence"][0][key] for key in ("location", "sha256")}
    decision = {
        "schema_version": 1, "evidence_sha256": evidence["evidence_sha256"],
        "applicability": {"status": "uncertain", "rationale": "Uncertain fixture", "evidence_refs": [reference]},
        "identity": {"status": "uncertain"}, "fields": {},
    }
    decision[broken_key] = []
    (root / "decision.json").write_text(json.dumps(decision), encoding="utf-8")
    before = {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in root.iterdir() if p.is_file()}
    report = validate_enrichment(tmp_path, config, persist=False)
    assert report["state"] == "invalid"
    assert report["errors"]
    assert enrichment_status(tmp_path, config)["state"] == "invalid"
    assert before == {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in root.iterdir() if p.is_file()}


@pytest.mark.parametrize("applicability,mode", [("non_publication", "required"), ("uncertain", "auto")])
def test_resume_preserves_reviewed_skip_without_redispatch(tmp_path, applicability, mode):
    config = {"metadata_enrichment": {"mode": mode, "fields": ["identifiers"], "online": False,
                                      "source_note": "A synthetic personal notebook with no publication record."}}
    prepare_enrichment(tmp_path, config)
    root = tmp_path / "metadata_enrichment"
    evidence = json.loads((root / "evidence.json").read_text(encoding="utf-8"))
    from hashlib import sha256
    reference = {"location": "config:metadata_enrichment.source_note",
                 "sha256": sha256(config["metadata_enrichment"]["source_note"].encode()).hexdigest()}
    decision = {
        "schema_version": 1, "evidence_sha256": evidence["evidence_sha256"],
        "applicability": {"status": applicability, "rationale": "Reviewed synthetic user note", "evidence_refs": [reference]},
        "identity": {"status": "uncertain"}, "fields": {},
    }
    (root / "decision.json").write_text(json.dumps(decision), encoding="utf-8")
    assert validate_enrichment(tmp_path, config)["state"] == "valid"
    before = {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in root.iterdir() if p.is_file()}
    resumed = prepare_enrichment(tmp_path, config, resume=True)
    assert resumed["pending_fields"] == []
    assert resumed["pending_units"] == []
    assert before == {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in root.iterdir() if p.is_file()}
