import json
from pathlib import Path
from datetime import datetime, timezone

import pytest

from pdf2epub.subagent_dispatch_recovery import record_failure, resume, snapshot, recovery_state_path
from pdf2epub.subagent_dispatch import claim_dispatch_lease, inspect_dispatch_assignment, build_assignment_sha256

DIGEST = "a" * 64


def fixture(tmp_path):
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"task": "translate", "model": "worker-model", "worker_handoffs": [{"worker_id": "worker_001", "assignment_sha256": DIGEST, "assigned_files": ["a.md"], "status": "pending"}]}), encoding="utf-8")
    return manifest


def test_retry_after_seconds_and_readonly(tmp_path):
    manifest = fixture(tmp_path)
    path = recovery_state_path(tmp_path, "translate")
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    event = record_failure(path, manifest, "worker_001", "rate_limit", expected_assignment_sha256=DIGEST, retry_after="120", actual_peak_concurrency=2, now=now)
    assert event["next_retry_at"] == "2026-01-01T00:02:00+00:00"
    before = set(tmp_path.iterdir())
    assert snapshot(path, "translate", now=now)["cooldown_active"]
    assert set(tmp_path.iterdir()) == before


def test_retry_after_preserves_microsecond_deadline(tmp_path):
    manifest = fixture(tmp_path)
    now = datetime(2026, 1, 1, 0, 0, 0, 900000, tzinfo=timezone.utc)
    event = record_failure(recovery_state_path(tmp_path, "translate"), manifest, "worker_001", "rate_limit", expected_assignment_sha256=DIGEST, retry_after="120", actual_peak_concurrency=1, now=now)
    assert event["next_retry_at"] == "2026-01-01T00:02:00.900000+00:00"


def test_quota_block_requires_explicit_resume_and_audit(tmp_path):
    manifest = fixture(tmp_path)
    path = recovery_state_path(tmp_path, "translate")
    record_failure(path, manifest, "worker_001", "quota_exhausted", expected_assignment_sha256=DIGEST, actual_peak_concurrency=1, now=datetime(2026, 1, 1, tzinfo=timezone.utc))
    assert snapshot(path, "translate")["task_block"]["operator_blocked"]
    with pytest.raises(ValueError):
        resume(path, "translate", reason="quota checked", confirm_sessions_reconciled=True, confirm_quota_available=False)
    result = resume(path, "translate", reason="quota reset verified", confirm_sessions_reconciled=True, confirm_quota_available=True)
    assert result["action"] == "operator_resume"


def test_completed_assignment_rejected(tmp_path):
    manifest = fixture(tmp_path)
    value = json.loads(manifest.read_text(encoding="utf-8"))
    value["worker_handoffs"][0]["handoff_status"] = "completed"
    manifest.write_text(json.dumps(value), encoding="utf-8")
    with pytest.raises(ValueError, match="completed"):
        record_failure(recovery_state_path(tmp_path, "translate"), manifest, "worker_001", "unknown_429", expected_assignment_sha256=DIGEST, actual_peak_concurrency=1)


def test_reused_worker_id_requires_current_digest_and_manifest_task(tmp_path):
    manifest = fixture(tmp_path)
    with pytest.raises(ValueError, match="does not match"):
        record_failure(recovery_state_path(tmp_path, "translate"), manifest, "worker_001", "unknown_429", expected_assignment_sha256="b" * 64, actual_peak_concurrency=1)
    other = tmp_path / "other.json"
    other.write_text(json.dumps({"task": "translate-html", "worker_handoffs": [{"worker_id": "worker_001", "assignment_sha256": DIGEST, "assigned_files": ["a.md"]}]}), encoding="utf-8")
    with pytest.raises(ValueError, match="task does not match"):
        record_failure(recovery_state_path(tmp_path, "translate"), other, "worker_001", "unknown_429", expected_assignment_sha256=DIGEST, actual_peak_concurrency=1)


def test_retry_after_http_date_and_invalid_state_fail_closed(tmp_path):
    manifest = fixture(tmp_path)
    path = recovery_state_path(tmp_path, "translate")
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    event = record_failure(path, manifest, "worker_001", "rate_limit", expected_assignment_sha256=DIGEST, retry_after="Thu, 01 Jan 2026 00:05:00 GMT", actual_peak_concurrency=1, now=now)
    assert event["next_retry_at"] == "2026-01-01T00:05:00+00:00"
    path.write_text('{"schema_version": 999}', encoding="utf-8")
    with pytest.raises(ValueError, match="schema"):
        snapshot(path, "translate", now=now)


def test_required_state_fields_quota_deadline_max_and_temporary_cap_only_decreases(tmp_path):
    manifest = fixture(tmp_path)
    path = recovery_state_path(tmp_path, "translate")
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    record_failure(path, manifest, "worker_001", "quota_exhausted", expected_assignment_sha256=DIGEST, reset_at="2026-01-01T00:10:00Z", retry_after="900", actual_peak_concurrency=2, temporary_cap=1, now=now)
    event = record_failure(path, manifest, "worker_001", "unknown_429", expected_assignment_sha256=DIGEST, actual_peak_concurrency=1, temporary_cap=3, now=now.replace(minute=1))
    assert event["task_next_retry_at"] == "2026-01-01T00:15:00+00:00"
    assert event["temporary_cap_effective"] == 1
    state = json.loads(path.read_text(encoding="utf-8"))
    del state["task_block"]
    path.write_text(json.dumps(state), encoding="utf-8")
    with pytest.raises(ValueError, match="schema"):
        snapshot(path, "translate", now=now)


def test_malformed_nested_recovery_state_fails_closed(tmp_path):
    manifest = fixture(tmp_path)
    path = recovery_state_path(tmp_path, "translate")
    record_failure(path, manifest, "worker_001", "rate_limit", expected_assignment_sha256=DIGEST, retry_after="20", actual_peak_concurrency=1)
    state = json.loads(path.read_text(encoding="utf-8"))
    state["history"][0]["observed_at"] = "not-a-time"
    path.write_text(json.dumps(state), encoding="utf-8")
    with pytest.raises(ValueError, match="history.observed_at"):
        snapshot(path, "translate")


def test_lock_symlink_escape_is_rejected(tmp_path):
    path = recovery_state_path(tmp_path, "translate")
    outside = tmp_path.parent / "outside-lock-target"
    outside.write_text("", encoding="utf-8")
    lock = Path(f"{path}.lock")
    try:
        lock.symlink_to(outside)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks are unavailable in this environment")
    with pytest.raises(ValueError, match="lock path escapes"):
        snapshot(path, "translate")


def test_task_deadline_is_monotonic_and_resume_waits(tmp_path):
    manifest = fixture(tmp_path)
    path = recovery_state_path(tmp_path, "translate")
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    record_failure(path, manifest, "worker_001", "rate_limit", expected_assignment_sha256=DIGEST, retry_after="120", actual_peak_concurrency=2, now=start)
    record_failure(path, manifest, "worker_001", "unknown_429", expected_assignment_sha256=DIGEST, actual_peak_concurrency=2, now=start.replace(second=1))
    state = snapshot(path, "translate", now=start)
    assert state["next_retry_at"] == "2026-01-01T00:02:00+00:00"
    with pytest.raises(ValueError, match="deadline"):
        resume(path, "translate", reason="checked sessions", confirm_sessions_reconciled=True, confirm_quota_available=False, now=start)


def test_retry_after_does_not_bypass_retry_limit(tmp_path):
    manifest = fixture(tmp_path)
    path = recovery_state_path(tmp_path, "translate")
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    for i in range(6):
        event = record_failure(path, manifest, "worker_001", "rate_limit", expected_assignment_sha256=DIGEST, retry_after="3600", actual_peak_concurrency=1, now=now)
        now = now.replace(minute=now.minute + 1)
    assert event["operator_blocked"]


def test_claim_and_inspect_gate_preserve_active_lease(tmp_path):
    manifest = fixture(tmp_path)
    assignment = build_assignment_sha256(task="translate", worker_id="worker_001", assigned_files=["a.md"])
    lease_path = tmp_path / "translate_dispatch_leases.json"
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    claimed = claim_dispatch_lease(lease_path, task="translate", assignment_sha256=assignment, worker_id="worker_001", assigned_files=["a.md"], owner_id="operator", now=now)
    assert claimed["status"] == "claimed"
    record_failure(recovery_state_path(tmp_path, "translate"), manifest, "worker_001", "rate_limit", expected_assignment_sha256=DIGEST, retry_after="120", actual_peak_concurrency=1, now=now)
    same = claim_dispatch_lease(lease_path, task="translate", assignment_sha256=assignment, worker_id="worker_001", assigned_files=["a.md"], owner_id="operator", now=now)
    assert same["status"] == "renewed"
    fresh = inspect_dispatch_assignment(lease_path, assignment_sha256="different", assigned_files=["b.md"], task="translate", now=now)
    assert fresh["status"] == "recovery_blocked"
    conflict = inspect_dispatch_assignment(lease_path, assignment_sha256="different", assigned_files=["a.md"], task="translate", now=now)
    assert conflict["status"] == "conflict"
    assert json.loads(lease_path.read_text(encoding="utf-8"))["leases"][0]["status"] == "running"


def test_recovery_status_does_not_change_assignment_or_model_and_caps_never_exceed_wave(tmp_path):
    manifest = fixture(tmp_path)
    before = manifest.read_bytes()
    uncapped = tmp_path / "uncapped"
    uncapped.mkdir()
    assert snapshot(recovery_state_path(uncapped, "translate"), "translate", configured_cap=4, available_cap=2, active_workers=2)["new_worker_slots"] == 2
    record_failure(recovery_state_path(tmp_path, "translate"), manifest, "worker_001", "unknown_429", expected_assignment_sha256=DIGEST, actual_peak_concurrency=2, temporary_cap=2)
    state = snapshot(recovery_state_path(tmp_path, "translate"), "translate", configured_cap=5, wave_cap=2, available_cap=0)
    assert state["effective_cap"] == 0
    assert snapshot(recovery_state_path(tmp_path, "translate"), "translate", configured_cap=5, wave_cap=1, available_cap=4)["effective_cap"] == 1
    assert snapshot(recovery_state_path(tmp_path, "translate"), "translate", configured_cap=4, wave_cap=4, available_cap=3, active_workers=1)["new_worker_slots"] == 1
    assert snapshot(recovery_state_path(tmp_path, "translate"), "translate", configured_cap=4, wave_cap=4, available_cap=2, active_workers=2)["new_worker_slots"] == 0
    assert snapshot(recovery_state_path(tmp_path, "translate"), "translate", configured_cap=4, wave_cap=1, available_cap=2, active_workers=1)["new_worker_slots"] == 0
    assert snapshot(recovery_state_path(tmp_path, "translate"), "translate", configured_cap=4, available_cap=2, active_workers=None)["new_worker_slots"] is None
    assert snapshot(recovery_state_path(tmp_path, "translate"), "translate", configured_cap=4, wave_cap=1, available_cap=3, active_workers=0)["new_worker_slots"] == 1
    assert snapshot(recovery_state_path(uncapped, "translate"), "translate", configured_cap=4, available_cap=2, active_workers=2)["new_worker_slots"] == 2
    assert manifest.read_bytes() == before


def test_manifest_path_escape_rejected(tmp_path):
    manifest = fixture(tmp_path)
    outside = tmp_path.parent / "outside-manifest.json"
    outside.write_text(manifest.read_text(encoding="utf-8"), encoding="utf-8")
    with pytest.raises(ValueError, match="inside the task output directory"):
        record_failure(recovery_state_path(tmp_path, "translate"), outside, "worker_001", "unknown_429", expected_assignment_sha256=DIGEST, actual_peak_concurrency=1)
