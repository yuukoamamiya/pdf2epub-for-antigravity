"""Pure Markdown review escalation and validation summary decisions."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Mapping

from .review_escalation import STATE_HUMAN


@dataclass(frozen=True)
class ReviewState:
    review_required_files: tuple[str, ...]
    human_review_required: tuple[dict[str, Any], ...]
    retry_required: tuple[dict[str, Any], ...]
    human_review_required_files: tuple[str, ...]
    retry_required_files: tuple[str, ...]


def resolve_review_state(
    review_required: Iterable[Mapping[str, Any]],
    missing: Iterable[str],
    invalid: Iterable[Mapping[str, Any]],
    escalations: Iterable[Mapping[str, Any]] = (),
) -> ReviewState:
    """Decide which review signals still retry and which need a human.

    Escalation state comes from the persisted review-escalation receipt, not
    from the hand-off manifest, so rebuilding the manifest cannot reset the
    retry budget.  An absent signal retries; a signal whose persisted state is
    ``human_review_required`` stops automatic retries.
    """
    review = [dict(item) for item in review_required]
    human_signals = {
        (str(item.get("file") or ""), str(item.get("kind") or ""))
        for item in escalations
        if isinstance(item, Mapping) and item.get("state") == STATE_HUMAN
    }
    review_files = tuple(sorted({str(i["file"]) for i in review if i.get("file")}))
    human = tuple(
        {
            **item,
            "action": "ask_human",
            "reason": (
                "the same review signal persisted after a Subagent retry; "
                "automatic retries are paused"
            ),
        }
        for item in review
        if (str(item.get("file") or ""), str(item.get("kind") or "")) in human_signals
    )
    human_keys = {(str(i.get("file") or ""), str(i.get("kind") or "")) for i in human}
    retry = [
        {"file": name, "kind": "missing_output", "action": "retry_subagent",
         "reason": "Subagent did not write the assigned target file"}
        for name in missing
    ]
    for item in invalid:
        value = dict(item)
        if value.get("kind") in {"prepared_context", "polish_footnote_semantics"} and value.get("action"):
            retry.append(value)
        else:
            retry.append({**value, "kind": "invalid_output", "action": "retry_subagent"})
    retry.extend(
        {**item, "action": "retry_subagent"}
        for item in review
        if (str(item.get("file") or ""), str(item.get("kind") or "")) not in human_keys
    )
    retry_files = tuple(sorted({str(i["file"]) for i in retry if i.get("file")}))
    human_files = tuple(sorted({str(i["file"]) for i in human if i.get("file")}))
    return ReviewState(review_files, human, tuple(retry), human_files, retry_files)
