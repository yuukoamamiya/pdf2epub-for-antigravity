"""Coverage for pure review escalation decisions."""

from pdf2epub.markdown_validation_state import resolve_review_state


def test_first_review_signal_retries_instead_of_escalating():
    state = resolve_review_state(
        [{"file": "a.md", "kind": "bilingual_warnings"}],
        missing=[],
        invalid=[],
        previous_review_files=[],
        previous_review_keys=[],
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
        previous_review_files=["a.md"],
        previous_review_keys=[("a.md", "bilingual_warnings")],
    )

    assert state.human_review_required_files == ("a.md",)
    assert state.human_review_required[0]["action"] == "ask_human"
    assert state.retry_required == ()


def test_previous_file_with_different_kind_still_retries():
    state = resolve_review_state(
        [{"file": "a.md", "kind": "different_kind"}],
        missing=[],
        invalid=[],
        previous_review_files=["a.md"],
        previous_review_keys=[("a.md", "bilingual_warnings")],
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
        previous_review_files=[],
        previous_review_keys=[],
    )

    kinds = {(item["file"], item["kind"]) for item in state.retry_required}
    assert ("missing.md", "missing_output") in kinds
    assert ("broken.md", "invalid_output") in kinds
    assert ("context.md", "prepared_context") in kinds
    assert state.retry_required_files == ("broken.md", "context.md", "missing.md")


def test_empty_inputs_produce_empty_state():
    state = resolve_review_state([], [], [], [], [])

    assert state.review_required_files == ()
    assert state.human_review_required_files == ()
    assert state.retry_required_files == ()
