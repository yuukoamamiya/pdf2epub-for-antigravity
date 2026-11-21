"""Decision-role constants shared by footnote validation and preparation."""

from __future__ import annotations


FOOTNOTE_PREPARE_SCHEMA_VERSION = 4
FOOTNOTE_UNIT_CONTEXT_PROJECTION_VERSION = 1
# Zero-candidate shortcut receipt policy.  Only a schema *major* change may
# invalidate it; a minor bump forward-rolls without another Subagent review.
FOOTNOTE_SHORTCUT_POLICY_VERSION = 1


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
    "FOOTNOTE_SHORTCUT_POLICY_VERSION",
    "FOOTNOTE_UNIT_CONTEXT_PROJECTION_VERSION",
    "MOVED_ROLES",
]
