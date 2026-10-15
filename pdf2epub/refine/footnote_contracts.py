"""Decision-role constants shared by footnote validation and preparation."""

from __future__ import annotations


FOOTNOTE_PREPARE_SCHEMA_VERSION = 1


DECISION_ROLES = frozenset(
    {
        "body",
        "citation",
        "bibliography",
        "footnote_start",
        "footnote_continuation",
        "footnote_definition",
        "review_required",
    }
)
MOVED_ROLES = frozenset(
    {"footnote_start", "footnote_continuation", "footnote_definition"}
)


__all__ = [
    "DECISION_ROLES",
    "FOOTNOTE_PREPARE_SCHEMA_VERSION",
    "MOVED_ROLES",
]
