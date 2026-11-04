import json
import hashlib
from pathlib import Path

import pytest
from pdf2epub import terminology_memory_validation as validation
from pdf2epub.dynamic_terminology import (
    affected_target_consistency_state,
    attach_candidate_sidecars,
    build_dynamic_unit_contexts,
    current_effective_projection,
    load_current_snapshot,
    prepare_review_packet,
    publish_review_packet,
    publication_target_inventory,
    publication_target_inventory_complete,
    session_attestation,
)
from pdf2epub.markdown_handoff import prepare_markdown_subagent
from pdf2epub.subagent_runtime import write_worker_handoffs
from pdf2epub.workflow_contracts import atomic_write_json


def _entry(source="term", target="译名", **extra):
    return {"kind": "memory", "source": source, "target": target, **extra}


def _inventory_sha(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def _dynamic_manifest_fields(target_dir, base_contexts=None):
    inventory = {path.name: validation._sha_file(path) for path in sorted(Path(target_dir).glob("*.md"))}
    return {
        "schema_version": 1, "enabled": True,
        "base_unit_context_files": base_contexts or {},
        "preexisting_target_inventory": inventory,
        "preexisting_target_inventory_sha256": _inventory_sha(inventory),
    }


def _workspace(tmp_path, monkeypatch, *, task="translate", source="A term.", target="译名。", entries=None, lineage=None):
    output = tmp_path / "book"
    source_dir, target_dir = output / "source", output / "translated"
    source_dir.mkdir(parents=True)
    target_dir.mkdir()
    (source_dir / "unit.md").write_text(source, encoding="utf-8")
    (target_dir / "unit.md").write_text(target, encoding="utf-8")
    source_hash = validation._sha_file(source_dir / "unit.md")
    target_hash = validation._sha_file(target_dir / "unit.md")
    unit_path = output / "translation_glossaries" / "dynamic_terminology" / "unit_contexts" / "unit.json"
    unit_path.parent.mkdir(parents=True)
    projection_entries = entries or []
    unit_path.write_text(json.dumps({
        "schema_version": 1, "source_file": "unit.md",
        "language_pair": {"source": "English", "target": "Chinese"},
        "entries": projection_entries,
    }), encoding="utf-8")
    unit_hash = validation._sha_file(unit_path)
    manifest = {
        "schema_version": 1,
        "task": task,
        "book_title": "book",
        "source_language": "English",
        "target_language": "Chinese",
        "source_dir": "source",
        "target_dir": "translated",
        "source_sha256": {"unit.md": source_hash},
        "files": ["unit.md"],
        "unit_context_files": {"unit.md": unit_path.relative_to(output).as_posix()},
        "unit_context_sha256": {"unit.md": unit_hash},
        "chapter_groups": {"ch1": ["unit.md"]},
        "dynamic_terminology": _dynamic_manifest_fields(target_dir),
    }
    (output / f"{task}_subagent_manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    projection = {"entries": projection_entries, "memory_entries": [e for e in projection_entries if e.get("kind") == "memory"], "projection_sha256": "a" * 64}
    monkeypatch.setattr(validation, "current_effective_projection", lambda *a, **k: projection)
    monkeypatch.setattr(validation, "affected_target_consistency_state", lambda *a, **k: lineage or {})
    monkeypatch.setattr(validation, "publication_target_inventory", lambda *a, **k: {})
    monkeypatch.setattr(validation, "publication_target_inventory_complete", lambda *a, **k: True)
    config = {"title": "book", "translation": {"dynamic_terminology": {"enabled": True}, "source_language": "English", "target_language": "Chinese"}}
    return output, source_dir, target_dir, config, source_hash, target_hash


def _validate(args, *, task="translate", selected_files=None):
    output, source_dir, target_dir, config, *_ = args
    return validation.validate_dynamic_terminology_units(
        output, task=task, source_dir=source_dir, target_dir=target_dir,
        config=config, selected_files=selected_files,
    )


def _set_prepared_entries(args, entries):
    output = args[0]
    manifest_path = output / "translate_subagent_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    context_path = output / manifest["unit_context_files"]["unit.md"]
    context = json.loads(context_path.read_text(encoding="utf-8"))
    context["entries"] = entries
    context_path.write_text(json.dumps(context), encoding="utf-8")
    manifest["unit_context_sha256"]["unit.md"] = validation._sha_file(context_path)
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")


def test_dynamic_disabled_preserves_legacy_flow(tmp_path):
    result = validation.validate_dynamic_terminology_units(
        tmp_path, task="translate", source_dir=tmp_path, target_dir=tmp_path,
        config={"translation": {"dynamic_terminology": {"enabled": False}}},
    )
    assert result == {"enabled": False, "valid": True, "files": {}, "errors": []}


def test_malformed_dynamic_config_fails_closed(tmp_path):
    with pytest.raises(ValueError, match="dynamic_terminology"):
        validation.validate_dynamic_terminology_units(
            tmp_path, task="translate", source_dir=tmp_path, target_dir=tmp_path,
            config={"translation": {"dynamic_terminology": "yes"}},
        )


def test_current_context_and_localized_selected_scope_pass(tmp_path, monkeypatch):
    args = _workspace(tmp_path, monkeypatch, entries=[_entry()])
    # CLI-prepared manifest language pair is authoritative even when config
    # stores a different fallback pair.
    args[3]["translation"]["source_language"] = "French"
    result = _validate(args, selected_files=["unit.md"])
    assert result["valid"] is True
    assert result["files"]["unit.md"]["projection_sha256"] == "a" * 64


def test_missing_memory_target_term_blocks(tmp_path, monkeypatch):
    args = _workspace(tmp_path, monkeypatch, target="The term is omitted.", entries=[_entry()])
    result = _validate(args)
    assert result["valid"] is False
    assert "required memory target term is missing" in result["files"]["unit.md"]["errors"][0]


def test_html_attribute_comment_and_code_are_not_target_evidence(tmp_path, monkeypatch):
    args = _workspace(
        tmp_path, monkeypatch, task="translate-html",
        target='<span title="译名">English</span><!-- 译名 --><code>译名</code>',
        entries=[_entry()],
    )
    result = _validate(args, task="translate-html")
    assert result["valid"] is False


@pytest.mark.parametrize(
    ("target", "expected"),
    [
        ("<code>译名</code>", False),
        ("<script>译名</script>", False),
        ("译**名**", True),
        ("<em>译</em>名", True),
        ("<!-- 译名 -->正文", False),
        ("[正文](https://example.test/译名)", False),
        ('<span title="译名">正文</span>', False),
        ("```\n译名\n```", False),
    ],
)
def test_markdown_target_visible_text_matches_renderer_semantics(tmp_path, monkeypatch, target, expected):
    assert validation._target_has_term(target, _entry(), "translate") is expected


@pytest.mark.parametrize(
    ("target", "expected"),
    [("<strong>译</strong>名", True), ("<em>译</em>名", True), ("<code>译名</code>", False), ("<script>译名</script>", False)],
)
def test_html_visible_target_preserves_inline_flow_but_ignores_hidden_regions(tmp_path, target, expected):
    assert validation._target_has_term(target, _entry(), "translate-html") is expected


def test_markdown_comment_inline_code_and_html_attribute_are_not_source_hits(tmp_path, monkeypatch):
    args = _workspace(
        tmp_path, monkeypatch,
        source='<!-- term --> `term` <span title="term">visible text</span>',
        target="No glossary translation required.", entries=[_entry()],
    )
    _set_prepared_entries(args, [])
    monkeypatch.setattr(validation, "current_effective_projection", lambda *a, **k: {
        "entries": [], "memory_entries": [], "projection_sha256": "e" * 64,
    })
    report = _validate(args)
    assert report["valid"] is True


def test_missing_dynamic_manifest_is_rejected(tmp_path, monkeypatch):
    args = _workspace(tmp_path, monkeypatch)
    manifest_path = args[0] / "translate_subagent_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest.pop("dynamic_terminology")
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ValueError, match="missing from the authoritative task manifest"):
        _validate(args)


def test_exact_current_consistent_review_is_reusable(tmp_path, monkeypatch):
    entry = _entry()
    output, src, target, config, source_hash, target_hash = _workspace(tmp_path, monkeypatch, entries=[entry])
    row = {
        "source_sha256": source_hash, "target_sha256": target_hash,
        "scope": {"kind": "chapter", "id": "ch1"}, "decision": "consistent",
        "language_pair": {"source": "English", "target": "Chinese"},
        "approved_projection_sha256": "a" * 64,
    }
    monkeypatch.setattr(validation, "affected_target_consistency_state", lambda *a, **k: {"unit.md": [row]})
    inventory_row = {
        **row, "review_required_change": True,
        "projection_before_sha256": "0" * 64,
        "projection_after_sha256": "a" * 64,
    }
    monkeypatch.setattr(validation, "publication_target_inventory", lambda *a, **k: {"unit.md": [inventory_row]})
    monkeypatch.setattr(validation, "publication_target_inventory_complete", lambda *a, **k: True)
    report = validation.validate_dynamic_terminology_units(
        output, task="translate", source_dir=src, target_dir=target, config=config,
    )
    assert report["valid"] is True


def test_publication_inventory_requires_review_even_without_resume_manifest_state(tmp_path, monkeypatch):
    args = _workspace(tmp_path, monkeypatch, entries=[_entry()])
    monkeypatch.setattr(validation, "publication_target_inventory", lambda *a, **k: {
        "unit.md": [{
            "source_sha256": args[4], "target_sha256": args[5],
            "scope": {"kind": "chapter", "id": "ch1"},
            "review_required_change": True,
            "projection_before_sha256": "0" * 64,
            "projection_after_sha256": "a" * 64,
        }],
    })
    report = _validate(args)
    assert report["valid"] is False
    assert any("lacks consistency review" in item["error"] for item in report["errors"])


def test_static_fixed_or_preferred_entry_suppresses_memory_obligation(tmp_path, monkeypatch):
    # The effective projection is the precedence-resolved result. A static
    # preferred/fixed term with this source wins, so no memory entry is gated.
    args = _workspace(tmp_path, monkeypatch, target="Static translation.")
    static = {"kind": "domain", "source": "term", "target": "Static translation", "status": "fixed"}
    _set_prepared_entries(args, [static])
    monkeypatch.setattr(validation, "current_effective_projection", lambda *a, **k: {
        "entries": [static], "memory_entries": [], "projection_sha256": "b" * 64,
    })
    assert _validate(args)["valid"] is True


def test_rejected_candidate_absent_from_approved_projection_is_not_required(tmp_path, monkeypatch):
    # Only approved snapshot entries are projected; a rejected candidate never
    # creates a target obligation.
    args = _workspace(tmp_path, monkeypatch, target="Translation omitted.")
    _set_prepared_entries(args, [])
    monkeypatch.setattr(validation, "current_effective_projection", lambda *a, **k: {
        "entries": [], "memory_entries": [], "projection_sha256": "c" * 64,
    })
    assert _validate(args)["valid"] is True


def test_chapter_scoped_projection_does_not_leak_to_other_unit(tmp_path, monkeypatch):
    args = _workspace(tmp_path, monkeypatch, target="Translation omitted.")
    _set_prepared_entries(args, [])
    monkeypatch.setattr(validation, "current_effective_projection", lambda *a, **k: {
        "entries": [], "memory_entries": [], "projection_sha256": "d" * 64,
    })
    assert _validate(args, selected_files=["unit.md"])["valid"] is True


def test_retranslate_required_review_does_not_accept_target_hash_refresh(tmp_path, monkeypatch):
    output, src, target, config, source_hash, _ = _workspace(
        tmp_path, monkeypatch, entries=[_entry()], target="punctuation changed; 译名。",
    )
    old_hash = "f" * 64
    row = {
        "source_sha256": source_hash, "target_sha256": old_hash,
        "scope": {"kind": "chapter", "id": "ch1"}, "decision": "retranslate_required",
        "language_pair": {"source": "English", "target": "Chinese"},
        "approved_projection_sha256": "a" * 64,
    }
    monkeypatch.setattr(validation, "affected_target_consistency_state", lambda *a, **k: {"unit.md": [row]})
    inventory_row = {
        **row, "review_required_change": True,
        "projection_before_sha256": "0" * 64,
        "projection_after_sha256": "a" * 64,
    }
    monkeypatch.setattr(validation, "publication_target_inventory", lambda *a, **k: {"unit.md": [inventory_row]})
    monkeypatch.setattr(validation, "publication_target_inventory_complete", lambda *a, **k: True)
    report = validation.validate_dynamic_terminology_units(
        output, task="translate", source_dir=src, target_dir=target, config=config,
    )
    assert report["valid"] is False
    assert any("unresolved retranslate_required" in e["error"] for e in report["errors"])


def test_malformed_prepared_context_fails_closed(tmp_path, monkeypatch):
    args = _workspace(tmp_path, monkeypatch)
    unit_path = args[0] / "translation_glossaries" / "dynamic_terminology" / "unit_contexts" / "unit.json"
    unit_path.write_text('{"schema_version":999,"entries":[]}', encoding="utf-8")
    report = _validate(args)
    assert report["valid"] is False
    assert any("unsupported schema" in item["error"] for item in report["errors"])


def test_path_escape_is_rejected(tmp_path, monkeypatch):
    args = _workspace(tmp_path, monkeypatch)
    args[1].mkdir(exist_ok=True)
    (args[1] / "unit.md").unlink()
    manifest_path = args[0] / "translate_subagent_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["source_sha256"] = {"../outside.md": "0" * 64}
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ValueError, match="declared files and source hashes disagree"):
        _validate(args, selected_files=["../outside.md"])


def test_source_added_after_prepare_blocks_full_and_selected_validation(tmp_path, monkeypatch):
    args = _workspace(tmp_path, monkeypatch)
    (args[1] / "late.md").write_text("new source after prepare", encoding="utf-8")
    full = _validate(args)
    assert full["valid"] is False
    assert any(item["file"] == "<source-inventory>" for item in full["errors"])
    with pytest.raises(ValueError, match="selected files must be present"):
        _validate(args, selected_files=["late.md"])


def test_real_projection_honors_static_priority_and_chapter_scope(tmp_path):
    output = tmp_path
    source = output / "source"
    source.mkdir()
    (source / "a.md").write_text("shared term occurs", encoding="utf-8")
    (source / "b.md").write_text("shared term occurs", encoding="utf-8")
    base = output / "translation_glossaries" / "unit_contexts" / "a.json"
    base.parent.mkdir(parents=True)
    fixed = {"kind": "domain", "policy": "fixed", "source": "shared term", "target": "fixed target"}
    base.write_text(json.dumps({"schema_version": 1, "entries": [fixed]}), encoding="utf-8")
    memory_root = output / "dynamic_terminology"
    snapshots = memory_root / "snapshots"
    snapshots.mkdir(parents=True)
    entry = {
        "candidate_id": "approved", "source": "shared term", "target": "memory target",
        "scope": {"kind": "chapter", "id": "chapter-a"},
        "language_pair": {"source": "English", "target": "Chinese"},
    }
    snapshot = {
        "schema_version": 1, "book_id": "book",
        "language_pair": {"source": "English", "target": "Chinese"},
        "parent_snapshot_sha256": None, "entries": [entry], "review": {},
    }
    payload = json.dumps(snapshot, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    digest = hashlib.sha256(payload).hexdigest()
    (snapshots / f"{digest}.json").write_bytes(payload)
    atomic_write_json(memory_root / "current.json", {"snapshot_sha256": digest, "path": f"snapshots/{digest}.json"})
    fixed_projection = current_effective_projection(
        output, "translate", source, "a.md", book_id="book",
        source_language="English", target_language="Chinese",
        chapter_groups={"chapter-a": ["a.md"], "chapter-b": ["b.md"]},
        base_context_path=base,
    )
    other_chapter = current_effective_projection(
        output, "translate", source, "b.md", book_id="book",
        source_language="English", target_language="Chinese",
        chapter_groups={"chapter-a": ["a.md"], "chapter-b": ["b.md"]},
    )
    assert fixed_projection["entries"] == [fixed]
    assert fixed_projection["memory_entries"] == []
    assert other_chapter["entries"] == []


def test_new_unit_survives_unrelated_ancestor_publish_without_review(tmp_path):
    output = tmp_path
    source, target = output / "source", output / "translated"
    source.mkdir()
    target.mkdir()
    (source / "new.md").write_text("ordinary text in its chapter", encoding="utf-8")
    (target / "new.md").write_text("该章中的普通文本", encoding="utf-8")
    pair = {"source": "English", "target": "Chinese"}
    empty_projection = _inventory_sha([])
    first = {
        "schema_version": 1, "book_id": "book", "language_pair": pair,
        "parent_snapshot_sha256": None,
        "entries": [{"candidate_id": "first", "source": "old phrase", "target": "旧短语", "scope": {"kind": "chapter", "id": "chapter-1"}, "language_pair": pair}],
        "review": {"affected_targets": []}, "approved_projection_sha256": {},
        "preexisting_target_inventory": {}, "preexisting_target_inventory_complete": True,
    }
    memory_root = output / "dynamic_terminology"
    snapshots = memory_root / "snapshots"
    snapshots.mkdir(parents=True)
    def store_snapshot(value):
        payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        digest = hashlib.sha256(payload).hexdigest()
        (snapshots / f"{digest}.json").write_bytes(payload)
        return digest
    first_digest = store_snapshot(first)
    new_source_hash = validation._sha_file(source / "new.md")
    new_target_hash = validation._sha_file(target / "new.md")
    unrelated = {
        "schema_version": 1, "book_id": "book", "language_pair": pair,
        "parent_snapshot_sha256": first_digest,
        "entries": [
            *first["entries"],
            {"candidate_id": "second", "source": "distant phrase", "target": "远处短语", "scope": {"kind": "chapter", "id": "chapter-3"}, "language_pair": pair},
        ],
        "review": {"affected_targets": []}, "approved_projection_sha256": {"new.md": empty_projection},
        "preexisting_target_inventory": {
            "new.md": {
                "source_file": "new.md", "target_file": "new.md",
                "source_sha256": new_source_hash, "target_sha256": new_target_hash,
                "chapter_id": "chapter-2", "scope": {"kind": "chapter", "id": "chapter-2"},
                "effective_projection_sha256": empty_projection,
                "projection_before_sha256": empty_projection,
                "projection_after_sha256": empty_projection,
                "review_required_change": False,
            },
        },
        "preexisting_target_inventory_complete": True,
    }
    second_digest = store_snapshot(unrelated)
    atomic_write_json(memory_root / "current.json", {
        "snapshot_sha256": second_digest, "path": f"snapshots/{second_digest}.json",
    })
    chapters = {"chapter-2": ["new.md"]}
    contexts = build_dynamic_unit_contexts(
        output, source, {}, unrelated["entries"], source_language="English",
        target_language="Chinese", chapter_groups=chapters,
    )
    paths = prepare_markdown_subagent(
        output, "translate", source, target, "English", "Chinese", config={},
        unit_context_files=contexts, chapter_groups=chapters,
    )
    manifest = json.loads(paths["manifest"].read_text(encoding="utf-8"))
    manifest["book_title"] = "book"
    manifest["dynamic_terminology"] = _dynamic_manifest_fields(target)
    atomic_write_json(paths["manifest"], manifest)
    report = validation.validate_dynamic_terminology_units(
        output, task="translate", source_dir=source, target_dir=target,
        config={"title": "book", "translation": {
            "source_language": "English", "target_language": "Chinese",
            "dynamic_terminology": {"enabled": True},
        }},
    )
    assert report["valid"] is True
    assert report["files"]["new.md"]["projection_sha256"] == empty_projection


def _build_real_publish_fixture(tmp_path, *, retranslate_file=None):
    output = tmp_path
    source, target = output / "polished_markdown", output / "translated"
    source.mkdir()
    target.mkdir()
    (source / "part1.md").write_text("social form appears in the earlier chapter", encoding="utf-8")
    (target / "part1.md").write_text("社会形式在前一章出现", encoding="utf-8")
    (source / "part2.md").write_text("social form appears in the later chapter", encoding="utf-8")
    (target / "part2.md").write_text("社会形式在后一章出现", encoding="utf-8")
    chapters = {"chapter-1": ["part1.md", "part2.md"]}
    empty_contexts = build_dynamic_unit_contexts(
        output, source, {}, [], source_language="English", target_language="Chinese",
        chapter_groups=chapters,
    )
    empty_contexts = {name: output / path for name, path in empty_contexts.items()}
    paths = prepare_markdown_subagent(
        output, "translate", source, target, "English", "Chinese", config={},
        unit_context_files=empty_contexts, chapter_groups=chapters,
    )
    handoffs = write_worker_handoffs(output, paths["manifest"], paths["prompt"])
    attach_candidate_sidecars(
        output, handoffs, task="translate", chapter_by_file={"part1.md": "chapter-1", "part2.md": "chapter-1"},
        source_language="English", target_language="Chinese",
    )
    manifest_path = paths["manifest"]
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["book_title"] = "book"
    manifest["dynamic_terminology"] = _dynamic_manifest_fields(target)
    atomic_write_json(manifest_path, manifest)
    from pdf2epub.markdown_subagent_validation import validate_markdown_subagent

    dynamic_config = {"title": "book", "translation": {
        "source_language": "English", "target_language": "Chinese",
        "dynamic_terminology": {"enabled": True},
    }}
    report = validate_markdown_subagent(
        output, "translate", source, target,
        create_validated_copy=True, target_language="Chinese",
        dynamic_terminology_config=dynamic_config,
    )
    assert report["all_passed"] is True
    assert set(report["valid_files"]) == {"part1.md", "part2.md"}
    source_hashes = {name: validation._sha_file(source / name) for name in ("part1.md", "part2.md")}
    target_hashes = {name: validation._sha_file(target / name) for name in ("part1.md", "part2.md")}
    handoff = next(item for item in handoffs if "part2.md" in item["assigned_files"])
    scoped = json.loads((output / handoff["manifest"]).read_text(encoding="utf-8"))
    sidecar = output / scoped["dynamic_terminology"]["candidate_sidecar"]
    sidecar.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(sidecar, {
        "schema_version": 1, "assignment_sha256": scoped["assignment_sha256"],
        "candidates": [{
            "candidate_id": "social-form", "source": "social form", "target": "社会形式",
            "source_file": "part2.md", "target_file": "part2.md",
            "source_sha256": source_hashes["part2.md"], "target_sha256": target_hashes["part2.md"],
            "source_evidence": "social form appears in the later chapter", "target_evidence": "社会形式在后一章出现",
            "assignment_sha256": scoped["assignment_sha256"],
            "language_pair": {"source": "English", "target": "Chinese"},
            "scope": {"kind": "chapter", "id": "chapter-1"},
        }],
    })
    packet, authority = prepare_review_packet(
        output, "translate", book_id="book", source_language="English",
        target_language="Chinese", chapter_groups=chapters, book_entities_path=None,
    )
    reviewed = {
        "prepared_packet_sha256": packet["prepared_packet_sha256"],
        "candidates": [{"candidate_id": "social-form", "decision": "approve"}],
        "affected_targets": [{
            **{key: row[key] for key in (
                "unit_id", "source_sha256", "target_sha256", "scope", "effective_projection_sha256",
            )},
            "decision": "retranslate_required" if row["unit_id"] == retranslate_file else "consistent",
        } for row in packet["affected_targets"]],
    }
    review_path = output / "review.json"
    atomic_write_json(review_path, reviewed)
    attestation = session_attestation(output, "translate", confirmed=True)
    attestation.update({"book_id": "book", "authority_sha256": authority.authority_sha256})
    parent = load_current_snapshot(
        output / "dynamic_terminology", book_id="book", source_language="English", target_language="Chinese",
    )[0]
    publish_review_packet(
        output, "translate", review_path, authority=authority,
        current_snapshot_sha256=parent, operator_attestation=attestation,
    )
    digest, approved = load_current_snapshot(
        output / "dynamic_terminology", book_id="book", source_language="English", target_language="Chinese",
    )
    assert digest and len(approved) == 1
    assert publication_target_inventory_complete(
        output, book_id="book", source_language="English", target_language="Chinese",
    )
    assert "part1.md" in publication_target_inventory(
        output, book_id="book", source_language="English", target_language="Chinese",
    )
    assert affected_target_consistency_state(
        output, book_id="book", source_language="English", target_language="Chinese",
    )["part1.md"][0]["decision"] == ("retranslate_required" if retranslate_file == "part1.md" else "consistent")

    config = {"title": "book", "translation": {
        "source_language": "English", "target_language": "Chinese",
        "dynamic_terminology": {"enabled": True},
    }}
    # Publication changes the effective context. Replaying the old handoff or
    # changing only target punctuation cannot clear the publication-time gate.
    stale = validation.validate_dynamic_terminology_units(
        output, task="translate", source_dir=source, target_dir=target, config=config,
    )
    assert stale["valid"] is False
    assert any("run translate --resume" in item["error"] for item in stale["errors"])
    (target / "part1.md").write_text("社会形式，在前一章出现", encoding="utf-8")
    refreshed_hash_only = validation.validate_dynamic_terminology_units(
        output, task="translate", source_dir=source, target_dir=target, config=config,
    )
    assert refreshed_hash_only["valid"] is False
    if retranslate_file == "part1.md":
        assert any("unresolved retranslate_required" in item["error"] for item in refreshed_hash_only["errors"])
    else:
        assert any("lacks consistency review" in item["error"] for item in refreshed_hash_only["errors"])
    (target / "part1.md").write_text("社会形式在前一章出现", encoding="utf-8")

    current_contexts = build_dynamic_unit_contexts(
        output, source, {}, approved, source_language="English", target_language="Chinese",
        chapter_groups=chapters,
    )
    current_paths = prepare_markdown_subagent(
        output, "translate", source, target, "English", "Chinese", config={}, resume=True,
        unit_context_files=current_contexts, chapter_groups=chapters,
    )
    current_manifest = json.loads(current_paths["manifest"].read_text(encoding="utf-8"))
    current_manifest["book_title"] = "book"
    current_manifest["dynamic_terminology"] = _dynamic_manifest_fields(target)
    atomic_write_json(current_paths["manifest"], current_manifest)
    report = validation.validate_dynamic_terminology_units(
        output, task="translate", source_dir=source, target_dir=target, config=config,
    )
    if retranslate_file:
        assert report["valid"] is False
        assert any("unresolved retranslate_required" in item["error"] for item in report["errors"])
    else:
        assert report["valid"] is True
    assert report["files"]["part1.md"]["projection_sha256"] == affected_target_consistency_state(
        output, book_id="book", source_language="English", target_language="Chinese",
    )["part1.md"][0]["approved_projection_sha256"]

    # A genuinely new target after the complete immutable publication inventory
    # is not treated as an old translation, but still must satisfy its term gate.
    (source / "part3.md").write_text("social form appears in a new unit", encoding="utf-8")
    (target / "part3.md").write_text("社会形式出现在新单元", encoding="utf-8")
    expanded_chapters = {"chapter-1": ["part1.md", "part2.md", "part3.md"]}
    new_contexts = build_dynamic_unit_contexts(
        output, source, {}, approved, source_language="English", target_language="Chinese",
        chapter_groups=expanded_chapters,
    )
    new_paths = prepare_markdown_subagent(
        output, "translate", source, target, "English", "Chinese", config={}, resume=True,
        unit_context_files=new_contexts, chapter_groups=expanded_chapters,
    )
    new_manifest = json.loads(new_paths["manifest"].read_text(encoding="utf-8"))
    new_manifest["book_title"] = "book"
    new_manifest["dynamic_terminology"] = _dynamic_manifest_fields(target)
    atomic_write_json(new_paths["manifest"], new_manifest)
    new_result = validation.validate_dynamic_terminology_units(
        output, task="translate", source_dir=source, target_dir=target,
        config=config, selected_files=["part3.md"],
    )
    assert new_result["valid"] is True
    return output, source, target, config


def test_real_publish_context_and_consistency_lineage_gate(tmp_path):
    _build_real_publish_fixture(tmp_path)
