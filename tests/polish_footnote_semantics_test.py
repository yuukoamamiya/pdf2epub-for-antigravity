import hashlib
import json

import pytest

from pdf2epub.markdown_handoff_context import MarkdownHandoffRequest
from pdf2epub.polish_footnote_semantics import (
    SIDECAR_DIRECTORY,
    build_semantic_review_contract,
    clear_reflow_round,
    escalated_reflow_files,
    load_reflow_state,
    polish_issue_task_id,
    record_reflow_round,
    reset_reflow_round,
    validate_semantic_review_sidecar,
)


def _review(source, target, contract, *, status="clear", issues=None):
    return {
        "schema_version": 1,
        "file": contract["file"],
        "source_sha256": hashlib.sha256(source.encode()).hexdigest(),
        "target_sha256": hashlib.sha256(target.encode()).hexdigest(),
        "context_sha256": contract["context_sha256"],
        "status": status,
        "reviewed_all": True,
        "reviewed_note_ids": contract["expected_note_ids"],
        "coverage": {
            "body_reviewed": {"status": "reviewed"},
            "definitions_reviewed": {"status": "reviewed"},
            "cross_boundary_reviewed": {"status": "not_applicable", "reason": "The assigned unit has no split boundary."},
        },
        "issues": issues or [],
    }


def test_contract_requires_body_and_note_coverage_even_when_no_markers(tmp_path):
    source = "A body paragraph.\n"
    target = "一个正文段落。\n"
    contract = build_semantic_review_contract("chapter.md", source)
    path = tmp_path / contract["sidecar"]
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(_review(source, target, contract)), encoding="utf-8")
    result = validate_semantic_review_sidecar(path, contract, source_text=source, target_text=target)
    assert result["valid"] is True
    assert contract["expected_note_ids"] == []
    assert contract["sidecar"] == f"{SIDECAR_DIRECTORY}/chapter.json"


def test_raw_crlf_hash_and_code_fenced_markers_are_handled_exactly(tmp_path):
    source = "Body[^1].\r\n\r\n[^1]: Note.\r\n```markdown\r\n[^9]: Example.\r\n```\r\n"
    target = "Body[^1].\r\n\r\n[^1]: Note.\r\n"
    contract = build_semantic_review_contract("crlf.md", source)
    assert contract["expected_note_ids"] == ["1"]
    path = tmp_path / "review.json"
    path.write_text(json.dumps(_review(source, target, contract)), encoding="utf-8")
    assert validate_semantic_review_sidecar(
        path, contract, source_text=source, target_text=target
    )["valid"] is True
    assert validate_semantic_review_sidecar(
        path, contract, source_text=source.replace("\r\n", "\n"), target_text=target
    )["valid"] is False


def test_prepare_emits_per_file_semantic_contract_only_when_enabled(tmp_path):
    from pdf2epub.markdown_handoff import prepare_markdown_subagent
    from pdf2epub.polish_footnote_semantics import validate_prepared_file_review

    source_dir = tmp_path / "ocr_markdown"
    target_dir = tmp_path / "polished_markdown"
    source_dir.mkdir()
    target_dir.mkdir()
    (source_dir / "chapter.md").write_text("Body[^1].\n\n[^1]: Note.\n", encoding="utf-8")
    paths = prepare_markdown_subagent(
        MarkdownHandoffRequest(
            output_dir=tmp_path,
            task="polish",
            source_dir=source_dir,
            target_dir=target_dir,
            source_language="English",
            target_language="Chinese",
            config={},
            footnote_semantics_enabled=True,
        )
    )
    manifest = json.loads(paths["manifest"].read_text(encoding="utf-8"))
    assert manifest["footnote_semantics"]["enabled"] is True
    assert set(manifest["footnote_semantics"]["files"]) == {"chapter.md"}
    assert manifest["footnote_semantics"]["files"]["chapter.md"]["expected_note_ids"] == ["1"]
    contract = manifest["footnote_semantics"]["files"]["chapter.md"]
    source = (source_dir / "chapter.md").read_bytes().decode("utf-8")
    target = "Body[^1].\n\n[^1]: Note.\n"
    (target_dir / "chapter.md").write_bytes(target.encode())
    sidecar = tmp_path / contract["sidecar"]
    sidecar.parent.mkdir()
    sidecar.write_text(json.dumps(_review(source, target, contract)), encoding="utf-8")
    review = validate_prepared_file_review(
        tmp_path, "chapter.md", source_text=source, target_text=target, required=True,
    )
    assert review["valid"] is True
    from pdf2epub.subagent_runtime import write_worker_handoffs

    handoffs = write_worker_handoffs(
        tmp_path, paths["manifest"], paths["prompt"],
        handoff_dir_name="polish_worker_handoffs",
    )
    worker_manifest = json.loads((tmp_path / handoffs[0]["manifest"]).read_text(encoding="utf-8"))
    assert set(worker_manifest["footnote_semantics"]["files"]) == {"chapter.md"}
    worker_prompt = (tmp_path / handoffs[0]["prompt"]).read_text(encoding="utf-8")
    assert contract["sidecar"] in worker_prompt

    disabled = prepare_markdown_subagent(
        MarkdownHandoffRequest(
            output_dir=tmp_path,
            task="polish",
            source_dir=source_dir,
            target_dir=target_dir,
            source_language="English",
            target_language="Chinese",
            config={},
            footnote_semantics_enabled=False,
        )
    )
    disabled_manifest = json.loads(disabled["manifest"].read_text(encoding="utf-8"))
    assert "footnote_semantics" not in disabled_manifest


def test_sidecar_blocks_stale_hash_incomplete_coverage_and_semantic_issue(tmp_path):
    source = "Text[^1].\n\n[^1]: Note text.\n"
    target = "Body[^1].\n\n[^1]: A note.\n"
    contract = build_semantic_review_contract("part.md", source)
    path = tmp_path / "review.json"
    payload = _review(source, target, contract)
    payload["coverage"]["body_reviewed"] = {"status": "not_applicable", "reason": ""}
    payload["target_sha256"] = "0" * 64
    path.write_text(json.dumps(payload), encoding="utf-8")
    result = validate_semantic_review_sidecar(path, contract, source_text=source, target_text=target)
    assert result["valid"] is False
    assert any("target_sha256" in error for error in result["errors"])
    assert any("body_reviewed" in error for error in result["errors"])

    issue = {
        "issue_id": "issue-1", "code": "body_footnote_misplacement",
        "source_excerpt": "Note text.", "target_excerpt": "A note.",
        "reason": "The source note appears to contain a continuation of body prose.",
        "file": "part.md", "unit_id": "unit-1",
        "source_sha256": contract["source_sha256"],
        "target_sha256": hashlib.sha256(target.encode()).hexdigest(),
        "context_sha256": contract["context_sha256"],
    }
    payload = _review(source, target, contract, status="issues", issues=[issue])
    path.write_text(json.dumps(payload), encoding="utf-8")
    result = validate_semantic_review_sidecar(path, contract, source_text=source, target_text=target)
    assert result["contract_valid"] is True
    assert result["valid"] is False
    assert result["issues"] == [issue]

    malformed = _review(source, target, contract)
    malformed["reviewed_note_ids"] = [{}]
    malformed["issues"] = [issue, issue]
    malformed["status"] = "issues"
    path.write_text(json.dumps(malformed), encoding="utf-8")
    result = validate_semantic_review_sidecar(path, contract, source_text=source, target_text=target)
    assert result["valid"] is False
    assert any("cover every expected" in error for error in result["errors"])
    assert any("duplicates issue_id" in error for error in result["errors"])


@pytest.mark.parametrize("bad_status", [None, {}, [], 1, False])
def test_malformed_top_level_status_fails_closed_without_crashing(tmp_path, bad_status):
    source, target = "Body.\n", "Body.\n"
    contract = build_semantic_review_contract("unit.md", source)
    payload = _review(source, target, contract)
    payload["status"] = bad_status
    path = tmp_path / "sidecar.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    result = validate_semantic_review_sidecar(path, contract, source_text=source, target_text=target)
    assert result["valid"] is False
    assert result["errors"]


def test_full_polish_receipt_requires_current_sidecar_and_revalidation(tmp_path):
    from pdf2epub.markdown_subagent_validation import validate_markdown_subagent
    from pdf2epub.polish_footnote_semantics import validate_prepared_file_review
    from pdf2epub.commands.sources import _polished_stage_is_current
    from pdf2epub.validation_receipts import validation_receipt_is_current

    source_dir = tmp_path / "ocr_markdown"
    target_dir = tmp_path / "polished_markdown"
    source_dir.mkdir()
    target_dir.mkdir()
    source = "Body[^1].\n\n[^1]: A note.\n"
    target = "Body[^1].\n\n[^1]: A note.\n"
    (source_dir / "chapter.md").write_bytes(source.encode())
    (target_dir / "chapter.md").write_bytes(target.encode())
    contract = build_semantic_review_contract("chapter.md", source)
    manifest = {
        "task": "polish", "source_language": "English", "target_language": "Chinese",
        "source_sha256": {"chapter.md": hashlib.sha256(source.encode()).hexdigest()},
        "footnote_semantics": {"schema_version": 1, "enabled": True, "files": {"chapter.md": contract}},
    }
    (tmp_path / "polish_subagent_manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    sidecar = tmp_path / contract["sidecar"]
    sidecar.parent.mkdir()
    sidecar.write_text(json.dumps(_review(source, target, contract)), encoding="utf-8")
    config = tmp_path / "config.yaml"
    config.write_text("title: Test\nfootnotes:\n  mode: auto\n", encoding="utf-8")

    report = validate_markdown_subagent(
        tmp_path, "polish", source_dir, target_dir,
        structural_patterns=(r"^#{1,6}\s",),
        require_footnote_semantic_review=True,
    )
    assert report["all_passed"] is True
    report_path = tmp_path / "polish_validation.json"
    assert validation_receipt_is_current(
        report_path, source_dir, target_dir, task="polish",
        output_dir=tmp_path, config_path=config,
    )
    assert _polished_stage_is_current(
        tmp_path, target_dir / "validated", source_dir,
        config={"title": "Test", "footnotes": {"mode": "auto"}},
    )

    # A sidecar rewrite invalidates the receipt even when its JSON semantics remain clear.
    sidecar.write_text(json.dumps(_review(source, target, contract), indent=1), encoding="utf-8")
    assert not validation_receipt_is_current(
        report_path, source_dir, target_dir, task="polish",
        output_dir=tmp_path, config_path=config,
    )
    assert not _polished_stage_is_current(
        tmp_path, target_dir / "validated", source_dir,
        config={"title": "Test", "footnotes": {"mode": "auto"}},
    )
    current = validate_prepared_file_review(
        tmp_path, "chapter.md", source_text=source, target_text=target, required=True,
    )
    assert current["valid"] is True


def test_semantic_issue_is_hard_block_and_preserves_polished_evidence(tmp_path):
    from pdf2epub.markdown_subagent_validation import validate_markdown_subagent

    source_dir = tmp_path / "ocr_markdown"
    target_dir = tmp_path / "polished_markdown"
    source_dir.mkdir()
    target_dir.mkdir()
    source = "Body line.\n\n[^1]: apparent note text.\n"
    target = "Body line.\n\n[^1]: apparent note text.\n"
    (source_dir / "chapter.md").write_bytes(source.encode())
    target_path = target_dir / "chapter.md"
    target_path.write_bytes(target.encode())
    contract = build_semantic_review_contract("chapter.md", source)
    manifest = {
        "task": "polish", "source_language": "English", "target_language": "Chinese",
        "source_sha256": {"chapter.md": contract["source_sha256"]},
        "footnote_semantics": {"schema_version": 1, "enabled": True, "files": {"chapter.md": contract}},
    }
    (tmp_path / "polish_subagent_manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    sidecar = tmp_path / contract["sidecar"]
    sidecar.parent.mkdir()
    issue = {
        "issue_id": "issue-1", "code": "possible_body_in_footnote",
        "source_excerpt": "apparent note text.", "target_excerpt": "apparent note text.",
        "reason": "This may be a continuation of the body paragraph, not note content.",
        "file": "chapter.md", "unit_id": None,
        "source_sha256": contract["source_sha256"],
        "target_sha256": hashlib.sha256(target.encode()).hexdigest(),
        "context_sha256": contract["context_sha256"],
    }
    sidecar.write_text(json.dumps(_review(source, target, contract, status="issues", issues=[issue])), encoding="utf-8")

    report = validate_markdown_subagent(
        tmp_path, "polish", source_dir, target_dir,
        create_validated_copy=True, require_footnote_semantic_review=True,
        allow_review_warnings=True, human_decision_id="test-human-ack",
    )
    assert report["all_passed"] is False
    assert any(item.get("kind") == "polish_footnote_semantics" for item in report["invalid"])
    assert report["invalid"][-1]["action"] == "footnote_prepare_validate_apply"
    assert target_path.read_bytes() == target.encode()
    assert not (target_dir / "validated" / "chapter.md").exists()
    assert not (tmp_path / "scratch" / "polish" / "invalid_outputs" / "chapter.md").exists()


def test_semantic_review_binds_the_normalized_polish_target(tmp_path):
    from pdf2epub.markdown_subagent_validation import validate_markdown_subagent

    source_dir = tmp_path / "source"
    target_dir = tmp_path / "target"
    source_dir.mkdir()
    target_dir.mkdir()
    source = "A short paragraph.\n"
    normalized_target = "A short paragraph.\n"
    wrapped_target = "```markdown\nA short paragraph.\n```\n"
    (source_dir / "unit.md").write_bytes(source.encode())
    (target_dir / "unit.md").write_bytes(wrapped_target.encode())
    contract = build_semantic_review_contract("unit.md", source)
    manifest = {
        "task": "polish", "source_sha256": {"unit.md": contract["source_sha256"]},
        "footnote_semantics": {"schema_version": 1, "enabled": True, "files": {"unit.md": contract}},
    }
    (tmp_path / "polish_subagent_manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    sidecar = tmp_path / contract["sidecar"]
    sidecar.parent.mkdir()
    sidecar.write_text(json.dumps(_review(source, normalized_target, contract)), encoding="utf-8")
    report = validate_markdown_subagent(
        tmp_path, "polish", source_dir, target_dir,
        require_footnote_semantic_review=True,
    )
    assert report["all_passed"] is True
    assert (target_dir / "unit.md").read_text(encoding="utf-8") == normalized_target
    assert report["target_sha256"]["unit.md"] == hashlib.sha256(normalized_target.encode()).hexdigest()


def test_polish_issue_task_id_is_content_addressed():
    issue = {
        "issue_id": "issue-1",
        "source_sha256": "a" * 64,
        "source_excerpt": "36 Note body.",
        "target_sha256": "b" * 64,
        "context_sha256": "c" * 64,
    }

    base = polish_issue_task_id("chapter.md", issue)
    renamed = polish_issue_task_id("chapter.md", {**issue, "issue_id": "renamed"})
    spaced = polish_issue_task_id("chapter.md", {**issue, "source_excerpt": "36  Note\nbody."})
    changed = polish_issue_task_id("chapter.md", {**issue, "source_excerpt": "36 Other body."})

    assert base == renamed == spaced
    assert base != changed
    assert base.startswith("fra-")


def test_reflow_rounds_persist_outside_manifest_and_escalate_after_two(tmp_path):
    for expected in (1, 2):
        entry = record_reflow_round(tmp_path, "chapter.md", source_sha256="a" * 64)
        assert entry["rounds"] == expected
        assert entry["escalated"] is False
    assert escalated_reflow_files(tmp_path) == {}

    # Deleting and rebuilding the polish manifest must not reset the counter.
    (tmp_path / "polish_subagent_manifest.json").write_text("{}", encoding="utf-8")
    entry = record_reflow_round(tmp_path, "chapter.md", source_sha256="d" * 64)
    assert entry["rounds"] == 3
    assert entry["escalated"] is True
    assert escalated_reflow_files(tmp_path)["chapter.md"]["source_sha256"] == "d" * 64

    reset_reflow_round(tmp_path, "chapter.md", decision_id="human-1")
    assert escalated_reflow_files(tmp_path) == {}
    state = load_reflow_state(tmp_path)
    assert state["files"]["chapter.md"]["human_decision_id"] == "human-1"
    assert state["files"]["chapter.md"]["rounds"] == 0

    record_reflow_round(tmp_path, "chapter.md")
    clear_reflow_round(tmp_path, "chapter.md")
    assert load_reflow_state(tmp_path)["files"] == {}


def test_third_semantic_issue_round_escalates_to_human_review(tmp_path):
    from pdf2epub.markdown_subagent_validation import validate_markdown_subagent

    source_dir = tmp_path / "ocr_markdown"
    target_dir = tmp_path / "polished_markdown"
    source_dir.mkdir()
    target_dir.mkdir()
    source = "Body line.\n\n[^1]: apparent note text.\n"
    target = source
    (source_dir / "chapter.md").write_bytes(source.encode())
    (target_dir / "chapter.md").write_bytes(target.encode())
    contract = build_semantic_review_contract("chapter.md", source)
    manifest = {
        "task": "polish", "source_sha256": {"chapter.md": contract["source_sha256"]},
        "footnote_semantics": {"schema_version": 1, "enabled": True, "files": {"chapter.md": contract}},
    }
    manifest_path = tmp_path / "polish_subagent_manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    issue = {
        "issue_id": "issue-1", "code": "possible_body_in_footnote",
        "source_excerpt": "apparent note text.", "target_excerpt": "apparent note text.",
        "reason": "This may be a continuation of the body paragraph, not note content.",
        "file": "chapter.md", "unit_id": None,
        "source_sha256": contract["source_sha256"],
        "target_sha256": hashlib.sha256(target.encode()).hexdigest(),
        "context_sha256": contract["context_sha256"],
    }
    sidecar = tmp_path / contract["sidecar"]
    sidecar.parent.mkdir()
    sidecar.write_text(
        json.dumps(_review(source, target, contract, status="issues", issues=[issue])),
        encoding="utf-8",
    )

    first = validate_markdown_subagent(
        tmp_path, "polish", source_dir, target_dir,
        create_validated_copy=False, require_footnote_semantic_review=True,
    )
    assert first["polish_footnote_reflow"]["chapter.md"]["rounds"] == 1
    assert first["human_review_required"] == []

    second = validate_markdown_subagent(
        tmp_path, "polish", source_dir, target_dir,
        create_validated_copy=False, require_footnote_semantic_review=True,
    )
    assert second["polish_footnote_reflow"]["chapter.md"]["rounds"] == 2
    assert second["human_review_required"] == []

    # Rebuilding the manifest must not reset the persisted round count.
    manifest_path.unlink()
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    third = validate_markdown_subagent(
        tmp_path, "polish", source_dir, target_dir,
        create_validated_copy=False, require_footnote_semantic_review=True,
    )
    assert third["polish_footnote_reflow"]["chapter.md"]["rounds"] == 3
    assert third["human_review_required_files"] == ["chapter.md"]
    escalated = third["human_review_required"][0]
    assert escalated["kind"] == "polish_footnote_reflow"
    assert escalated["action"] == "ask_human"
    assert "chapter.md" not in third["retry_required_files"]
    assert third["all_passed"] is False
