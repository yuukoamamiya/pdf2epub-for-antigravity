"""Auditable operator recovery state for workspace Subagent dispatch."""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone, timedelta
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Mapping

from .state_lock import state_lock as _state_lock
from .workflow_contracts import atomic_write_json

SCHEMA = 1
HISTORY_LIMIT = 256
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
RETRY_LIMIT = 5
BACKOFF_CAP_SECONDS = 3600


def _now(now: datetime | None = None) -> datetime:
    value = now or datetime.now(timezone.utc)
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    return value.astimezone(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def _read(path: Path, task: str) -> dict[str, Any]:
    if not path.exists():
        return {"schema_version": SCHEMA, "task": task, "history": [], "workers": {}, "task_block": None, "temporary_cap": None, "actual_peak_concurrency": 0, "failure_attempts": 0}
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid dispatch recovery state: {exc}") from exc
    required = {"schema_version", "task", "history", "workers", "task_block", "temporary_cap", "actual_peak_concurrency", "failure_attempts"}
    if not isinstance(state, dict) or not required.issubset(state) or state.get("schema_version") != SCHEMA or state.get("task") != task or not isinstance(state.get("history"), list) or len(state["history"]) > HISTORY_LIMIT or not isinstance(state.get("workers"), dict):
        raise ValueError("unsupported or invalid dispatch recovery state schema")
    if type(state.get("actual_peak_concurrency", 0)) is not int or state.get("actual_peak_concurrency", 0) < 0:
        raise ValueError("invalid actual_peak_concurrency in recovery state")
    if type(state.get("failure_attempts", 0)) is not int or state.get("failure_attempts", 0) < 0:
        raise ValueError("invalid failure_attempts in recovery state")
    cap = state.get("temporary_cap")
    if cap is not None and (type(cap) is not int or not 1 <= cap <= 6):
        raise ValueError("invalid temporary_cap in recovery state")
    block = state.get("task_block")
    if block is not None:
        if not isinstance(block, dict) or block.get("kind") not in {"rate_limit", "quota_exhausted", "unknown_429"} or type(block.get("operator_blocked")) is not bool:
            raise ValueError("invalid task_block in recovery state")
        if block.get("next_retry_at") is not None:
            _aware_iso(block["next_retry_at"], "task_block.next_retry_at")
    for worker, item in state["workers"].items():
        if not isinstance(worker, str) or not isinstance(item, dict) or not isinstance(item.get("assignment_sha256"), str) or not _SHA256_RE.fullmatch(item["assignment_sha256"]) or type(item.get("attempts")) is not int or item["attempts"] < 1 or item.get("kind") not in {"rate_limit", "quota_exhausted", "unknown_429"} or type(item.get("operator_blocked")) is not bool:
            raise ValueError("invalid worker entry in recovery state")
        if item.get("next_retry_at") is not None:
            _aware_iso(item["next_retry_at"], f"workers.{worker}.next_retry_at")
        if not isinstance(item.get("assigned_files"), list) or not all(isinstance(name, str) for name in item["assigned_files"]):
            raise ValueError("invalid assigned_files in recovery worker entry")
    for item in state["history"]:
        if not isinstance(item, dict):
            raise ValueError("invalid recovery history entry")
        if item.get("action") == "operator_resume":
            if not isinstance(item.get("reason"), str) or not item["reason"].strip() or type(item.get("sessions_reconciled")) is not bool or type(item.get("quota_available")) is not bool:
                raise ValueError("invalid operator resume history entry")
        else:
            if not isinstance(item.get("worker_id"), str) or not _SHA256_RE.fullmatch(str(item.get("assignment_sha256") or "")) or item.get("kind") not in {"rate_limit", "quota_exhausted", "unknown_429"} or type(item.get("attempt")) is not int or item["attempt"] < 1 or type(item.get("operator_blocked")) is not bool:
                raise ValueError("invalid provider failure history entry")
        _aware_iso(item.get("observed_at"), "history.observed_at")
        if item.get("next_retry_at") is not None:
            _aware_iso(item["next_retry_at"], "history.next_retry_at")
    return state


def _aware_iso(value: Any, label: str) -> datetime:
    if not isinstance(value, str):
        raise ValueError(f"{label} must be timezone-aware ISO-8601")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{label} must be timezone-aware ISO-8601") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"{label} must include a timezone")
    try:
        return parsed.astimezone(timezone.utc)
    except OverflowError as exc:
        raise ValueError(f"{label} is outside the supported datetime range") from exc


def _confined(path: Path, root: Path, label: str) -> Path:
    root = root.resolve()
    resolved = path.resolve()
    if not resolved.is_relative_to(root):
        raise ValueError(f"{label} must stay inside the task output directory")
    for lock in (Path(f"{path}.lock"), Path(f"{resolved}.lock")):
        if not lock.resolve().is_relative_to(root):
            raise ValueError(f"{label} lock path escapes the task output directory")
    return resolved


def recovery_state_path(output_dir: Path, task: str) -> Path:
    if not task or any(c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.-" for c in task):
        raise ValueError("task must contain only letters, digits, dot, underscore, or hyphen")
    return Path(output_dir) / f"{task}_dispatch_recovery.json"


def _validate_state_path(path: Path, task: str) -> Path:
    root = Path(path).parent
    resolved = _confined(Path(path), root, "recovery state")
    if resolved.name != recovery_state_path(root, task).name:
        raise ValueError("recovery state filename does not match task")
    return resolved


def snapshot(path: Path, task: str, *, now: datetime | None = None, configured_cap: int = 6, wave_cap: int | None = None, available_cap: int | None = None, active_workers: int | None = None) -> dict[str, Any]:
    """Pure read; does not create output or lock files, nor expire leases."""
    for name, value in (("configured_cap", configured_cap), ("wave_cap", wave_cap)):
        if value is not None and (type(value) is not int or not 1 <= value <= 6):
            raise ValueError(f"{name} must be between 1 and 6")
    if available_cap is not None and (type(available_cap) is not int or not 0 <= available_cap <= 6):
        raise ValueError("available_cap must be between 0 and 6")
    if active_workers is not None and (type(active_workers) is not int or not 0 <= active_workers <= 6):
        raise ValueError("active_workers must be between 0 and 6")
    state = _read(_validate_state_path(Path(path), task), task)
    current = _now(now)
    result = dict(state)
    block = state.get("task_block")
    result["next_retry_at"] = block.get("next_retry_at") if isinstance(block, dict) else None
    result["cooldown_active"] = bool(result["next_retry_at"] and _aware_iso(result["next_retry_at"], "next_retry_at") > current)
    caps = [configured_cap, wave_cap or configured_cap, result.get("temporary_cap") or configured_cap]
    if available_cap is not None:
        caps.append(available_cap)
    result["concurrency_cap"] = min(configured_cap, wave_cap or configured_cap, result.get("temporary_cap") or configured_cap)
    result["effective_cap"] = min(caps)
    result["new_worker_slots"] = (
        0 if available_cap == 0
        else None if available_cap is None or active_workers is None
        else max(0, min(available_cap, result["concurrency_cap"] - active_workers))
    )
    result["cap_inputs"] = {"configured_cap": configured_cap, "wave_cap": wave_cap, "temporary_cap": result.get("temporary_cap"), "available_cap": available_cap, "active_workers": active_workers}
    return result


def _retry_after_deadline(now: datetime, header: str) -> datetime:
    try:
        seconds = int(header.strip())
        if seconds < 0:
            raise ValueError("Retry-After seconds must not be negative")
        try:
            return now + timedelta(seconds=seconds)
        except OverflowError as exc:
            raise ValueError("Retry-After is outside the supported datetime range") from exc
    except ValueError as seconds_error:
        if str(seconds_error).startswith("Retry-After"):
            raise
        try:
            parsed = parsedate_to_datetime(header)
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError("Retry-After must be nonnegative seconds or a valid HTTP date") from exc
        if parsed is None or parsed.tzinfo is None:
            raise ValueError("Retry-After HTTP date must include timezone")
        try:
            return parsed.astimezone(timezone.utc)
        except OverflowError as exc:
            raise ValueError("Retry-After date is outside the supported datetime range") from exc


def _retry_at(now: datetime, kind: str, attempt: int, retry_after: str | None, reset_at: str | None) -> tuple[str | None, bool]:
    if kind == "quota_exhausted":
        if not reset_at:
            return None, True
        parsed = _aware_iso(reset_at, "reset-at")
        latest = parsed
        if retry_after:
            latest = max(latest, _retry_after_deadline(now, retry_after))
        return _iso(max(now, latest)), attempt > RETRY_LIMIT
    if attempt > RETRY_LIMIT:
        if retry_after:
            deadline = _retry_after_deadline(now, retry_after)
            return _iso(max(now, deadline)), True
        return None, True
    if retry_after:
        return _iso(max(now, _retry_after_deadline(now, retry_after))), False
    return _iso(now + timedelta(seconds=min(30 * (2 ** (attempt - 1)), BACKOFF_CAP_SECONDS))), False


def _find_assignment(manifest_path: Path, worker_id: str) -> tuple[str, str, list[str], Mapping[str, Any]]:
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read parent manifest: {exc}") from exc
    if not isinstance(manifest, dict) or not isinstance(manifest.get("worker_handoffs"), list):
        raise ValueError("parent manifest has no worker_handoffs")
    found = [h for h in manifest["worker_handoffs"] if isinstance(h, dict) and h.get("worker_id") == worker_id]
    if len(found) != 1:
        raise ValueError("worker_id must identify exactly one current parent handoff")
    h = found[0]
    if h.get("handoff_status") in {"completed", "superseded"} or h.get("status") in {"completed", "superseded"}:
        raise ValueError("cannot record failure for completed or superseded assignment")
    digest = str(h.get("assignment_sha256") or "")
    if not _SHA256_RE.fullmatch(digest):
        raise ValueError("parent handoff has invalid assignment_sha256")
    files = h.get("assigned_files")
    if not isinstance(files, list) or not all(isinstance(name, str) for name in files):
        raise ValueError("current handoff assigned_files must be a list of filenames")
    task = manifest.get("task") or "translate"
    if not isinstance(task, str) or not task.strip():
        raise ValueError("parent manifest task must be a nonempty string")
    return task.strip(), digest, files, h


def record_failure(path: Path, manifest_path: Path, worker_id: str, kind: str, *, expected_assignment_sha256: str, retry_after: str | None = None, reset_at: str | None = None, actual_peak_concurrency: int, temporary_cap: int | None = None, now: datetime | None = None) -> dict[str, Any]:
    if kind not in {"rate_limit", "quota_exhausted", "unknown_429"}:
        raise ValueError("kind must be rate_limit, quota_exhausted, or unknown_429")
    if not isinstance(worker_id, str) or not worker_id.strip() or (retry_after is not None and not isinstance(retry_after, str)) or (reset_at is not None and not isinstance(reset_at, str)):
        raise ValueError("worker_id, retry_after, and reset_at have invalid types")
    if type(actual_peak_concurrency) is not int or actual_peak_concurrency < 0:
        raise ValueError("actual peak concurrency must be a nonnegative integer")
    if temporary_cap is not None and (type(temporary_cap) is not int or temporary_cap < 1 or temporary_cap > 6):
        raise ValueError("temporary cap must be between 1 and 6")
    path = Path(path)
    manifest_path = _confined(Path(manifest_path), path.parent, "parent manifest")
    path = _confined(path, path.parent, "recovery state")
    task, digest, files, _ = _find_assignment(manifest_path, worker_id)
    if not _SHA256_RE.fullmatch(expected_assignment_sha256) or expected_assignment_sha256 != digest:
        raise ValueError("assignment digest does not match the current parent handoff")
    if path.name != recovery_state_path(path.parent, task).name:
        raise ValueError("state path task does not match parent manifest task")
    current = _now(now)
    path = Path(path)
    with _state_lock(path):
        state = _read(path, task)
        prior = state["workers"].get(worker_id, {})
        attempt = int(state.get("failure_attempts", 0)) + 1
        next_at, blocked = _retry_at(current, kind, attempt, retry_after, reset_at)
        state["workers"][worker_id] = {"assignment_sha256": digest, "assigned_files": files, "attempts": attempt, "kind": kind, "next_retry_at": next_at, "operator_blocked": blocked}
        state["failure_attempts"] = attempt
        old_block = state.get("task_block")
        if isinstance(old_block, dict):
            old_deadline = old_block.get("next_retry_at")
            old_active = old_deadline and _aware_iso(old_deadline, "prior next retry") > current
            if old_active and next_at:
                next_at = _iso(max(_aware_iso(old_deadline, "prior next retry"), _aware_iso(next_at, "next retry")))
            elif old_active:
                next_at = old_deadline
            old_quota = old_block.get("kind") == "quota_exhausted"
            preserve_permanent = old_block.get("operator_blocked") and not old_deadline
            if old_quota or preserve_permanent:
                blocked = bool(old_block.get("operator_blocked") or blocked)
                kind = "quota_exhausted" if old_quota or kind == "quota_exhausted" else old_block.get("kind")
                if old_block.get("operator_blocked") and not old_deadline:
                    next_at = None
        state["task_block"] = {"kind": kind, "next_retry_at": next_at, "operator_blocked": blocked}
        state["actual_peak_concurrency"] = max(int(state.get("actual_peak_concurrency", 0)), actual_peak_concurrency)
        cap_before = state.get("temporary_cap")
        if temporary_cap is not None:
            state["temporary_cap"] = min(cap_before, temporary_cap) if cap_before is not None else temporary_cap
        event = {"worker_id": worker_id, "assignment_sha256": digest, "kind": kind, "attempt": attempt, "observed_at": _iso(current), "retry_after": retry_after, "reset_at": reset_at, "worker_next_retry_at": state["workers"][worker_id]["next_retry_at"], "next_retry_at": next_at, "task_next_retry_at": state["task_block"]["next_retry_at"], "operator_blocked": blocked, "actual_peak_concurrency": actual_peak_concurrency, "temporary_cap_before": cap_before, "temporary_cap_requested": temporary_cap, "temporary_cap_effective": state["temporary_cap"]}
        state["history"] = (state["history"] + [event])[-HISTORY_LIMIT:]
        atomic_write_json(path, state)
    return event


def resume(path: Path, task: str, *, reason: str, confirm_sessions_reconciled: bool, confirm_quota_available: bool, now: datetime | None = None) -> dict[str, Any]:
    if not isinstance(reason, str) or not reason.strip() or not confirm_sessions_reconciled:
        raise ValueError("resume requires a reason and confirmation that actual sessions were reconciled")
    current = _now(now)
    path = _validate_state_path(Path(path), task)
    with _state_lock(path):
        state = _read(path, task)
        block = state.get("task_block")
        if block is None and state.get("temporary_cap") is None:
            raise ValueError("there is no recovery block or temporary cap to resume")
        if block and block.get("next_retry_at") and _aware_iso(block["next_retry_at"], "next_retry_at") > current:
            raise ValueError("provider Retry-After/reset deadline has not elapsed; early resume is not allowed")
        if block and block.get("kind") == "quota_exhausted" and not confirm_quota_available:
            raise ValueError("quota resume requires explicit confirmation that quota is available")
        state["task_block"] = None
        cap_before = state.get("temporary_cap")
        cap_cleared = cap_before is not None
        state["temporary_cap"] = None
        state["failure_attempts"] = 0
        entry = {"action": "operator_resume", "reason": reason.strip(), "sessions_reconciled": True, "quota_available": bool(confirm_quota_available), "temporary_cap_before": cap_before, "temporary_cap_cleared": cap_cleared, "observed_at": _iso(current)}
        state["history"] = (state["history"] + [entry])[-HISTORY_LIMIT:]
        atomic_write_json(path, state)
    return entry


__all__ = ["SCHEMA", "recovery_state_path", "snapshot", "record_failure", "resume"]
