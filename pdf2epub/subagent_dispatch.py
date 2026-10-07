"""Atomic, file-backed dispatch leases for workspace Subagent handoffs.

The Markdown handoff files describe *what* should be processed.  This module
tracks the short-lived claim made by a dispatcher before opening a worker.  It
is deliberately independent from the IDE: a lease is an advisory coordination
record, not proof that a conversation is alive.  Callers must still reconcile
it with the actual workspace Subagent list.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator, Mapping, Optional

from .workflow_contracts import atomic_write_text


DISPATCH_LEASE_SCHEMA_VERSION = 1
DEFAULT_DISPATCH_LEASE_SECONDS = 15 * 60
MAX_DISPATCH_LEASE_HISTORY = 256
_ACTIVE_STATUSES = frozenset({"claimed", "running", "assigned"})
_SAFE_FILE_RE = re.compile(r"^[^/\\]+$")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="seconds")


def _parse_time(value: Any) -> Optional[datetime]:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _normalise_files(files: Any) -> list[str]:
    if not isinstance(files, (list, tuple, set)):
        raise ValueError("assigned_files must be a list of filenames")
    result = []
    for raw_name in files:
        name = str(raw_name).strip()
        if not name or not _SAFE_FILE_RE.fullmatch(name):
            raise ValueError(f"invalid assigned filename: {name!r}")
        result.append(name)
    return sorted(set(result))


def build_assignment_sha256(
    *,
    task: str,
    worker_id: str,
    assigned_files: list[str] | tuple[str, ...] | set[str],
    batch_ids: list[Any] | tuple[Any, ...] = (),
    source_sha256: Optional[Mapping[str, Any]] = None,
    context_sha256: Optional[Mapping[str, Any]] = None,
) -> str:
    """Build the stable identity of one worker assignment."""
    files = _normalise_files(assigned_files)
    source = source_sha256 or {}
    context = context_sha256 or {}
    payload = {
        "task": str(task).strip(),
        "worker_id": str(worker_id).strip(),
        "assigned_files": files,
        "batch_ids": [str(value) for value in batch_ids],
        "source_sha256": {
            name: str(source[name])
            for name in files
            if name in source
        },
        "context_sha256": {
            str(name): str(value)
            for name, value in sorted(context.items(), key=lambda item: str(item[0]))
        },
    }
    return hashlib.sha256(
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def dispatch_lease_path(output_dir: Path, task: str) -> Path:
    """Return the per-task lease registry path without creating it."""
    safe_task = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(task).strip()).strip("_")
    if not safe_task:
        raise ValueError("task must not be empty")
    return Path(output_dir) / f"{safe_task}_dispatch_leases.json"


@contextmanager
def _state_lock(state_path: Path) -> Iterator[None]:
    """Serialize lease mutations on Windows and POSIX hosts."""
    lock_path = Path(f"{state_path}.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+b") as handle:
        if handle.tell() == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            if os.name == "nt":
                import msvcrt

                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _empty_state() -> dict[str, Any]:
    return {"schema_version": DISPATCH_LEASE_SCHEMA_VERSION, "leases": []}


def _load_state(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return _empty_state()
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid dispatch lease registry: {exc}") from exc
    if not isinstance(value, dict) or value.get("schema_version") != DISPATCH_LEASE_SCHEMA_VERSION:
        raise ValueError("unsupported dispatch lease registry schema")
    leases = value.get("leases")
    if not isinstance(leases, list):
        raise ValueError("dispatch lease registry leases must be a list")
    return {"schema_version": DISPATCH_LEASE_SCHEMA_VERSION, "leases": leases}


def _write_state(path: Path, state: Mapping[str, Any]) -> None:
    atomic_write_text(path, json.dumps(dict(state), ensure_ascii=False, indent=2))


def _is_active(lease: Mapping[str, Any], now: datetime) -> bool:
    if str(lease.get("status") or "") not in _ACTIVE_STATUSES:
        return False
    expires = _parse_time(lease.get("lease_expires_at"))
    return expires is not None and expires > now


def _expire_and_prune(leases: list[Any], now: datetime) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for raw in leases:
        if not isinstance(raw, Mapping):
            continue
        lease = dict(raw)
        if str(lease.get("status") or "") in _ACTIVE_STATUSES:
            expires = _parse_time(lease.get("lease_expires_at"))
            if expires is None or expires <= now:
                lease["status"] = "expired"
                lease["expired_at"] = _iso(now)
        result.append(lease)
    if len(result) <= MAX_DISPATCH_LEASE_HISTORY:
        return result
    active = [lease for lease in result if _is_active(lease, now)]
    inactive = [lease for lease in result if not _is_active(lease, now)]
    return active + inactive[-MAX_DISPATCH_LEASE_HISTORY:]


def _lease_result(status: str, lease: Optional[Mapping[str, Any]] = None, **extra: Any) -> dict[str, Any]:
    result: dict[str, Any] = {"status": status}
    if lease is not None:
        result["lease"] = dict(lease)
    result.update(extra)
    return result


def inspect_dispatch_assignment(
    state_path: Path,
    *,
    assignment_sha256: str,
    assigned_files: list[str] | tuple[str, ...] | set[str],
    now: Optional[datetime] = None,
) -> dict[str, Any]:
    """Report whether an assignment is available, active, or conflicting."""
    files = set(_normalise_files(assigned_files))
    current_time = now or _now()
    state_path = Path(state_path)
    with _state_lock(state_path):
        state = _load_state(state_path)
        leases = _expire_and_prune(state["leases"], current_time)
        active = [lease for lease in leases if _is_active(lease, current_time)]
        same = [
            lease
            for lease in active
            if lease.get("assignment_sha256") == assignment_sha256
        ]
        conflicts = [
            lease
            for lease in active
            if lease.get("assignment_sha256") != assignment_sha256
            and files.intersection(set(lease.get("assigned_files") or []))
        ]
        if leases != state["leases"]:
            state["leases"] = leases
            _write_state(state_path, state)
    if same:
        return _lease_result("active_same_assignment", same[0])
    if conflicts:
        return _lease_result("conflict", conflicts=conflicts)
    return _lease_result("available")


def claim_dispatch_lease(
    state_path: Path,
    *,
    task: str,
    assignment_sha256: str,
    worker_id: str,
    assigned_files: list[str] | tuple[str, ...] | set[str],
    owner_id: str,
    conversation_id: Optional[str] = None,
    lease_seconds: int = DEFAULT_DISPATCH_LEASE_SECONDS,
    now: Optional[datetime] = None,
) -> dict[str, Any]:
    """Atomically claim an assignment or return a duplicate/conflict result."""
    files = _normalise_files(assigned_files)
    if not str(owner_id).strip():
        raise ValueError("owner_id must not be empty")
    if int(lease_seconds) <= 0:
        raise ValueError("lease_seconds must be positive")
    current_time = now or _now()
    state_path = Path(state_path)
    with _state_lock(state_path):
        state = _load_state(state_path)
        leases = _expire_and_prune(state["leases"], current_time)
        active = [lease for lease in leases if _is_active(lease, current_time)]
        same = next(
            (
                lease
                for lease in active
                if lease.get("assignment_sha256") == assignment_sha256
            ),
            None,
        )
        if same is not None:
            if same.get("owner_id") != owner_id:
                state["leases"] = leases
                _write_state(state_path, state)
                return _lease_result("already_active", same)
            same["lease_expires_at"] = _iso(
                current_time + timedelta(seconds=int(lease_seconds))
            )
            if conversation_id:
                same["conversation_id"] = str(conversation_id)
            same["status"] = "running"
            state["leases"] = leases
            _write_state(state_path, state)
            return _lease_result("renewed", same)

        conflicts = [
            lease
            for lease in active
            if set(files).intersection(set(lease.get("assigned_files") or []))
        ]
        if conflicts:
            state["leases"] = leases
            _write_state(state_path, state)
            return _lease_result("conflict", conflicts=conflicts)

        lease = {
            "task": str(task).strip(),
            "assignment_sha256": str(assignment_sha256).strip(),
            "worker_id": str(worker_id).strip(),
            "assigned_files": files,
            "owner_id": str(owner_id).strip(),
            "status": "claimed",
            "claimed_at": _iso(current_time),
            "lease_expires_at": _iso(
                current_time + timedelta(seconds=int(lease_seconds))
            ),
        }
        if conversation_id:
            lease["conversation_id"] = str(conversation_id)
        leases.append(lease)
        state["leases"] = _expire_and_prune(leases, current_time)
        _write_state(state_path, state)
        return _lease_result("claimed", lease)


def renew_dispatch_lease(
    state_path: Path,
    *,
    assignment_sha256: str,
    owner_id: str,
    lease_seconds: int = DEFAULT_DISPATCH_LEASE_SECONDS,
    conversation_id: Optional[str] = None,
    now: Optional[datetime] = None,
) -> dict[str, Any]:
    """Extend a live lease owned by the current dispatcher."""
    if int(lease_seconds) <= 0:
        raise ValueError("lease_seconds must be positive")
    current_time = now or _now()
    state_path = Path(state_path)
    with _state_lock(state_path):
        state = _load_state(state_path)
        leases = _expire_and_prune(state["leases"], current_time)
        for lease in leases:
            if lease.get("assignment_sha256") != assignment_sha256:
                continue
            if lease.get("owner_id") != owner_id:
                return _lease_result("not_owner", lease)
            if not _is_active(lease, current_time):
                state["leases"] = leases
                _write_state(state_path, state)
                return _lease_result("expired", lease)
            lease["lease_expires_at"] = _iso(
                current_time + timedelta(seconds=int(lease_seconds))
            )
            lease["status"] = "running"
            if conversation_id:
                lease["conversation_id"] = str(conversation_id)
            state["leases"] = leases
            _write_state(state_path, state)
            return _lease_result("renewed", lease)
        state["leases"] = leases
        _write_state(state_path, state)
        return _lease_result("not_found")


def release_dispatch_lease(
    state_path: Path,
    *,
    assignment_sha256: str,
    owner_id: str,
    now: Optional[datetime] = None,
) -> dict[str, Any]:
    """Release a lease owned by the current dispatcher."""
    current_time = now or _now()
    state_path = Path(state_path)
    with _state_lock(state_path):
        state = _load_state(state_path)
        leases = _expire_and_prune(state["leases"], current_time)
        for lease in leases:
            if lease.get("assignment_sha256") != assignment_sha256:
                continue
            if lease.get("owner_id") != owner_id:
                return _lease_result("not_owner", lease)
            lease["status"] = "released"
            lease["released_at"] = _iso(current_time)
            state["leases"] = leases
            _write_state(state_path, state)
            return _lease_result("released", lease)
        state["leases"] = leases
        _write_state(state_path, state)
        return _lease_result("not_found")


__all__ = [
    "DEFAULT_DISPATCH_LEASE_SECONDS",
    "DISPATCH_LEASE_SCHEMA_VERSION",
    "build_assignment_sha256",
    "claim_dispatch_lease",
    "dispatch_lease_path",
    "inspect_dispatch_assignment",
    "release_dispatch_lease",
    "renew_dispatch_lease",
]
