import json

import pytest

from pdf2epub.metadata_enrichment.service import (
    MetadataEnrichmentRequiredError,
    enrichment_status,
    prepare_enrichment,
    resolve_enrichment,
    validate_enrichment,
)


def _fixture(tmp_path, mode="auto", applicability="publication"):
    output = tmp_path / "book"
    (output / "pages").mkdir(parents=True)
    (output / "pages" / "page_0001.md").write_text("Title page: A Work\nAuthor: Ada Writer", encoding="utf-8")
    config = {"title": "A Work", "target_language": "English", "metadata_enrichment": {"mode": mode, "fields": ["description", "identifiers"], "required_fields": ["description"] if mode == "required" else []}}
    prepare_enrichment(output, config)
    root = output / "metadata_enrichment"
    evidence = json.loads((root / "evidence.json").read_text(encoding="utf-8"))
    ref = {"location": evidence["evidence"][0]["location"], "sha256": evidence["evidence"][0]["sha256"]}
    decision = {
        "schema_version": 1,
        "evidence_sha256": evidence["evidence_sha256"],
        "applicability": {"status": applicability, "rationale": "Evidence reviewed", "evidence_refs": [ref]},
        "identity": {"status": "verified", "document_type": "book", "work_title": "A Work", "authors": [{"name": "Ada Writer", "role": "author"}], "source_edition": {}, "artifact_relation": "source edition", "evidence_refs": [ref]},
        "fields": {
            "description": {"status": "verified", "value": "<p>A reviewed description.</p>", "evidence_refs": [ref]},
            "identifiers": {"status": "needs_review", "value": {"doi": "10.1234/example"}, "evidence_refs": []},
        },
    }
    (root / "decision.json").write_text(json.dumps(decision), encoding="utf-8")
    return output, config


def test_resolve_requires_explicit_validation_and_only_returns_verified(tmp_path):
    output, config = _fixture(tmp_path)
    assert resolve_enrichment(output, config)["status"] == "pending_validation"
    status = enrichment_status(output, config)
    assert status["state"] == "pending_validation"
    assert status["validation_current"] is False
    assert status["eligible"] is False
    report = validate_enrichment(output, config)
    assert report["state"] == "incomplete"
    resolved = resolve_enrichment(output, config)
    assert resolved["verified_fields"] == ["description"]
    assert resolved["metadata"] == {"description": "<p>A reviewed description.</p>"}


def test_status_is_read_only_and_stale_evidence_blocks_resolution(tmp_path):
    output, config = _fixture(tmp_path)
    before = sorted(p.name for p in (output / "metadata_enrichment").iterdir())
    enrichment_status(output, config)
    assert sorted(p.name for p in (output / "metadata_enrichment").iterdir()) == before
    validate_enrichment(output, config)
    (output / "pages" / "page_0001.md").write_text("Changed evidence", encoding="utf-8")
    assert resolve_enrichment(output, config)["metadata"] == {}


def test_status_marks_changed_decision_as_pending_validation(tmp_path):
    output, config = _fixture(tmp_path)
    validate_enrichment(output, config)
    decision_path = output / "metadata_enrichment" / "decision.json"
    decision = json.loads(decision_path.read_text(encoding="utf-8"))
    decision["fields"]["description"]["value"] = "<p>Changed reviewed description.</p>"
    decision_path.write_text(json.dumps(decision), encoding="utf-8")
    status = enrichment_status(output, config)
    assert status["state"] == "pending_validation"
    assert status["validation_current"] is False
    assert status["eligible"] is False


def test_validation_context_changes_require_validate_but_irrelevant_config_does_not(tmp_path):
    output, config = _fixture(tmp_path)
    validate_enrichment(output, config)

    unrelated = {**config, "title": "Unrelated build title"}
    assert enrichment_status(output, unrelated)["validation_current"] is True

    changed_scope = json.loads(json.dumps(config))
    changed_scope["metadata_enrichment"]["fields"] = ["description"]
    status = enrichment_status(output, changed_scope)
    assert status["state"] == "pending_validation"
    assert status["validation_current"] is False
    assert resolve_enrichment(output, changed_scope)["status"] == "pending_validation"


def test_prepare_resume_requests_validation_for_existing_unvalidated_decision(tmp_path):
    output, config = _fixture(tmp_path)
    prompt_path = output / "metadata_enrichment" / "metadata_subagent_prompt.md"
    prompt_before = prompt_path.read_text(encoding="utf-8")
    resumed = prepare_enrichment(output, config, resume=True)
    assert resumed["state"] == "pending_validation"
    assert resumed["pending_commands"] == ["metadata-validate"]
    assert resumed["pending_units"] == []
    assert prompt_path.read_text(encoding="utf-8") == prompt_before


def test_source_file_hash_without_readable_evidence_waits_for_evidence(tmp_path):
    output = tmp_path / "book"
    output.mkdir()
    pdf = tmp_path / "input.pdf"
    pdf.write_bytes(b"pdf source bytes")
    config = {"input_pdf": str(pdf), "metadata_enrichment": {"mode": "required", "fields": ["description"], "required_fields": ["description"]}}
    prepared = prepare_enrichment(output, config)
    manifest = json.loads((output / "metadata_enrichment" / "manifest.json").read_text(encoding="utf-8"))
    assert prepared["state"] == "waiting_evidence"
    assert prepared["pending_fields"] == []
    assert manifest["state"] == "waiting_evidence"
    assert "no readable" in manifest["reason"]


def test_required_mode_allows_reviewed_not_applicable(tmp_path):
    output, config = _fixture(tmp_path, mode="required", applicability="non_publication")
    validate_enrichment(output, config)
    assert resolve_enrichment(output, config)["status"] == "not_applicable"


def test_required_mode_blocks_unreviewed_decision(tmp_path):
    output = tmp_path / "book"
    output.mkdir()
    config = {"metadata_enrichment": {"mode": "required", "fields": ["description"], "required_fields": ["description"]}}
    with pytest.raises(MetadataEnrichmentRequiredError):
        resolve_enrichment(output, config)


def test_required_subset_can_pass_while_optional_fields_remain_pending(tmp_path):
    output, config = _fixture(tmp_path, mode="required")
    report = validate_enrichment(output, config)
    assert report["state"] == "incomplete"
    assert report["missing_required_fields"] == []
    assert report["pending_fields"] == ["identifiers"]
    assert resolve_enrichment(output, config)["metadata"]["description"]
    status = enrichment_status(output, config)
    assert status["state"] == "incomplete"
    assert status["required_complete"] is True
