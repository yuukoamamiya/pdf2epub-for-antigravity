from datetime import datetime, timedelta, timezone
import json

import pytest

from pdf2epub.subagent_dispatch import (
    build_assignment_sha256,
    claim_dispatch_lease,
    inspect_dispatch_assignment,
    release_dispatch_lease,
    renew_dispatch_lease,
)


def _time(seconds: int = 0) -> datetime:
    return datetime(2026, 10, 7, 1, 0, 0, tzinfo=timezone.utc) + timedelta(
        seconds=seconds
    )


def _assignment(files: list[str], *, worker: str = "worker_001") -> str:
    return build_assignment_sha256(
        task="translate",
        worker_id=worker,
        assigned_files=files,
        batch_ids=["batch_001"],
        source_sha256={name: f"hash-{name}" for name in files},
        context_sha256={"unit:one": "context-hash"},
    )


def test_assignment_digest_is_order_independent_but_context_sensitive():
    first = build_assignment_sha256(
        task="translate",
        worker_id="worker_001",
        assigned_files=["b.md", "a.md"],
        batch_ids=["batch_001"],
        source_sha256={"a.md": "a", "b.md": "b"},
        context_sha256={"unit:a": "one"},
    )
    same = build_assignment_sha256(
        task="translate",
        worker_id="worker_001",
        assigned_files=["a.md", "b.md"],
        batch_ids=["batch_001"],
        source_sha256={"a.md": "a", "b.md": "b"},
        context_sha256={"unit:a": "one"},
    )
    changed = build_assignment_sha256(
        task="translate",
        worker_id="worker_001",
        assigned_files=["a.md", "b.md"],
        batch_ids=["batch_001"],
        source_sha256={"a.md": "a", "b.md": "b"},
        context_sha256={"unit:a": "changed"},
    )

    assert first == same
    assert first != changed


def test_dispatch_lease_prevents_duplicate_and_overlapping_claims(tmp_path):
    state = tmp_path / "translate_dispatch_leases.json"
    assignment = _assignment(["chapter_1.md"])

    claimed = claim_dispatch_lease(
        state,
        task="translate",
        assignment_sha256=assignment,
        worker_id="worker_001",
        assigned_files=["chapter_1.md"],
        owner_id="dispatcher-a",
        conversation_id="conversation-a",
        now=_time(),
    )
    duplicate = claim_dispatch_lease(
        state,
        task="translate",
        assignment_sha256=assignment,
        worker_id="worker_001",
        assigned_files=["chapter_1.md"],
        owner_id="dispatcher-b",
        now=_time(1),
    )
    overlap = claim_dispatch_lease(
        state,
        task="translate",
        assignment_sha256=_assignment(["chapter_1.md"], worker="worker_002"),
        worker_id="worker_002",
        assigned_files=["chapter_1.md", "chapter_2.md"],
        owner_id="dispatcher-b",
        now=_time(1),
    )

    assert claimed["status"] == "claimed"
    assert duplicate["status"] == "already_active"
    assert overlap["status"] == "conflict"
    assert inspect_dispatch_assignment(
        state,
        assignment_sha256=assignment,
        assigned_files=["chapter_1.md"],
        now=_time(1),
    )["status"] == "active_same_assignment"


def test_dispatch_lease_owner_can_renew_and_release(tmp_path):
    state = tmp_path / "leases.json"
    assignment = _assignment(["unit.md"])
    claim_dispatch_lease(
        state,
        task="translate",
        assignment_sha256=assignment,
        worker_id="worker_001",
        assigned_files=["unit.md"],
        owner_id="dispatcher-a",
        now=_time(),
    )

    renewed = renew_dispatch_lease(
        state,
        assignment_sha256=assignment,
        owner_id="dispatcher-a",
        lease_seconds=30,
        now=_time(5),
    )
    wrong_owner = release_dispatch_lease(
        state,
        assignment_sha256=assignment,
        owner_id="dispatcher-b",
        now=_time(6),
    )
    released = release_dispatch_lease(
        state,
        assignment_sha256=assignment,
        owner_id="dispatcher-a",
        now=_time(7),
    )

    assert renewed["status"] == "renewed"
    assert wrong_owner["status"] == "not_owner"
    assert released["status"] == "released"
    assert inspect_dispatch_assignment(
        state,
        assignment_sha256=assignment,
        assigned_files=["unit.md"],
        now=_time(8),
    )["status"] == "available"


def test_expired_lease_can_be_reclaimed(tmp_path):
    state = tmp_path / "leases.json"
    assignment = _assignment(["unit.md"])
    claim_dispatch_lease(
        state,
        task="translate",
        assignment_sha256=assignment,
        worker_id="worker_001",
        assigned_files=["unit.md"],
        owner_id="dispatcher-a",
        lease_seconds=5,
        now=_time(),
    )

    reclaimed = claim_dispatch_lease(
        state,
        task="translate",
        assignment_sha256=assignment,
        worker_id="worker_001",
        assigned_files=["unit.md"],
        owner_id="dispatcher-b",
        now=_time(6),
    )

    assert reclaimed["status"] == "claimed"
    records = json.loads(state.read_text(encoding="utf-8"))["leases"]
    assert any(record["status"] == "expired" for record in records)
    assert any(record["status"] == "claimed" for record in records)


def test_invalid_dispatch_state_is_not_silently_reset(tmp_path):
    state = tmp_path / "leases.json"
    state.write_text("not json", encoding="utf-8")

    with pytest.raises(ValueError, match="invalid dispatch lease registry"):
        inspect_dispatch_assignment(
            state,
            assignment_sha256="assignment",
            assigned_files=["unit.md"],
        )
