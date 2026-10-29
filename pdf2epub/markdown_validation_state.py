"""Pure Markdown review escalation and validation summary decisions."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Mapping


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
    previous_review_files: Iterable[str],
    previous_review_keys: Iterable[tuple[str, str]],
) -> ReviewState:
    review = [dict(item) for item in review_required]
    prev_files, prev_keys = set(previous_review_files), set(previous_review_keys)
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
        if str(item.get("file") or "") in prev_files
        and (
            not prev_keys
            or (str(item.get("file") or ""), str(item.get("kind") or "")) in prev_keys
        )
    )
    human_keys = {(str(i.get("file") or ""), str(i.get("kind") or "")) for i in human}
    retry = [
        {"file": name, "kind": "missing_output", "action": "retry_subagent",
         "reason": "Subagent did not write the assigned target file"}
        for name in missing
    ]
    retry.extend({**dict(item), "kind": "invalid_output", "action": "retry_subagent"} for item in invalid)
    retry.extend(
        {**item, "action": "retry_subagent"}
        for item in review
        if (str(item.get("file") or ""), str(item.get("kind") or "")) not in human_keys
    )
    retry_files = tuple(sorted({str(i["file"]) for i in retry if i.get("file")}))
    human_files = tuple(sorted({str(i["file"]) for i in human if i.get("file")}))
    return ReviewState(review_files, human, tuple(retry), human_files, retry_files)
