import hashlib
import json
from pathlib import Path

import pytest

from pdf2epub.dynamic_terminology import (
    affected_target_consistency_state,
    attach_candidate_sidecars,
    build_dynamic_unit_contexts,
    current_effective_projection,
    load_current_snapshot,
    prepare_review_packet,
    publish_review_packet,
    session_attestation,
    verify_session_attestation,
)
from pdf2epub.markdown_handoff import prepare_markdown_subagent
from pdf2epub.subagent_dispatch import dispatch_lease_path
from pdf2epub.subagent_runtime import write_worker_handoffs
from pdf2epub.workflow_contracts import atomic_write_json


def sha(data):
    return hashlib.sha256(data).hexdigest()


def test_context_projection_is_scope_aware_stable_and_deduped(tmp_path):
    source = tmp_path / "polished"
    source.mkdir()
    (source / "a.md").write_text("social form appears", encoding="utf-8")
    (source / "b.md").write_text("social form appears", encoding="utf-8")
    chapters = {"chapter-1": ["a.md"], "chapter-2": ["b.md"]}
    empty = build_dynamic_unit_contexts(
        tmp_path, source, {}, [], source_language="English", target_language="Chinese", chapter_groups=chapters,
    )
    empty_hashes = {name: sha(path.read_bytes()) for name, path in empty.items()}
    memory = [{
        "candidate_id": "proposal-1", "source": "social form", "target": "社会形式",
        "scope": {"kind": "chapter", "id": "chapter-1"},
        "language_pair": {"source": "English", "target": "Chinese"},
    }]
    current = build_dynamic_unit_contexts(
        tmp_path, source, {}, memory, source_language="English", target_language="Chinese", chapter_groups=chapters,
    )
    assert json.loads(current["a.md"].read_text(encoding="utf-8"))["entries"][0]["target"] == "社会形式"
    assert json.loads(current["b.md"].read_text(encoding="utf-8"))["entries"] == []
    assert sha(current["b.md"].read_bytes()) == empty_hashes["b.md"]
    # Proposal IDs and duplicate proposals do not enter the unit hash.
    memory.append({**memory[0], "candidate_id": "different-proposal-id"})
    repeated = build_dynamic_unit_contexts(
        tmp_path, source, {}, memory, source_language="English", target_language="Chinese", chapter_groups=chapters,
    )
    assert repeated["a.md"] == current["a.md"]


def test_memory_equal_to_book_entity_does_not_change_context_hash():
    from pdf2epub.dynamic_terminology import _project_effective_entries

    entity = {"kind": "book_entity", "source": "social form", "target": "社会形式"}
    memory = [{
        "candidate_id": "proposal", "source": "social form", "target": "社会形式",
        "scope": {"kind": "chapter", "id": "chapter-1"},
        "language_pair": {"source": "English", "target": "Chinese"},
    }]
    baseline = _project_effective_entries(
        "social form", [entity], [], source_language="English", target_language="Chinese",
        chapter_id="chapter-1",
    )
    effective = _project_effective_entries(
        "social form", [entity], memory, source_language="English", target_language="Chinese",
        chapter_id="chapter-1",
    )
    assert effective["entries"] == baseline["entries"]
    assert effective["projection_sha256"] == baseline["projection_sha256"]


def test_same_priority_memory_and_book_entity_disagreement_blocks_projection():
    from pdf2epub.dynamic_terminology import _project_effective_entries

    memory = [{
        "candidate_id": "proposal", "source": "social form", "target": "社会形式",
        "scope": {"kind": "book", "id": "demo"},
        "language_pair": {"source": "English", "target": "Chinese"},
    }]
    with pytest.raises(ValueError, match="same-priority terminology entries disagree"):
        _project_effective_entries(
            "social form", [{"kind": "book_entity", "source": "social form", "target": "社会形态"}],
            memory, source_language="English", target_language="Chinese", chapter_id="chapter-1",
        )


def test_current_projection_api_uses_explicit_task_and_source_dir(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "a.md").write_text("social form", encoding="utf-8")
    memory_root = tmp_path / "dynamic_terminology"
    (memory_root / "snapshots").mkdir(parents=True)
    entries = [{
        "candidate_id": "proposal", "source": "social form", "target": "社会形式",
        "scope": {"kind": "book", "id": "demo"},
        "language_pair": {"source": "English", "target": "Chinese"},
    }]
    snapshot = {
        "schema_version": 1, "book_id": "demo",
        "language_pair": {"source": "English", "target": "Chinese"},
        "parent_snapshot_sha256": None, "entries": entries, "review": {},
        "affected_targets": [], "approved_projection_sha256": {},
        "publication_key": "test",
    }
    digest = sha(json.dumps(snapshot, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode())
    snapshot_path = memory_root / "snapshots" / f"{digest}.json"
    snapshot_path.write_text(json.dumps(snapshot, ensure_ascii=False, sort_keys=True, separators=(",", ":")), encoding="utf-8")
    atomic_write_json(memory_root / "current.json", {"snapshot_sha256": digest, "path": f"snapshots/{digest}.json"})
    projection = current_effective_projection(
        tmp_path, "translate", source, "a.md", book_id="demo",
        source_language="English", target_language="Chinese",
        chapter_groups={"chapter-1": ["a.md"]},
    )
    assert projection["entries"] == [{"kind": "memory", "source": "social form", "target": "社会形式"}]
    assert projection["memory_entries"] == projection["entries"]
    assert len(projection["projection_sha256"]) == 64
    assert "candidate_id" not in projection["entries"][0]


def test_session_gate_checks_both_consumers_and_claimed_leases_read_only(tmp_path):
    path = dispatch_lease_path(tmp_path, "translate-html")
    now = "2999-01-01T00:00:00+00:00"
    path.write_text(json.dumps({"schema_version": 1, "leases": [{"status": "claimed", "lease_expires_at": now}]}), encoding="utf-8")
    before = path.read_bytes()
    with pytest.raises(ValueError, match="active/claimed translate-html"):
        session_attestation(tmp_path, "translate", confirmed=True)
    assert path.read_bytes() == before


def test_registry_change_after_attestation_blocks_before_prepare_writes(tmp_path):
    attestation = session_attestation(tmp_path, "translate", confirmed=True)
    marker = tmp_path / "unit_contexts" / "existing.json"
    marker.parent.mkdir()
    marker.write_text("existing", encoding="utf-8")
    path = dispatch_lease_path(tmp_path, "translate-html")
    path.write_text(json.dumps({"schema_version": 1, "leases": [{"status": "claimed", "lease_expires_at": "2999-01-01T00:00:00Z"}]}), encoding="utf-8")
    with pytest.raises(ValueError, match="registries changed"):
        verify_session_attestation(tmp_path, attestation)
    assert marker.read_text(encoding="utf-8") == "existing"


def test_wave_review_requires_current_validated_units_in_all_prior_waves():
    from pdf2epub.dynamic_terminology import _assert_dispatch_waves_current

    manifest = {
        "dispatch_plan": [
            {"wave": 1, "worker_ids": ["worker-a"]},
            {"wave": 2, "worker_ids": ["worker-b"]},
        ],
        "worker_handoffs": [
            {"worker_id": "worker-a", "dispatch_wave": 1, "assigned_files": ["a.md"]},
            {"worker_id": "worker-b", "dispatch_wave": 2, "assigned_files": ["b.md"]},
        ],
        "unit_context_sha256": {"a.md": "context-a", "b.md": "context-b"},
    }
    inventory = {
        name: {"validated": True, "validated_context_sha256": context}
        for name, context in (("a.md", "context-a"), ("b.md", "context-b"))
    }

    _assert_dispatch_waves_current(manifest, inventory, 2)
    for invalid in (
        {**inventory, "a.md": {"validated": False, "validated_context_sha256": "context-a"}},
        {**inventory, "a.md": {"validated": True, "validated_context_sha256": "stale-context"}},
        {**inventory, "b.md": {"validated": False, "validated_context_sha256": "context-b"}},
    ):
        with pytest.raises(ValueError, match="wave [12] (is incomplete|context checkpoint is stale or missing)"):
            _assert_dispatch_waves_current(manifest, invalid, 2)


def test_review_prepare_command_claimed_session_gate_precedes_all_writes(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from pdf2epub.commands import terminology_memory as command

    output = tmp_path / "book"
    output.mkdir()
    marker = output / "term-memory-review_prompt.md"
    marker.write_bytes(b"previous prompt")
    lease_path = dispatch_lease_path(output, "translate-html")
    lease_path.parent.mkdir(parents=True, exist_ok=True)
    lease_path.write_text(json.dumps({
        "schema_version": 1,
        "leases": [{"status": "claimed", "lease_expires_at": "2999-01-01T00:00:00Z"}],
    }), encoding="utf-8")
    before = marker.read_bytes()
    monkeypatch.setattr(command, "_task_context", lambda _args: (
        {"translation": {"dynamic_terminology": {"enabled": True}}},
        "demo", output, {}, "English", "Chinese", {}, None,
    ))
    args = SimpleNamespace(task="translate", config="unused", wave=None,
                           confirm_no_active_sessions=True, consistency_only_file=[])
    assert command.review_prepare_command(args) == 1
    assert marker.read_bytes() == before
    assert not (output / "dynamic_terminology" / "review_packet.json").exists()


@pytest.mark.parametrize("task", ["translate", "html-prepare"])
def test_real_prepare_entry_claimed_gate_runs_before_logging_and_writes(tmp_path, monkeypatch, task):
    from types import SimpleNamespace
    from pdf2epub.subagent_dispatch import dispatch_lease_path

    output = tmp_path / "book"
    output.mkdir()
    if task == "translate":
        from pdf2epub.commands import markdown as command
        monkeypatch.setattr(command, "load_config", lambda _path: {
            "title": "demo", "translation": {"dynamic_terminology": {"enabled": True}},
        })
        monkeypatch.setattr(command, "load_book_context", lambda *a, **k: (_ for _ in ()).throw(AssertionError("logging context must not load")))
        monkeypatch.setattr("pdf2epub.utils.paths.book_output_dir", lambda _title: output)
        entry = lambda args: command._prepare_pdf_markdown_task(args, "translate")
        args = SimpleNamespace(config="unused", confirm_no_active_sessions=True)
        consumer = "translate-html"
    else:
        from pdf2epub.commands import html as command
        (output / "input.epub").write_bytes(b"synthetic")
        monkeypatch.setattr(command, "load_config", lambda _path: {
            "title": "demo", "input_epub": "input.epub",
            "translation": {"dynamic_terminology": {"enabled": True}},
        })
        monkeypatch.setattr(command, "resolve_book_input_path", lambda *a, **k: output / "input.epub")
        monkeypatch.setattr(command, "configure_logging", lambda *a, **k: (_ for _ in ()).throw(AssertionError("logging must not start")))
        monkeypatch.setattr(command, "book_output_dir", lambda _title: output)
        entry = lambda args: command._load_html_book_context(args, "html-prepare")
        args = SimpleNamespace(config="unused", input=None, confirm_no_active_sessions=True)
        consumer = "translate"
    lease = dispatch_lease_path(output, consumer)
    lease.parent.mkdir(parents=True, exist_ok=True)
    lease.write_text(json.dumps({
        "schema_version": 1,
        "leases": [{"status": "claimed", "lease_expires_at": "2999-01-01T00:00:00Z"}],
    }), encoding="utf-8")
    before = {path.relative_to(output).as_posix(): path.read_bytes() for path in output.rglob("*") if path.is_file()}
    result = entry(args)
    assert result == 1 if task == "translate" else result is None
    after = {path.relative_to(output).as_posix(): path.read_bytes() for path in output.rglob("*") if path.is_file()}
    assert after == before
    assert not (output / "logs").exists()


@pytest.mark.parametrize(
    ("task", "source_name", "target_name", "validation_name", "scope_kind", "review_decision", "target_preexists"),
    [
        ("translate", "polished_markdown", "translated", "translate_validation.json", "chapter", "consistent", True),
        ("translate-html", "compressed_units", "translated_compressed", "translate-html_validation.json", "chapter", "consistent", True),
        ("translate", "polished_markdown", "translated", "translate_validation.json", "book", "consistent", True),
        ("translate", "polished_markdown", "translated", "translate_validation.json", "chapter", "retranslate_required", True),
        ("translate", "polished_markdown", "translated", "translate_validation.json", "chapter", "retranslate_required", False),
    ],
)
def test_actual_handoff_candidate_review_publish_and_resume(tmp_path, monkeypatch, task, source_name, target_name, validation_name, scope_kind, review_decision, target_preexists):
    output = tmp_path
    source = output / source_name
    target = output / target_name
    source.mkdir()
    target.mkdir()
    texts = {
        "a.md": "social form occurs here",
        "b.md": "unrelated chapter one text",
        "c.md": "social form occurs in another chapter",
    }
    translations = {
        "a.md": "社会形式在此出现",
        "b.md": "无关的第一章文本",
        "c.md": "社会形式在另一章出现",
    }
    for name, text in texts.items():
        (source / name).write_text(text, encoding="utf-8")
        if target_preexists:
            (target / name).write_text(translations[name], encoding="utf-8")
    if task == "translate-html":
        for name in texts:
            (source / f"{Path(name).stem}.mapping.json").write_text(
                json.dumps({"translation_parts": [name], "original_extension": ".xhtml"}),
                encoding="utf-8",
            )
        (output / "input.epub").write_bytes(b"synthetic epub")
    chapters = {"chapter-1": ["a.md", "b.md"], "chapter-2": ["c.md"]}
    unit_contexts = build_dynamic_unit_contexts(
        output, source, {}, [], source_language="English", target_language="Chinese", chapter_groups=chapters,
    )
    paths = prepare_markdown_subagent(
        output, task, source, target, "English", "Chinese", config={},
        unit_context_files=unit_contexts, chapter_groups=chapters,
    )
    initial_manifest = json.loads(paths["manifest"].read_text(encoding="utf-8"))
    preexisting = {path.name: sha(path.read_bytes()) for path in target.glob("*.md")}
    initial_manifest["dynamic_terminology"] = {
        "schema_version": 1, "enabled": True, "current_snapshot_sha256": None,
        "base_unit_context_files": {}, "preexisting_target_inventory": preexisting,
        "preexisting_target_inventory_sha256": sha(json.dumps(
            preexisting, sort_keys=True, separators=(",", ":"),
        ).encode()),
    }
    atomic_write_json(paths["manifest"], initial_manifest)
    from pdf2epub.dynamic_terminology import dynamic_handoff_dir_name, publication_target_inventory
    first_handoff_dir = dynamic_handoff_dir_name(
        task, None, source, unit_contexts, manifest_path=paths["manifest"],
    )
    handoffs = write_worker_handoffs(
        output, paths["manifest"], paths["prompt"], handoff_dir_name=first_handoff_dir,
        retire_previous=False,
    )
    attach_candidate_sidecars(
        output, handoffs, task=task, chapter_by_file={name: chapter for chapter, files in chapters.items() for name in files},
        source_language="English", target_language="Chinese", book_id="demo",
    )
    if not target_preexists:
        # This target is produced after prepare but before review/publication.
        # It still needs a publication-time frozen projection and review.
        for name in texts:
            (target / name).write_text(translations[name], encoding="utf-8")
    manifest_path = paths["manifest"]
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    source_hashes = {name: sha((source / name).read_bytes()) for name in texts}
    target_hashes = {name: sha((target / name).read_bytes()) for name in texts}
    if task == "translate":
        from pdf2epub.commands.markdown import _persist_file_validation_checkpoint
        from pdf2epub.markdown_subagent_validation import validate_markdown_subagent

        for name in texts:
            report = validate_markdown_subagent(
                output, task, source, target, selected_files=[name],
                create_validated_copy=False, target_language="Chinese",
            )
            assert report["all_passed"] and name in report["valid_files"]
            _persist_file_validation_checkpoint(output, report, task)
    else:
        from pdf2epub.html_translation.translation_validation import (
            HTMLTranslationValidator, HTMLValidationWorkspace,
        )

        manifest["input_epub"] = "input.epub"
        manifest["input_epub_sha256"] = sha((output / "input.epub").read_bytes())
        atomic_write_json(manifest_path, manifest)
        validator = HTMLTranslationValidator(HTMLValidationWorkspace(
            output, source, target, {"translation": {"target_language": "Chinese"}},
        ))
        for name in texts:
            report = validator.validate(file_name=name)
            assert report["all_passed"] and name in report["valid_files"]
            validator.persist_checkpoint(report)
    candidate_handoff = next(h for h in handoffs if "a.md" in h["assigned_files"])
    scoped_path = output / candidate_handoff["manifest"]
    scoped = json.loads(scoped_path.read_text(encoding="utf-8"))
    scoped_bytes = scoped_path.read_bytes()
    tampered = dict(scoped)
    tampered["assigned_files"] = ["c.md"]
    atomic_write_json(scoped_path, tampered)
    with pytest.raises(ValueError, match="assigned_files do not match parent"):
        prepare_review_packet(
            output, task, book_id="demo", source_language="English", target_language="Chinese",
            chapter_groups=chapters, book_entities_path=None,
        )
    scoped_path.write_bytes(scoped_bytes)
    scoped = json.loads(scoped_bytes)
    contract = scoped["dynamic_terminology"]
    frozen_artifacts = {
        output / candidate_handoff["manifest"]: (output / candidate_handoff["manifest"]).read_bytes(),
        output / candidate_handoff["prompt"]: (output / candidate_handoff["prompt"]).read_bytes(),
    }
    for context_relative in (scoped.get("worker_context_files", {}) or {}).values():
        context_path = output / context_relative
        frozen_artifacts[context_path] = context_path.read_bytes()
    sidecar = output / contract["candidate_sidecar"]
    sidecar.parent.mkdir(parents=True, exist_ok=True)
    sidecar_data = {
        "schema_version": 1,
        "assignment_sha256": scoped["assignment_sha256"],
        "candidates": [{
            "candidate_id": "social-form", "source": "social form", "target": "社会形式",
            "source_file": "a.md", "target_file": "a.md",
            "source_sha256": source_hashes["a.md"], "target_sha256": target_hashes["a.md"],
            "source_evidence": "social form occurs here", "target_evidence": "社会形式在此出现",
            "assignment_sha256": scoped["assignment_sha256"],
            "language_pair": {"source": "English", "target": "Chinese"},
            "scope": {"kind": scope_kind, "id": "demo" if scope_kind == "book" else "chapter-1"},
        }],
    }
    atomic_write_json(sidecar, sidecar_data)
    if task == "translate" and "b.md" in texts:
        # An unselected-wave review still verifies every assigned file in the
        # candidate worker's full wave, including files without a candidate.
        ledger_path = output / "translate_file_validation.json"
        original_ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
        altered_ledger = json.loads(json.dumps(original_ledger))
        altered_ledger["files"]["b.md"]["dynamic_unit_context_sha256"] = "0" * 64
        atomic_write_json(ledger_path, altered_ledger)
        with pytest.raises(ValueError, match="wave 1 (is incomplete|context checkpoint is stale or missing): b.md"):
            prepare_review_packet(
                output, task, book_id="demo", source_language="English", target_language="Chinese",
                chapter_groups=chapters, book_entities_path=None,
            )
        atomic_write_json(ledger_path, original_ledger)
    candidate = sidecar_data["candidates"][0]
    for edits, error in (
        ({"source_sha256": "0" * 64}, "source hash"),
        ({"target_sha256": "0" * 64}, "target_sha256"),
        ({"language_pair": {"source": "French", "target": "Chinese"}}, "language pair"),
        ({"target_file": "b.md"}, "crosses source/target"),
    ):
        atomic_write_json(sidecar, {
            **sidecar_data,
            "candidates": [{**candidate, **edits}],
        })
        with pytest.raises(ValueError, match=error):
            prepare_review_packet(
                output, task, book_id="demo", source_language="English", target_language="Chinese",
                chapter_groups=chapters, book_entities_path=None,
            )
    atomic_write_json(sidecar, sidecar_data)
    from pdf2epub.commands import terminology_memory as terminology_commands
    from types import SimpleNamespace
    monkeypatch.setattr(terminology_commands, "load_config", lambda _path: {
        "title": "demo", "translation": {
            "source_language": "English", "target_language": "Chinese",
            "dynamic_terminology": {"enabled": True},
        },
    })
    monkeypatch.setattr(terminology_commands, "book_output_dir", lambda _title: output)
    cli_args = SimpleNamespace(
        task=task, config="unused", wave=None, confirm_no_active_sessions=True,
        consistency_only_file=[],
    )
    assert terminology_commands.review_prepare_command(cli_args) == 0
    packet = json.loads((output / "dynamic_terminology" / "review_packet.json").read_text(encoding="utf-8"))
    assert [row["unit_id"] for row in packet["affected_targets"]] == (["a.md", "c.md"] if scope_kind == "book" else ["a.md"])
    review = {
        "prepared_packet_sha256": packet["prepared_packet_sha256"],
        "candidates": [{"candidate_id": "social-form", "decision": "approve"}],
        "affected_targets": [{
            **{key: row[key] for key in (
                "unit_id", "source_sha256", "target_sha256", "scope", "effective_projection_sha256",
            )},
            "decision": review_decision,
        } for row in packet["affected_targets"]],
    }
    review_path = output / "review.json"
    atomic_write_json(review_path, review)
    cli_args.review = str(review_path)
    assert terminology_commands.publish_command(cli_args) == 0
    digest = load_current_snapshot(output / "dynamic_terminology", book_id="demo", source_language="English", target_language="Chinese")[0]
    assert digest
    from pdf2epub.dynamic_terminology import publication_target_inventory
    published_inventory = publication_target_inventory(
        output, book_id="demo", source_language="English", target_language="Chinese",
    )
    assert "a.md" in published_inventory
    if not target_preexists:
        assert published_inventory["a.md"][0]["preexisting_target_sha256"] is None
        assert published_inventory["a.md"][0]["review_required_change"] is True
    assert "b.md" in published_inventory
    assert published_inventory["b.md"][0]["review_required_change"] is False
    if review_decision == "retranslate_required":
        from pdf2epub.terminology_memory_validation import validate_dynamic_terminology_units

        publication_gate = validate_dynamic_terminology_units(
            output, task=task, source_dir=source, target_dir=target,
            config={"title": "demo", "translation": {"dynamic_terminology": {"enabled": True}}},
        )
        assert publication_gate["valid"] is False
        assert any(row.get("file") == "a.md" for row in publication_gate["errors"])
    consistency = affected_target_consistency_state(output, book_id="demo", source_language="English", target_language="Chinese")
    assert consistency["a.md"][0]["decision"] == review_decision
    assert consistency["a.md"][0]["approved_projection_sha256"]

    _, memory_entries = load_current_snapshot(output / "dynamic_terminology", book_id="demo", source_language="English", target_language="Chinese")
    resumed_contexts = build_dynamic_unit_contexts(
        output, source, {}, memory_entries, source_language="English", target_language="Chinese", chapter_groups=chapters,
    )
    assert "social form" in json.loads(resumed_contexts["a.md"].read_text(encoding="utf-8"))["entries"][0]["source"]
    assert bool(json.loads(resumed_contexts["c.md"].read_text(encoding="utf-8"))["entries"]) == (scope_kind == "book")
    resume_paths = prepare_markdown_subagent(
        output, task, source, target, "English", "Chinese", config={}, resume=True,
        unit_context_files=resumed_contexts, chapter_groups=chapters,
    )
    if review_decision == "retranslate_required":
        # A replacement target is structurally valid but its dynamic memory
        # gate remains blocked until the separate consistency review publishes.
        (target / "a.md").write_text("重新译：社会形式在此出现", encoding="utf-8")
        # Recreate the resume manifest with the frozen post-publication context.
        resume_paths = prepare_markdown_subagent(
            output, task, source, target, "English", "Chinese", config={}, resume=True,
            unit_context_files=resumed_contexts, chapter_groups=chapters,
        )
        current_targets = {name: sha((target / name).read_bytes()) for name in texts}
        resumed_manifest = json.loads(resume_paths["manifest"].read_text(encoding="utf-8"))
        current_snapshot = load_current_snapshot(
            output / "dynamic_terminology", book_id="demo",
            source_language="English", target_language="Chinese",
        )[0]
        resumed_manifest["dynamic_terminology"] = {
            "schema_version": 1, "enabled": True, "current_snapshot_sha256": current_snapshot,
            "base_unit_context_files": {}, "preexisting_target_inventory": current_targets,
            "preexisting_target_inventory_sha256": sha(json.dumps(
                current_targets, sort_keys=True, separators=(",", ":"),
            ).encode()),
        }
        atomic_write_json(resume_paths["manifest"], resumed_manifest)
        resumed_handoffs = write_worker_handoffs(
            output, resume_paths["manifest"], resume_paths["prompt"],
            handoff_dir_name=dynamic_handoff_dir_name(
                task, current_snapshot, source, resumed_contexts,
                manifest_path=resume_paths["manifest"],
            ), retire_previous=False,
        )
        attach_candidate_sidecars(
            output, resumed_handoffs, task=task,
            chapter_by_file={name: chapter for chapter, files in chapters.items() for name in files},
            source_language="English", target_language="Chinese", book_id="demo",
        )
        consistency_args = SimpleNamespace(
            task=task, config="unused", wave=None, confirm_no_active_sessions=True,
            consistency_only_file=["a.md"],
        )
        assert terminology_commands.review_prepare_command(consistency_args) == 0
        consistency_packet = json.loads(
            (output / "dynamic_terminology" / "review_packet.json").read_text(encoding="utf-8")
        )
        consistency_review = {
            "prepared_packet_sha256": consistency_packet["prepared_packet_sha256"],
            "candidates": [],
            "affected_targets": [{
                **{key: consistency_packet["affected_targets"][0][key] for key in (
                    "unit_id", "source_sha256", "target_sha256", "scope", "effective_projection_sha256",
                )},
                "decision": "consistent",
            }],
        }
        consistency_review_path = output / "consistency_review.json"
        atomic_write_json(consistency_review_path, consistency_review)
        consistency_args.consistency_only_file = []
        consistency_args.review = str(consistency_review_path)
        assert terminology_commands.publish_command(consistency_args) == 0
        from pdf2epub.terminology_memory_validation import validate_dynamic_terminology_units

        final_validation = validate_dynamic_terminology_units(
            output, task=task, source_dir=source, target_dir=target, config={},
        )
        assert final_validation["valid"] is True, final_validation["errors"]
    resumed = json.loads(resume_paths["manifest"].read_text(encoding="utf-8"))
    if review_decision == "consistent":
        assert resumed["pending_files"] == (["a.md", "c.md"] if scope_kind == "book" else ["a.md"])
    resumed_handoffs = write_worker_handoffs(
        output, resume_paths["manifest"], resume_paths["prompt"],
        handoff_dir_name=dynamic_handoff_dir_name(
            task, digest, source, resumed_contexts, manifest_path=resume_paths["manifest"],
        ), retire_previous=False,
    )
    assert resumed_handoffs
    assert resumed_handoffs[0]["manifest"] != candidate_handoff["manifest"]
    for path, expected_bytes in frozen_artifacts.items():
        assert path.read_bytes() == expected_bytes


def test_html_inventory_uses_active_mapping_parts_and_rejects_new_active_parts(tmp_path):
    from pdf2epub.dynamic_terminology import _build_unit_inventory

    output = tmp_path / "output"
    source = output / "compressed_units"
    target = output / "translated_compressed"
    source.mkdir(parents=True)
    target.mkdir()
    (source / "chapter.md").write_text("legacy aggregate source", encoding="utf-8")
    (source / "chapter.part1.md").write_text("active split source", encoding="utf-8")
    (source / "chapter.mapping.json").write_text(json.dumps({
        "translation_parts": ["chapter.part1.md"],
    }), encoding="utf-8")
    (target / "chapter.md").write_text("legacy aggregate translation", encoding="utf-8")
    (target / "chapter.part1.md").write_text("active split translation", encoding="utf-8")
    active_target_sha = sha((target / "chapter.part1.md").read_bytes())
    context_sha = "a" * 64
    preexisting = {"chapter.part1.md": active_target_sha}
    manifest = {
        "files": ["chapter.part1.md"],
        "source_sha256": {"chapter.part1.md": sha((source / "chapter.part1.md").read_bytes())},
        "unit_context_sha256": {"chapter.part1.md": context_sha},
        "dynamic_terminology": {
            "enabled": True,
            "preexisting_target_inventory": preexisting,
            "preexisting_target_inventory_sha256": sha(json.dumps(
                preexisting, sort_keys=True, separators=(",", ":"),
            ).encode()),
        },
    }
    report = {
        "valid_files": ["chapter.part1.md"],
        "source_sha256": manifest["source_sha256"],
        "target_sha256": {"chapter.part1.md": active_target_sha},
        "validated_unit_context_sha256": {"chapter.part1.md": context_sha},
    }
    inventory = _build_unit_inventory(
        source, target, manifest, {"chapter-1": ["chapter.part1.md"]},
        report, task="translate-html",
    )
    assert list(inventory) == ["chapter.part1.md"]
    assert inventory["chapter.part1.md"]["validated"] is True

    (source / "chapter.part2.md").write_text("new active split source", encoding="utf-8")
    (source / "chapter.mapping.json").write_text(json.dumps({
        "translation_parts": ["chapter.part1.md", "chapter.part2.md"],
    }), encoding="utf-8")
    with pytest.raises(ValueError, match="active HTML mapping translation parts"):
        _build_unit_inventory(
            source, target, manifest, {"chapter-1": ["chapter.part1.md"]},
            report, task="translate-html",
        )


def test_dynamic_review_prepare_rejects_memory_directory_resolved_outside_book(tmp_path, monkeypatch):
    from pdf2epub import dynamic_terminology

    output = tmp_path / "book"
    outside = tmp_path / "outside"
    output.mkdir()
    outside.mkdir()
    original_resolve = Path.resolve

    def resolve_with_junction(path, *args, **kwargs):
        if path == output / "dynamic_terminology":
            return outside / "dynamic_terminology"
        return original_resolve(path, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", resolve_with_junction)
    with pytest.raises(ValueError, match="dynamic terminology directory escapes"):
        prepare_review_packet(
            output, "translate", book_id="demo", source_language="English",
            target_language="Chinese", chapter_groups={}, book_entities_path=None,
        )
    assert list(outside.iterdir()) == []


def test_dynamic_prepare_review_publish_accepts_cwd_relative_output_root(tmp_path, monkeypatch):
    from pdf2epub.commands.markdown import _persist_file_validation_checkpoint
    from pdf2epub.dynamic_terminology import dynamic_handoff_dir_name, publication_target_inventory
    from pdf2epub.markdown_subagent_validation import validate_markdown_subagent

    monkeypatch.chdir(tmp_path)
    output = Path("output/Book")
    output.mkdir(parents=True)
    source = output / "polished_markdown"
    target = output / "translated"
    source.mkdir()
    target.mkdir()
    (source / "chapter.md").write_text("social form appears here", encoding="utf-8")
    contexts = build_dynamic_unit_contexts(
        output, source, {}, [], source_language="English", target_language="Chinese",
        chapter_groups={"chapter-1": ["chapter.md"]},
    )
    paths = prepare_markdown_subagent(
        output, "translate", source, target, "English", "Chinese", config={},
        unit_context_files=contexts, chapter_groups={"chapter-1": ["chapter.md"]},
    )
    manifest_path = output / "translate_subagent_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["book_title"] = "Book"
    manifest["dynamic_terminology"] = {
        "schema_version": 1, "enabled": True, "current_snapshot_sha256": None,
        "base_unit_context_files": {}, "preexisting_target_inventory": {},
        "preexisting_target_inventory_sha256": sha(b"{}"),
    }
    atomic_write_json(manifest_path, manifest)
    handoffs = write_worker_handoffs(
        output, paths["manifest"], paths["prompt"],
        handoff_dir_name=dynamic_handoff_dir_name(
            "translate", None, source, contexts, manifest_path=paths["manifest"],
        ),
        retire_previous=False,
    )
    attach_candidate_sidecars(
        output, handoffs, task="translate", chapter_by_file={"chapter.md": "chapter-1"},
        source_language="English", target_language="Chinese", book_id="Book",
    )
    target_file = target / "chapter.md"
    target_file.write_text("社会形式在此出现。", encoding="utf-8")
    output_abs, source_abs, target_abs = output.resolve(), source.resolve(), target.resolve()
    report = validate_markdown_subagent(
        output_abs, "translate", source_abs, target_abs,
        create_validated_copy=False, selected_files=["chapter.md"], target_language="Chinese",
    )
    assert report["all_passed"] is True
    _persist_file_validation_checkpoint(output_abs, report, "translate")

    handoff = handoffs[0]
    scoped_path = output_abs / handoff["manifest"]
    scoped = json.loads(scoped_path.read_text(encoding="utf-8"))
    sidecar = output_abs / scoped["dynamic_terminology"]["candidate_sidecar"]
    atomic_write_json(sidecar, {
        "schema_version": 1, "assignment_sha256": scoped["assignment_sha256"],
        "candidates": [{
            "candidate_id": "social-form", "source": "social form", "target": "社会形式",
            "source_file": "chapter.md", "target_file": "chapter.md",
            "source_sha256": sha((source_abs / "chapter.md").read_bytes()),
            "target_sha256": sha(target_file.read_bytes()),
            "source_evidence": "social form appears here", "target_evidence": "社会形式在此出现。",
            "assignment_sha256": scoped["assignment_sha256"],
            "language_pair": {"source": "English", "target": "Chinese"},
            "scope": {"kind": "chapter", "id": "chapter-1"},
        }],
    })
    packet, authority = prepare_review_packet(
        output, "translate", book_id="Book", source_language="English", target_language="Chinese",
        chapter_groups={"chapter-1": ["chapter.md"]}, book_entities_path=None,
    )
    review = {
        "prepared_packet_sha256": packet["prepared_packet_sha256"],
        "candidates": [{"candidate_id": "social-form", "decision": "approve"}],
        "affected_targets": [{
            **{key: packet["affected_targets"][0][key] for key in (
                "unit_id", "source_sha256", "target_sha256", "scope", "effective_projection_sha256",
            )},
            "decision": "retranslate_required",
        }],
    }
    review_path = output_abs / "dynamic_terminology" / "review.json"
    atomic_write_json(review_path, review)
    attestation = session_attestation(output, "translate", confirmed=True)
    attestation.update({"book_id": "Book", "authority_sha256": authority.authority_sha256})
    digest = publish_review_packet(
        output, "translate", review_path, authority=authority,
        current_snapshot_sha256=None, operator_attestation=attestation,
    )
    assert digest
    assert "chapter.md" in publication_target_inventory(
        output, book_id="Book", source_language="English", target_language="Chinese",
    )
