"""Coverage for pure review escalation decisions."""

from pdf2epub.markdown_validation_state import resolve_review_state


def test_first_review_signal_retries_instead_of_escalating():
    state = resolve_review_state(
        [{"file": "a.md", "kind": "bilingual_warnings"}],
        missing=[],
        invalid=[],
        escalations=[{
            "file": "a.md", "kind": "bilingual_warnings",
            "state": "retry", "retry_count": 1,
        }],
    )

    assert state.review_required_files == ("a.md",)
    assert state.human_review_required == ()
    assert state.retry_required_files == ("a.md",)
    assert state.retry_required[0]["action"] == "retry_subagent"


def test_persistent_review_signal_goes_to_human_without_retry():
    state = resolve_review_state(
        [{"file": "a.md", "kind": "bilingual_warnings"}],
        missing=[],
        invalid=[],
        escalations=[{
            "file": "a.md", "kind": "bilingual_warnings",
            "state": "human_review_required", "retry_count": 2,
        }],
    )

    assert state.human_review_required_files == ("a.md",)
    assert state.human_review_required[0]["action"] == "ask_human"
    assert state.retry_required == ()


def test_previous_file_with_different_kind_still_retries():
    state = resolve_review_state(
        [{"file": "a.md", "kind": "different_kind"}],
        missing=[],
        invalid=[],
        escalations=[{
            "file": "a.md", "kind": "bilingual_warnings",
            "state": "human_review_required", "retry_count": 2,
        }],
    )

    assert state.human_review_required == ()
    assert state.retry_required_files == ("a.md",)


def test_missing_and_invalid_outputs_are_retried():
    state = resolve_review_state(
        [],
        missing=["missing.md"],
        invalid=[
            {"file": "broken.md", "kind": "structural"},
            {
                "file": "context.md",
                "kind": "prepared_context",
                "action": "rebuild_context",
            },
        ],
        escalations=[],
    )

    kinds = {(item["file"], item["kind"]) for item in state.retry_required}
    assert ("missing.md", "missing_output") in kinds
    assert ("broken.md", "invalid_output") in kinds
    assert ("context.md", "prepared_context") in kinds
    assert state.retry_required_files == ("broken.md", "context.md", "missing.md")


def test_empty_inputs_produce_empty_state():
    state = resolve_review_state([], [], [], [])

    assert state.review_required_files == ()
    assert state.human_review_required_files == ()
    assert state.retry_required_files == ()


def test_escalation_receipt_survives_manifest_rebuild(tmp_path):
    """Deleting and rebuilding the manifest must not reset the retry budget."""
    from pdf2epub.markdown_subagent_validation import validate_markdown_subagent

    source_dir = tmp_path / "source"
    target_dir = tmp_path / "target"
    source_dir.mkdir()
    target_dir.mkdir()
    line = "This is a deliberately long English paragraph that remains unchanged in the output."
    (source_dir / "unit.md").write_text(f"{line}\n{line}\n", encoding="utf-8")
    (target_dir / "unit.md").write_text(f"{line}\n{line}\n", encoding="utf-8")

    def validate():
        return validate_markdown_subagent(
            tmp_path, "translate", source_dir, target_dir,
            target_language="Chinese", create_validated_copy=False,
        )

    first = validate()
    assert first["human_review_required"] == []
    assert first["review_escalation"][0]["retry_count"] == 1

    # Rebuild the hand-off manifest from scratch; the receipt must persist.
    (tmp_path / "translate_subagent_manifest.json").write_text(
        '{"task": "translate"}', encoding="utf-8"
    )
    second = validate()
    assert second["human_review_required_files"] == ["unit.md"]
    assert second["review_escalation"][0]["retry_count"] == 2
    assert second["review_escalation"][0]["state"] == "human_review_required"
    assert second["all_passed"] is False

    # One explicit human decision acknowledges and stages the reviewed file.
    acknowledged = validate_markdown_subagent(
        tmp_path, "translate", source_dir, target_dir,
        target_language="Chinese", create_validated_copy=False,
        allow_review_warnings=True, human_decision_id="human-a1",
    )
    assert acknowledged["human_review_required"] == []
    assert acknowledged["review_warnings_acknowledged"] is True
    assert acknowledged["human_decision_id"] == "human-a1"
    assert acknowledged["review_escalation"][0]["state"] == "resolved"


def test_allow_review_warnings_requires_human_decision_id(tmp_path):
    import pytest
    from pdf2epub.markdown_subagent_validation import validate_markdown_subagent

    source_dir = tmp_path / "source"
    target_dir = tmp_path / "target"
    source_dir.mkdir()
    target_dir.mkdir()
    (source_dir / "unit.md").write_text("Body.\n", encoding="utf-8")
    (target_dir / "unit.md").write_text("Body.\n", encoding="utf-8")

    with pytest.raises(ValueError, match="human-decision-id"):
        validate_markdown_subagent(
            tmp_path, "polish", source_dir, target_dir,
            allow_review_warnings=True,
        )


def test_partial_validation_consolidates_with_full_validation_and_never_normalizes_rejected_targets(tmp_path):
    from pdf2epub.markdown_subagent_validation import validate_markdown_subagent

    source_dir = tmp_path / "source"
    target_dir = tmp_path / "target"
    source_dir.mkdir()
    target_dir.mkdir()
    line = "This is a deliberately long English paragraph that remains unchanged in the output."
    (source_dir / "unit.md").write_bytes(f"{line}\n{line}\n".encode("utf-8"))
    fenced_target = f"```markdown\n{line}\n{line}\n```\n"
    (target_dir / "unit.md").write_bytes(fenced_target.encode("utf-8"))

    partial = validate_markdown_subagent(
        tmp_path, "translate", source_dir, target_dir,
        selected_files=["unit.md"], target_language="Chinese", create_validated_copy=True,
    )
    assert partial["review_escalation"][0]["retry_count"] == 1
    assert partial["review_escalation"][0]["state"] == "retry"
    # Rejected target must preserve its un-normalized fenced bytes in quarantine.
    quarantined = tmp_path / partial["quarantined_files"]["unit.md"]
    assert quarantined.read_text(encoding="utf-8") == fenced_target
    (target_dir / "unit.md").write_bytes(quarantined.read_bytes())

    consolidated = validate_markdown_subagent(
        tmp_path, "translate", source_dir, target_dir,
        target_language="Chinese", create_validated_copy=True,
    )
    assert consolidated["review_escalation"][0]["retry_count"] == 1
    assert consolidated["review_escalation"][0]["state"] == "retry"
    assert consolidated["human_review_required"] == []
    quarantined2 = tmp_path / consolidated["quarantined_files"]["unit.md"]
    (target_dir / "unit.md").write_bytes(quarantined2.read_bytes())

    second_full = validate_markdown_subagent(
        tmp_path, "translate", source_dir, target_dir,
        target_language="Chinese", create_validated_copy=True,
    )
    assert second_full["review_escalation"][0]["retry_count"] == 2
    assert second_full["review_escalation"][0]["state"] == "human_review_required"
    assert second_full["human_review_required_files"] == ["unit.md"]


def test_quarantine_invalid_targets_replaces_existing_identical_digest_file(tmp_path):
    from pdf2epub.markdown_validation_checkpoint import quarantine_invalid_targets

    target_dir = tmp_path / "target"
    target_dir.mkdir()
    for _ in range(2):
        (target_dir / "unit.md").write_bytes(b"bad output\n")
        moved = quarantine_invalid_targets(tmp_path, "translate", target_dir, ["unit.md"])
        assert "unit.md" in moved
        assert not (target_dir / "unit.md").exists()
