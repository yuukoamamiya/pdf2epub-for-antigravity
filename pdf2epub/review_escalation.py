"""Durable review escalation state shared by every review gate.

A review signal that survives one Subagent retry escalates to a human. The
counter lives in ``review_escalation.json`` inside the book workspace instead of
the hand-off manifest, so deleting and rebuilding a manifest cannot silently
restart the retry budget. Every gate that turns ``review_required`` into
``human_review_required`` records its signals here, and every explicit human
adjudication (acknowledging warnings or authorizing one more retry) is written
to the same receipt with its ``human_decision_id``.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

from .workflow_contracts import atomic_write_json

SCHEMA_VERSION = 1
FILENAME = "review_escalation.json"
STATE_RETRY = "retry"
STATE_HUMAN = "human_review_required"
STATE_RESOLVED = "resolved"
DEFAULT_ESCALATE_AFTER = 2


@dataclass(frozen=True)
class ReviewEscalation:
    """One persisted review signal and its escalation position."""

    signal_id: str
    task: str
    file: str
    kind: str
    first_seen_hash: str
    retry_count: int
    state: str
    last_seen_hash: str = ""
    escalate_after: int = DEFAULT_ESCALATE_AFTER
    human_decision_id: str | None = None
    detail: dict[str, Any] = field(default_factory=dict)

    @property
    def is_human_required(self) -> bool:
        return self.state == STATE_HUMAN

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _timestamp() -> str:
    return datetime.now(timezone.utc).isoformat()


def _text(value: Any) -> str:
    return str(value or "").strip()


def signal_id_for(task: str, filename: str, kind: str) -> str:
    """Return the stable identifier for one task/file/signal kind."""
    identity = "\0".join((_text(task), _text(filename), _text(kind)))
    return "res-" + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:20]


def _signal_key(task: str, filename: str, kind: str) -> str:
    return "\0".join((_text(task), _text(filename), _text(kind)))


def _path(output_dir: Path) -> Path:
    return Path(output_dir) / FILENAME


def _read_payload(output_dir: Path) -> dict[str, Any]:
    path = _path(output_dir)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        payload = {}
    if not isinstance(payload, Mapping):
        payload = {}
    signals = payload.get("signals")
    decisions = payload.get("human_decisions")
    return {
        "schema_version": SCHEMA_VERSION,
        "signals": {
            str(key): dict(value)
            for key, value in (signals.items() if isinstance(signals, Mapping) else [])
            if isinstance(value, Mapping)
        },
        "human_decisions": [
            dict(item) for item in (decisions if isinstance(decisions, list) else [])
            if isinstance(item, Mapping)
        ],
    }


def _write_payload(output_dir: Path, payload: Mapping[str, Any]) -> None:
    atomic_write_json(_path(output_dir), dict(payload))


def _coerce(task: str, filename: str, kind: str, raw: Mapping[str, Any]) -> ReviewEscalation:
    try:
        retry_count = int(raw.get("retry_count") or 0)
    except (TypeError, ValueError):
        retry_count = 0
    try:
        escalate_after = max(1, int(raw.get("escalate_after") or DEFAULT_ESCALATE_AFTER))
    except (TypeError, ValueError):
        escalate_after = DEFAULT_ESCALATE_AFTER
    state = _text(raw.get("state")) or STATE_RETRY
    return ReviewEscalation(
        signal_id=_text(raw.get("signal_id")) or signal_id_for(task, filename, kind),
        task=_text(raw.get("task")) or _text(task),
        file=_text(raw.get("file")) or _text(filename),
        kind=_text(raw.get("kind")) or _text(kind),
        first_seen_hash=_text(raw.get("first_seen_hash")),
        retry_count=retry_count,
        state=state,
        last_seen_hash=_text(raw.get("last_seen_hash")),
        escalate_after=escalate_after,
        human_decision_id=(_text(raw.get("human_decision_id")) or None),
        detail=dict(raw.get("detail")) if isinstance(raw.get("detail"), Mapping) else {},
    )


def load_review_escalations(output_dir: Path) -> dict[tuple[str, str, str], ReviewEscalation]:
    """Load every persisted signal keyed by ``(task, file, kind)``."""
    payload = _read_payload(output_dir)
    result: dict[tuple[str, str, str], ReviewEscalation] = {}
    for raw in payload["signals"].values():
        task = _text(raw.get("task"))
        filename = _text(raw.get("file"))
        kind = _text(raw.get("kind"))
        if not task or not filename or not kind:
            continue
        result[(task, filename, kind)] = _coerce(task, filename, kind, raw)
    return result


def escalation_for(
    output_dir: Path,
    *,
    task: str,
    file: str,
    kind: str,
) -> ReviewEscalation | None:
    """Read one signal without mutating the receipt."""
    raw = _read_payload(output_dir)["signals"].get(_signal_key(task, file, kind))
    if not isinstance(raw, Mapping):
        return None
    return _coerce(task, file, kind, raw)


def _record_decision(
    payload: dict[str, Any],
    *,
    decision_id: str,
    action: str,
    signals: Iterable[ReviewEscalation],
) -> None:
    payload["human_decisions"].append({
        "human_decision_id": decision_id,
        "action": action,
        "signals": [
            {"signal_id": item.signal_id, "task": item.task, "file": item.file, "kind": item.kind}
            for item in signals
        ],
        "created_at": _timestamp(),
    })


def record_review_signals(
    output_dir: Path,
    *,
    task: str,
    signals: Iterable[Mapping[str, Any]],
    scope_files: Iterable[str] | None = None,
    human_decision_id: str | None = None,
    acknowledge: bool = False,
    escalate_after: int = DEFAULT_ESCALATE_AFTER,
    increment_same_hash: bool = True,
) -> dict[tuple[str, str, str], ReviewEscalation]:
    """Record the current review signals and persist their escalation state.

    ``acknowledge`` resolves the signals with an explicit ``human_decision_id``
    (used by ``--allow-review-warnings``). Without acknowledgment a signal that
    reaches ``escalate_after`` occurrences becomes ``human_review_required``.
    ``scope_files`` marks unobserved signals for those files as resolved so a
    later recurrence starts a fresh budget.
    """
    decision_id = _text(human_decision_id)
    if acknowledge and not decision_id:
        raise ValueError("acknowledging review warnings requires an explicit human_decision_id")
    payload = _read_payload(output_dir)
    seen: set[tuple[str, str, str]] = set()
    recorded: dict[tuple[str, str, str], ReviewEscalation] = {}
    for raw in signals:
        if not isinstance(raw, Mapping):
            continue
        filename = _text(raw.get("file"))
        kind = _text(raw.get("kind"))
        if not filename or not kind:
            continue
        key = _signal_key(task, filename, kind)
        seen.add(key)
        evidence_hash = _text(raw.get("evidence_hash"))
        detail = dict(raw.get("detail")) if isinstance(raw.get("detail"), Mapping) else {}
        existing_raw = payload["signals"].get(key)
        existing = _coerce(task, filename, kind, existing_raw) if isinstance(existing_raw, Mapping) else None
        if existing is None or existing.state == STATE_RESOLVED:
            entry = ReviewEscalation(
                signal_id=signal_id_for(task, filename, kind),
                task=_text(task),
                file=filename,
                kind=kind,
                first_seen_hash=evidence_hash,
                retry_count=1,
                state=STATE_RETRY,
                last_seen_hash=evidence_hash,
                escalate_after=escalate_after,
                detail=detail,
            )
        else:
            same_evidence = existing.last_seen_hash == evidence_hash and bool(evidence_hash)
            retry_count = existing.retry_count if (same_evidence and not increment_same_hash) else existing.retry_count + 1
            state = existing.state
            if state != STATE_HUMAN and retry_count >= max(1, escalate_after):
                state = STATE_HUMAN
            entry = replace(
                existing,
                retry_count=retry_count,
                state=state,
                last_seen_hash=evidence_hash or existing.last_seen_hash,
                escalate_after=escalate_after,
                detail=detail or existing.detail,
            )
        if acknowledge:
            entry = replace(entry, state=STATE_RESOLVED, human_decision_id=decision_id)
        payload["signals"][key] = entry.as_dict()
        recorded[(_text(task), filename, kind)] = entry
    if acknowledge:
        _record_decision(
            payload,
            decision_id=decision_id,
            action="allow_review_warnings",
            signals=[entry for entry in recorded.values()],
        )
    if scope_files is not None:
        for filename in {_text(name) for name in scope_files if _text(name)}:
            for key, raw in list(payload["signals"].items()):
                signal = _coerce(task, filename, "", raw)
                if signal.file != filename or signal.task != _text(task) or key in seen:
                    continue
                if signal.state != STATE_RESOLVED:
                    payload["signals"][key] = replace(signal, state=STATE_RESOLVED).as_dict()
    _write_payload(output_dir, payload)
    return recorded


def authorize_retry(
    output_dir: Path,
    *,
    task: str,
    files: Iterable[str],
    human_decision_id: str,
) -> list[ReviewEscalation]:
    """Reset escalated signals after one explicit human retry decision."""
    decision_id = _text(human_decision_id)
    if not decision_id:
        raise ValueError("authorizing another retry requires an explicit human_decision_id")
    payload = _read_payload(output_dir)
    authorized: list[ReviewEscalation] = []
    target_files = {_text(name) for name in files if _text(name)}
    for key, raw in list(payload["signals"].items()):
        signal = _coerce(task, _text(raw.get("file")), _text(raw.get("kind")), raw)
        if signal.task != _text(task) or signal.file not in target_files:
            continue
        updated = replace(
            signal,
            retry_count=0,
            state=STATE_RETRY,
            human_decision_id=decision_id,
            # The next observation is the first authorized retry even when it
            # reports the same evidence as the human-reviewed signal.
            last_seen_hash="",
        )
        payload["signals"][key] = updated.as_dict()
        authorized.append(updated)
    if authorized:
        _record_decision(
            payload,
            decision_id=decision_id,
            action="retry_after_human_review",
            signals=authorized,
        )
    _write_payload(output_dir, payload)
    return authorized


def resolve_signals(
    output_dir: Path,
    *,
    task: str,
    signals: Iterable[Mapping[str, Any]],
) -> None:
    """Mark converged signals resolved so a recurrence starts a fresh budget."""
    payload = _read_payload(output_dir)
    changed = False
    for raw in signals:
        if not isinstance(raw, Mapping):
            continue
        key = _signal_key(_text(task), _text(raw.get("file")), _text(raw.get("kind")))
        existing_raw = payload["signals"].get(key)
        if not isinstance(existing_raw, Mapping):
            continue
        signal = _coerce(task, _text(raw.get("file")), _text(raw.get("kind")), existing_raw)
        if signal.state == STATE_RESOLVED:
            continue
        payload["signals"][key] = replace(signal, state=STATE_RESOLVED).as_dict()
        changed = True
    if changed:
        _write_payload(output_dir, payload)


def record_footnote_adjudications(
    output_dir: Path,
    *,
    decisions: Iterable[Mapping[str, Any]],
    effective_decisions_version: Any,
    file: str = "footnote_decisions.json",
) -> dict[str, Any]:
    """Persist footnote override adjudications in the shared receipt.

    High-confidence role/key/marker changes require ``override``; recording the
    exact candidate identity here keeps that adjudication auditable alongside
    every other review escalation instead of only inside the throw-away
    decision checkpoint.
    """
    payload = _read_payload(output_dir)
    recorded: list[dict[str, Any]] = []
    for decision in decisions:
        if not isinstance(decision, Mapping) or decision.get("override") is not True:
            continue
        evidence_id = _text(decision.get("candidate_evidence_id"))
        address = ":".join(str(decision.get(key) or "") for key in ("page", "source", "block"))
        kind = f"override:{address or evidence_id or 'unknown'}"
        key = _signal_key("footnote", file, kind)
        entry = ReviewEscalation(
            signal_id=signal_id_for("footnote", file, kind),
            task="footnote",
            file=_text(file),
            kind=kind,
            first_seen_hash=evidence_id,
            retry_count=0,
            state=STATE_RESOLVED,
            last_seen_hash=evidence_id,
            human_decision_id=None,
        )
        payload["signals"][key] = entry.as_dict()
        recorded.append({
            "signal_id": entry.signal_id,
            "kind": kind,
            "candidate_evidence_id": evidence_id or None,
            "decision_source": _text(decision.get("decision_source")) or None,
            "effective_decisions_version": effective_decisions_version,
        })
    payload["footnote_effective_decisions_version"] = effective_decisions_version
    _write_payload(output_dir, payload)
    return {
        "effective_decisions_version": effective_decisions_version,
        "overrides": recorded,
    }


__all__ = [
    "DEFAULT_ESCALATE_AFTER",
    "FILENAME",
    "ReviewEscalation",
    "SCHEMA_VERSION",
    "STATE_HUMAN",
    "STATE_RESOLVED",
    "STATE_RETRY",
    "authorize_retry",
    "escalation_for",
    "load_review_escalations",
    "record_footnote_adjudications",
    "record_review_signals",
    "resolve_signals",
    "signal_id_for",
]
