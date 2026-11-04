import json

from pdf2epub.terminology_memory_validation import validate_dynamic_terminology_units
from pdf2epub.markdown_subagent_validation import validate_markdown_subagent
from pdf2epub.dynamic_terminology import build_dynamic_unit_contexts, load_current_snapshot
from pdf2epub.dynamic_terminology import (
    affected_target_consistency_state,
    attach_candidate_sidecars,
    prepare_review_packet,
    publish_review_packet,
    session_attestation,
)
from pdf2epub.html_translation.translation_validation import (
    HTMLTranslationValidator,
    HTMLValidationWorkspace,
)
from pdf2epub.markdown_handoff import prepare_markdown_subagent
from pdf2epub.subagent_runtime import write_worker_handoffs
from pdf2epub.workflow_contracts import atomic_write_json
from tests.terminology_memory_validation_test import (
    _build_real_publish_fixture,
    _dynamic_manifest_fields,
)


def test_pdf_validator_gates_before_staging_and_check_only_is_read_only(tmp_path):
    output, source, target, config = _build_real_publish_fixture(tmp_path)
    first = validate_markdown_subagent(
        output, "translate", source, target,
        create_validated_copy=True,
        target_language="Chinese",
        dynamic_terminology_config=config,
    )
    assert first["all_passed"] is True
    assert first["dynamic_terminology"]["valid"] is True
    validated_part = output / "translated" / "validated" / "part1.md"
    assert validated_part.is_file()

    target_part = target / "part1.md"
    target_before = target_part.read_bytes()
    receipt_path = output / "translate_validation.json"
    receipt_before = receipt_path.read_bytes()
    validated_before = validated_part.read_bytes()
    checked = validate_markdown_subagent(
        output, "translate", source, target,
        create_validated_copy=False,
        selected_files=["part1.md"],
        target_language="Chinese",
        dynamic_terminology_config=config,
    )
    assert checked["all_passed"] is True
    assert target_part.read_bytes() == target_before
    assert receipt_path.read_bytes() == receipt_before
    assert validated_part.read_bytes() == validated_before


def test_pdf_memory_consistency_failure_keeps_raw_target_and_blocks_validated_copy(tmp_path):
    output, source, target, config = _build_real_publish_fixture(tmp_path)
    target_part = target / "part1.md"
    target_part.write_text("社会形式，在前一章出现", encoding="utf-8")
    raw_bytes = target_part.read_bytes()

    report = validate_markdown_subagent(
        output, "translate", source, target,
        create_validated_copy=True,
        selected_files=["part1.md"],
        target_language="Chinese",
        dynamic_terminology_config=config,
    )
    assert report["all_passed"] is False
    assert report["dynamic_terminology"]["valid"] is False
    assert "part1.md" not in report["valid_files"]
    assert any("consistency review" in item["error"] for item in report["dynamic_terminology"]["errors"])
    assert target_part.read_bytes() == raw_bytes
    assert not (output / "translated" / "validated" / "part1.md").exists()
    assert not (output / "scratch" / "translate" / "invalid_outputs" / "part1.md").exists()


def test_html_single_file_gate_catches_target_term_hidden_in_attribute_comment_and_code(tmp_path):
    output, _source, _target, config = _build_real_publish_fixture(tmp_path)
    source = output / "compressed_units"
    target = output / "translated_compressed"
    source.mkdir()
    target.mkdir()
    source_text = '<p title="source metadata">social form</p><!-- source note --><code>hidden source</code>'
    target_text = '<p title="社会形式">社会形</p><!-- 社会形式 --><code>社会形式</code>'
    (source / "body.md").write_text(source_text, encoding="utf-8")
    (target / "body.md").write_text(target_text, encoding="utf-8")
    (source / "body.mapping.json").write_text(
        '{"translation_parts":["body.md"],"original_extension":".xhtml"}',
        encoding="utf-8",
    )
    (output / "input.epub").write_bytes(b"synthetic source EPUB")
    chapter_groups = {"chapter-1": ["body.md"]}
    _digest, entries = load_current_snapshot(
        output / "dynamic_terminology", book_id="book",
        source_language="English", target_language="Chinese",
    )
    contexts = build_dynamic_unit_contexts(
        output, source, {}, entries, source_language="English",
        target_language="Chinese", chapter_groups=chapter_groups,
    )
    paths = prepare_markdown_subagent(
        output, "translate-html", source, target, "English", "Chinese",
        config={}, unit_context_files=contexts, chapter_groups=chapter_groups,
    )
    import hashlib
    import json

    manifest = json.loads(paths["manifest"].read_text(encoding="utf-8"))
    manifest["book_title"] = "book"
    manifest["input_epub"] = "input.epub"
    manifest["input_epub_sha256"] = hashlib.sha256((output / "input.epub").read_bytes()).hexdigest()
    manifest["dynamic_terminology"] = _dynamic_manifest_fields(target)
    atomic_write_json(paths["manifest"], manifest)
    # Old aggregation files may remain in compressed_units after EPUB parts
    # are split. The mapping declares active files and is the HTML inventory.
    (source / "legacy-logical.md").write_text("obsolete aggregate", encoding="utf-8")
    (target / "legacy-logical.md").write_text("旧聚合内容", encoding="utf-8")
    validator = HTMLTranslationValidator(HTMLValidationWorkspace(
        output, source, target,
        {"title": "book", "translation": {
            "source_language": "English", "target_language": "Chinese",
            "dynamic_terminology": {"enabled": True},
        }},
    ))
    report = validator.validate(file_name="body.md")
    assert report["metadata"]["skipped"] is True
    assert report["translation_context"]["skipped"] is True
    assert report["dynamic_terminology"]["valid"] is False
    full_report = validator.validate()
    assert full_report["dynamic_terminology"]["valid"] is False
    assert "legacy-logical.md" not in full_report["dynamic_terminology"]["files"]
    assert report["all_passed"] is False
    assert "body.md" not in report["valid_files"]
    assert any("required memory target term is missing" in item["reason"] for item in report["invalid"])
    assert (target / "body.md").read_text(encoding="utf-8") == target_text


def test_retranslate_required_repair_consistency_only_review_and_publish_closes_gate(tmp_path):
    output, source, target, config = _build_real_publish_fixture(
        tmp_path, retranslate_file="part1.md",
    )
    repaired = "前一章中，社会形式再次出现。"
    (target / "part1.md").write_text(repaired, encoding="utf-8")
    _digest, entries = load_current_snapshot(
        output / "dynamic_terminology", book_id="book",
        source_language="English", target_language="Chinese",
    )
    chapters = {"chapter-1": ["part1.md", "part2.md", "part3.md"]}
    contexts = build_dynamic_unit_contexts(
        output, source, {}, entries, source_language="English", target_language="Chinese",
        chapter_groups=chapters,
    )
    contexts = {name: output / path for name, path in contexts.items()}
    paths = prepare_markdown_subagent(
        output, "translate", source, target, "English", "Chinese", config={}, resume=True,
        unit_context_files=contexts, chapter_groups=chapters,
    )
    handoffs = write_worker_handoffs(output, paths["manifest"], paths["prompt"])
    attach_candidate_sidecars(
        output, handoffs, task="translate",
        chapter_by_file={name: "chapter-1" for name in chapters["chapter-1"]},
        source_language="English", target_language="Chinese",
    )
    manifest = json.loads(paths["manifest"].read_text(encoding="utf-8"))
    manifest["book_title"] = "book"
    manifest["dynamic_terminology"] = _dynamic_manifest_fields(target)
    atomic_write_json(paths["manifest"], manifest)

    from pdf2epub.commands.terminology_memory import _read_only_consistency_audits

    audits = _read_only_consistency_audits(
        output, "translate", manifest, ["part1.md"], config,
    )
    packet, authority = prepare_review_packet(
        output, "translate", book_id="book", source_language="English",
        target_language="Chinese", chapter_groups=chapters, book_entities_path=None,
        consistency_units=["part1.md"], consistency_audits=audits,
    )
    assert packet["candidates"] == []
    review = {
        "prepared_packet_sha256": packet["prepared_packet_sha256"],
        "candidates": [],
        "affected_targets": [{
            **{key: row[key] for key in (
                "unit_id", "source_sha256", "target_sha256", "scope", "effective_projection_sha256",
            )},
            "decision": "consistent",
        } for row in packet["affected_targets"]],
    }
    review_path = output / "consistency-review.json"
    atomic_write_json(review_path, review)
    attestation = session_attestation(output, "translate", confirmed=True)
    attestation.update({"book_id": "book", "authority_sha256": authority.authority_sha256})
    current = load_current_snapshot(
        output / "dynamic_terminology", book_id="book",
        source_language="English", target_language="Chinese",
    )[0]
    publish_review_packet(
        output, "translate", review_path, authority=authority,
        current_snapshot_sha256=current, operator_attestation=attestation,
    )
    state = affected_target_consistency_state(
        output, book_id="book", source_language="English", target_language="Chinese",
    )
    assert state["part1.md"][0]["decision"] == "consistent"
    assert any(row["decision"] == "retranslate_required" for row in state["part1.md"])

    report = validate_dynamic_terminology_units(
        output, task="translate", source_dir=source, target_dir=target, config=config,
    )
    assert report["valid"] is True


def test_source_hash_refresh_does_not_reuse_old_memory_consistency_review(tmp_path):
    output, source, target, _config = _build_real_publish_fixture(tmp_path)
    (source / "part1.md").write_text(
        "social form appears in the earlier chapter.", encoding="utf-8",
    )
    _digest, entries = load_current_snapshot(
        output / "dynamic_terminology", book_id="book",
        source_language="English", target_language="Chinese",
    )
    chapters = {"chapter-1": ["part1.md", "part2.md"]}
    contexts = build_dynamic_unit_contexts(
        output, source, {}, entries, source_language="English", target_language="Chinese",
        chapter_groups=chapters,
    )
    contexts = {name: output / path for name, path in contexts.items()}
    paths = prepare_markdown_subagent(
        output, "translate", source, target, "English", "Chinese", config={}, resume=True,
        unit_context_files=contexts, chapter_groups=chapters,
    )
    manifest = json.loads(paths["manifest"].read_text(encoding="utf-8"))
    manifest["book_title"] = "book"
    manifest["dynamic_terminology"] = _dynamic_manifest_fields(target)
    atomic_write_json(paths["manifest"], manifest)

    report = validate_dynamic_terminology_units(
        output, task="translate", source_dir=source, target_dir=target,
        config={"title": "book", "translation": {
            "source_language": "English", "target_language": "Chinese",
            "dynamic_terminology": {"enabled": True},
        }},
    )
    assert report["valid"] is False
    assert any(
        "lacks consistency review" in error["error"]
        for error in report["errors"]
    )
    assert (target / "part1.md").read_text(encoding="utf-8") == "社会形式在前一章出现"
