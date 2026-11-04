"""Fail-closed contracts and storage for opt-in terminology memory.

The module validates evidence and reviewer records; it never chooses or
approves terminology. Runtime integration is intentionally left to callers.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping

from pdf2epub.glossary import _normalize_match_text
from pdf2epub.subagent_dispatch import build_assignment_sha256
from pdf2epub.workflow_contracts import atomic_write_json


def _canonical(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _sha(value: Any) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _file_sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _nonempty(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _hash(value: Any) -> bool:
    return isinstance(value, str) and len(value) == 64 and all(c in "0123456789abcdef" for c in value)


@dataclass(frozen=True)
class MemoryAuthority:
    """Authoritative live workspace facts required for prepare and publish."""

    workspace: Path
    source_dir: Path
    target_dir: Path
    assignments: Mapping[str, Mapping[str, Any]]
    book_id: str
    source_language: str
    target_language: str
    chapter_map: Mapping[str, str]
    unit_inventory: Mapping[str, Mapping[str, Any]]
    book_entities_path: Path | None = None
    book_entities_sha256: str | None = None

    def __post_init__(self) -> None:
        workspace = Path(self.workspace).resolve()
        for label, directory in (("source_dir", self.source_dir), ("target_dir", self.target_dir)):
            resolved = Path(directory).resolve()
            if not resolved.is_relative_to(workspace):
                raise ValueError(f"{label} must be inside the authorized workspace")
        if not self.assignments:
            raise ValueError("at least one authoritative assignment is required")
        for digest, assignment in self.assignments.items():
            if not _hash(digest) or not isinstance(assignment, Mapping):
                raise ValueError("assignments must map SHA-256 ids to assignment contracts")
            expected = build_assignment_sha256(
                task=assignment.get("task", ""), worker_id=assignment.get("worker_id", ""),
                assigned_files=assignment.get("assigned_files", ()),
                batch_ids=assignment.get("batch_ids", ()),
                source_sha256=assignment.get("source_sha256", {}),
                context_sha256=assignment.get("context_sha256", {}),
            )
            if digest != expected:
                raise ValueError("assignment key does not match scoped assignment_sha256 contract")
        if self.book_entities_path is None:
            if self.book_entities_sha256 is not None:
                raise ValueError("explicit no-book-entities state must not carry a file hash")
        else:
            entity_path = Path(self.book_entities_path).resolve()
            if not entity_path.is_relative_to(workspace) or not entity_path.is_file() or not _hash(self.book_entities_sha256):
                raise ValueError("book entity file must be a live workspace file with declared SHA-256")
        if not _nonempty(self.book_id) or not _nonempty(self.source_language) or not _nonempty(self.target_language):
            raise ValueError("book identity and language pair must be non-empty")

    @property
    def authority_sha256(self) -> str:
        return _sha({
            "assignments": self.assignments,
            "book_id": self.book_id,
            "language_pair": [self.source_language, self.target_language],
            "unit_inventory": self.unit_inventory,
            "chapter_map": self.chapter_map,
        })

    def file_path(self, base: Path, relative: str) -> Path:
        rel = Path(relative)
        if rel.is_absolute() or ".." in rel.parts:
            raise ValueError(f"file path escapes workspace: {relative}")
        base_resolved = Path(base).resolve()
        candidate = (base_resolved / rel).resolve()
        if not candidate.is_relative_to(base_resolved) or not candidate.is_file():
            raise ValueError(f"file is outside workspace or missing: {relative}")
        return candidate

    def assignment_for(self, digest: str, name: str) -> bool:
        assignment = self.assignments.get(digest)
        return bool(assignment and name in assignment.get("assigned_files", ()))

    def current_book_entities(self) -> list[dict[str, Any]]:
        if self.book_entities_path is None:
            return []
        path = Path(self.book_entities_path).resolve()
        if not path.is_relative_to(Path(self.workspace).resolve()):
            raise ValueError("book entity path escapes authorized workspace")
        raw = path.read_bytes()
        if hashlib.sha256(raw).hexdigest() != self.book_entities_sha256:
            raise ValueError("book entity file changed since authority was established")
        data = json.loads(raw.decode("utf-8"))
        if not isinstance(data, dict):
            raise ValueError("book entity file must contain an object")
        result = []
        for category in ("characters", "places", "organizations", "terms", "races", "items"):
            values = data.get(category, [])
            if not isinstance(values, list):
                raise ValueError(f"book entity collection {category} must be an array")
            result.extend(values)
        return result


def _scope_valid(scope: Any, authority: MemoryAuthority, source_file: str) -> bool:
    if not isinstance(scope, dict):
        return False
    kind, identifier = scope.get("kind"), scope.get("id")
    if kind == "book":
        return identifier == authority.book_id
    if kind == "chapter":
        return _nonempty(identifier) and authority.chapter_map.get(source_file) == identifier
    return False


def _evidence_errors(item: Mapping[str, Any], authority: MemoryAuthority, path: str) -> list[str]:
    errors: list[str] = []
    source_file, target_file = item.get("source_file"), item.get("target_file")
    assignment_sha = item.get("assignment_sha256")
    if not _nonempty(source_file) or not authority.assignment_for(str(assignment_sha), str(source_file)):
        errors.append(f"{path}.source_file must belong to its referenced assignment")
        return errors
    if target_file != source_file:
        errors.append(f"{path}.target_file must match source_file under the 1:1 unit mapping")
        return errors
    if not _scope_valid(item.get("scope"), authority, str(source_file)):
        errors.append(f"{path}.scope must match the book or source file chapter")
    try:
        source_path = authority.file_path(authority.source_dir, str(source_file))
        target_path = authority.file_path(authority.target_dir, str(target_file))
        source_bytes, target_bytes = source_path.read_bytes(), target_path.read_bytes()
        source_text = source_bytes.decode("utf-8")
        target_text = target_bytes.decode("utf-8")
    except (OSError, UnicodeError, ValueError) as exc:
        errors.append(f"{path} evidence files are unavailable: {exc}")
        return errors
    if item.get("source_sha256") != hashlib.sha256(source_bytes).hexdigest():
        errors.append(f"{path}.source_sha256 is stale")
    assignment = authority.assignments.get(str(assignment_sha), {})
    assigned_source_hashes = assignment.get("source_sha256", {})
    if isinstance(assigned_source_hashes, Mapping) and source_file in assigned_source_hashes:
        if assigned_source_hashes[source_file] != hashlib.sha256(source_bytes).hexdigest():
            errors.append(f"{path} source hash no longer matches its scoped assignment")
    if item.get("target_sha256") != hashlib.sha256(target_bytes).hexdigest():
        errors.append(f"{path}.target_sha256 is stale")
    source, target = item.get("source"), item.get("target")
    source_evidence, target_evidence = item.get("source_evidence"), item.get("target_evidence")
    if not all(_nonempty(v) for v in (source, target, source_evidence, target_evidence)):
        errors.append(f"{path} source, target, and evidence must be non-empty")
        return errors
    if source not in source_text or source_evidence not in source_text or source not in source_evidence:
        errors.append(f"{path} source evidence must occur in current source and contain source term")
    if target not in target_text or target_evidence not in target_text or target not in target_evidence:
        errors.append(f"{path} target evidence must occur in current target and contain target term")
    return errors


def validate_candidates(candidates: Any, authority: MemoryAuthority) -> list[str]:
    errors: list[str] = []
    if not isinstance(candidates, list):
        return ["candidates must be an array"]
    seen: set[str] = set()
    for i, item in enumerate(candidates):
        path = f"candidates[{i}]"
        if not isinstance(item, dict):
            errors.append(f"{path} must be an object")
            continue
        for key in ("candidate_id", "source", "target"):
            if not _nonempty(item.get(key)):
                errors.append(f"{path}.{key} must be a non-empty string")
        if item.get("language_pair") != {"source": authority.source_language, "target": authority.target_language}:
            errors.append(f"{path}.language_pair does not match configured language pair")
        cid = item.get("candidate_id")
        if _nonempty(cid):
            if cid in seen:
                errors.append(f"{path}.candidate_id is duplicated")
            seen.add(cid)
        errors.extend(_evidence_errors(item, authority, path))
    return errors


def _source_forms(entry: Mapping[str, Any]) -> list[str]:
    values = [entry.get("source"), entry.get("original")]
    for key in ("aliases", "variants"):
        forms = entry.get(key, [])
        if isinstance(forms, list):
            values.extend(forms)
    return [v for v in values if _nonempty(v)]


def _scope_overlap(left: Mapping[str, Any], right: Mapping[str, Any]) -> bool:
    return left == right or left.get("kind") == "book" or right.get("kind") == "book"


def _check_entry_conflicts(entries: list[Mapping[str, Any]]) -> None:
    ids: dict[str, Mapping[str, Any]] = {}
    forms: dict[str, list[Mapping[str, Any]]] = {}
    for entry in entries:
        cid = entry.get("candidate_id", entry.get("id"))
        if cid in ids and ids[cid] != entry:
            raise ValueError(f"duplicate terminology id conflict: {cid}")
        ids[str(cid)] = entry
        for form in _source_forms(entry):
            key = _normalize_match_text(form)
            for previous in forms.get(key, []):
                if _scope_overlap(previous.get("scope", {}), entry.get("scope", {})) and previous.get("target", previous.get("suggested_translation")) != entry.get("target", entry.get("suggested_translation")):
                    raise ValueError(f"terminology form conflict for {form!r} across overlapping scopes")
            forms.setdefault(key, []).append(entry)


def _check_book_entity_conflicts(candidates: list[Mapping[str, Any]], book_entities: Iterable[Mapping[str, Any]]) -> None:
    static = list(book_entities)
    for candidate in candidates:
        for entity in static:
            if not isinstance(entity, Mapping):
                raise ValueError("book entity must be an object")
            common = {_normalize_match_text(x) for x in _source_forms(candidate)} & {_normalize_match_text(x) for x in _source_forms(entity)}
            if common and candidate.get("target", candidate.get("suggested_translation")) != entity.get("target", entity.get("suggested_translation")):
                raise ValueError(f"candidate {candidate.get('candidate_id')} conflicts with book entity; review required")


def _validate_affected(affected: list[dict[str, Any]], authority: MemoryAuthority) -> list[str]:
    errors: list[str] = []
    seen: set[str] = set()
    for i, item in enumerate(affected):
        if not isinstance(item, dict):
            errors.append(f"affected_targets[{i}] must be an object")
            continue
        uid = item.get("unit_id")
        if not _nonempty(uid) or uid in seen:
            errors.append(f"affected_targets[{i}].unit_id must be unique and non-empty")
        seen.add(str(uid))
        inventory = authority.unit_inventory.get(str(uid))
        if not isinstance(inventory, Mapping):
            errors.append(f"affected_targets[{i}] is not in authoritative book unit inventory")
            continue
        for key in ("source_file", "target_file", "scope", "source_sha256", "target_sha256", "effective_projection_sha256"):
            if item.get(key) != inventory.get(key):
                errors.append(f"affected_targets[{i}].{key} does not match authoritative unit inventory")
        if item.get("source_file") != item.get("target_file"):
            errors.append(f"affected_targets[{i}] source and target unit mapping must be 1:1")
            continue
        try:
            source_path = authority.file_path(authority.source_dir, item["source_file"])
            target_path = authority.file_path(authority.target_dir, item["target_file"])
            source_bytes, target_bytes = source_path.read_bytes(), target_path.read_bytes()
        except (KeyError, OSError, TypeError, ValueError) as exc:
            errors.append(f"affected_targets[{i}] files are unavailable: {exc}")
            continue
        if hashlib.sha256(source_bytes).hexdigest() != item.get("source_sha256"):
            errors.append(f"affected_targets[{i}].source_sha256 is stale")
        if hashlib.sha256(target_bytes).hexdigest() != item.get("target_sha256"):
            errors.append(f"affected_targets[{i}].target_sha256 is stale")
        if not _hash(item.get("effective_projection_sha256")):
            errors.append(f"affected_targets[{i}].effective_projection_sha256 must be SHA-256")
        chapter = authority.chapter_map.get(item.get("source_file"))
        if item.get("chapter_id") != chapter or not _scope_valid(item.get("scope"), authority, item.get("source_file", "")):
            errors.append(f"affected_targets[{i}] chapter or scope does not match authoritative mapping")
    return errors


def _packet_payload(packet: Mapping[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in packet.items() if k != "prepared_packet_sha256"}


def prepare_memory(
    root: Path,
    candidates: list[dict[str, Any]],
    *,
    authority: MemoryAuthority,
    affected_targets: Iterable[Mapping[str, Any]] = (),
    book_entities: Iterable[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    """Build a packet from live assigned source/target evidence and authority."""
    errors = validate_candidates(candidates, authority)
    affected = [dict(row) for row in affected_targets]
    errors.extend(_validate_affected(affected, authority))
    if errors:
        raise ValueError("; ".join(errors))
    entities = authority.current_book_entities()
    supplied_entities = [dict(row) for row in book_entities]
    if supplied_entities and supplied_entities != entities:
        raise ValueError("supplied book entities do not match authoritative read-only entity file")
    _check_book_entity_conflicts(candidates, entities)
    payload = {
        "schema_version": 1,
        "book_id": authority.book_id,
        "language_pair": {"source": authority.source_language, "target": authority.target_language},
        "authority_sha256": authority.authority_sha256,
        "book_entities_state": {"source": "none"} if authority.book_entities_path is None else {"source": "file", "sha256": authority.book_entities_sha256},
        "parent_snapshot_sha256": _current_parent(root),
        "candidates": candidates,
        "candidate_set_sha256": _sha(candidates),
        "affected_targets": affected,
        "affected_targets_sha256": _sha(affected),
        "book_entities_sha256": _sha(entities),
        "book_entities": entities,
    }
    packet = {**payload, "prepared_packet_sha256": _sha(payload)}
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    atomic_write_json(root / "review_packet.json", packet)
    return packet


def _current_parent(root: Path) -> str | None:
    _assert_memory_store_confined(root)
    path = Path(root) / "current.json"
    if not path.exists():
        return None
    pointer = json.loads(path.read_text(encoding="utf-8"))
    digest = pointer.get("snapshot_sha256") if isinstance(pointer, dict) else None
    if not _hash(digest):
        raise ValueError("current snapshot pointer contains invalid digest")
    expected = f"snapshots/{digest}.json"
    if pointer.get("path") != expected:
        raise ValueError("current snapshot pointer path is invalid")
    resolved_root = Path(root).resolve()
    snapshots_lexical = resolved_root / "snapshots"
    snapshots_root = snapshots_lexical.resolve()
    if not snapshots_root.is_relative_to(resolved_root):
        raise ValueError("snapshots directory escapes memory root")
    pointer_target = (resolved_root / pointer["path"]).resolve()
    if not pointer_target.is_relative_to(snapshots_root):
        raise ValueError("current snapshot pointer escapes memory root")
    return digest


def _assert_memory_store_confined(root: Path) -> tuple[Path, Path]:
    """Ensure snapshot storage resolves under the selected memory root."""
    root_resolved = Path(root).resolve()
    snapshots_lexical = root_resolved / "snapshots"
    snapshots_root = snapshots_lexical.resolve()
    if not snapshots_root.is_relative_to(root_resolved):
        raise ValueError("snapshots directory escapes memory root")
    return root_resolved, snapshots_root


def _validate_packet(packet: Any, authority: MemoryAuthority) -> list[str]:
    if not isinstance(packet, Mapping):
        return ["prepared packet must be an object"]
    errors: list[str] = []
    if packet.get("schema_version") != 1 or packet.get("book_id") != authority.book_id:
        errors.append("packet schema or book identity mismatch")
    if packet.get("language_pair") != {"source": authority.source_language, "target": authority.target_language}:
        errors.append("packet language pair mismatch")
    if packet.get("authority_sha256") != authority.authority_sha256:
        errors.append("packet multi-assignment authority no longer matches")
    if not isinstance(packet.get("book_entities"), list) or packet.get("book_entities_sha256") != _sha(packet.get("book_entities")):
        errors.append("book entity inventory content hash mismatch")
    elif isinstance(packet.get("candidates"), list):
        try:
            _check_book_entity_conflicts(packet["candidates"], packet["book_entities"])
        except ValueError as exc:
            errors.append(str(exc))
    try:
        live_entities = authority.current_book_entities()
        live_state = {"source": "none"} if authority.book_entities_path is None else {"source": "file", "sha256": authority.book_entities_sha256}
        if packet.get("book_entities") != live_entities or packet.get("book_entities_state") != live_state:
            errors.append("book entity source or live content changed since prepare")
    except (OSError, UnicodeError, ValueError, json.JSONDecodeError) as exc:
        errors.append(f"book entity live binding failed: {exc}")
    payload = _packet_payload(packet)
    if packet.get("prepared_packet_sha256") != _sha(payload):
        errors.append("prepared packet content hash mismatch")
    candidates = packet.get("candidates")
    errors.extend(validate_candidates(candidates, authority))
    if packet.get("candidate_set_sha256") != _sha(candidates):
        errors.append("candidate set content hash mismatch")
    affected = packet.get("affected_targets")
    if not isinstance(affected, list):
        errors.append("affected_targets must be an array")
    else:
        errors.extend(_validate_affected(affected, authority))
        if packet.get("affected_targets_sha256") != _sha(affected):
            errors.append("affected target inventory content hash mismatch")
    return errors


def _validate_review(packet: Mapping[str, Any], review: Any) -> list[str]:
    errors: list[str] = []
    if not isinstance(review, dict):
        return ["review must be an object"]
    if review.get("prepared_packet_sha256") != packet.get("prepared_packet_sha256"):
        errors.append("review is not bound to prepared packet content")
    expected = {r["candidate_id"] for r in packet["candidates"]}
    rows = review.get("candidates")
    if not isinstance(rows, list):
        errors.append("review.candidates must be an array")
    else:
        if any(not isinstance(r, dict) for r in rows):
            errors.append("each candidate review must be an object")
        ids = [r.get("candidate_id") for r in rows if isinstance(r, dict)]
        if any(not _nonempty(cid) for cid in ids) or set(ids) != expected or len(ids) != len(rows) or len(set(ids)) != len(ids):
            errors.append("review must cover every candidate exactly once")
        if any(not isinstance(r.get("decision"), str) or r.get("decision") not in {"approve", "reject"} for r in rows if isinstance(r, dict)):
            errors.append("candidate decisions must be approve or reject")
    expected_targets = {r["unit_id"] for r in packet["affected_targets"]}
    targets = review.get("affected_targets")
    if not isinstance(targets, list):
        errors.append("review.affected_targets must be an array")
    else:
        if any(not isinstance(r, dict) for r in targets):
            errors.append("each affected target review must be an object")
        ids = [r.get("unit_id") for r in targets if isinstance(r, dict)]
        if any(not _nonempty(uid) for uid in ids) or set(ids) != expected_targets or len(ids) != len(targets) or len(set(ids)) != len(ids):
            errors.append("review must cover every affected existing target exactly once")
        target_by_id = {r.get("unit_id"): r for r in targets if isinstance(r, dict) and _nonempty(r.get("unit_id"))}
        for old in packet["affected_targets"]:
            decision = target_by_id.get(old["unit_id"], {})
            for field in ("source_sha256", "target_sha256", "scope"):
                if decision.get(field) != old.get(field):
                    errors.append(f"review for {old['unit_id']} is not bound to current {field}")
            if not isinstance(decision.get("decision"), str) or decision.get("decision") not in {"consistent", "retranslate_required"}:
                errors.append(f"affected target {old['unit_id']} requires a consistency decision")
    return errors


def _load_parent(root: Path, digest: str | None, authority: MemoryAuthority) -> dict[str, Any] | None:
    if digest is None:
        return None
    if not _hash(digest):
        raise ValueError("parent snapshot digest must be lowercase SHA-256")
    root_resolved, snapshots_root = _assert_memory_store_confined(root)
    path = snapshots_root / f"{digest}.json"
    resolved = path.resolve()
    if not resolved.is_relative_to(snapshots_root) or not resolved.is_file():
        raise ValueError("parent snapshot is missing")
    parent = json.loads(resolved.read_text(encoding="utf-8"))
    if _sha(parent) != digest:
        raise ValueError("parent snapshot content hash mismatch")
    if parent.get("book_id") != authority.book_id or parent.get("language_pair") != {"source": authority.source_language, "target": authority.target_language}:
        raise ValueError("parent snapshot book or language identity mismatch")
    if not isinstance(parent.get("entries"), list):
        raise ValueError("parent snapshot entries are invalid")
    for entry in parent["entries"]:
        if not isinstance(entry, dict) or not _nonempty(entry.get("candidate_id")) or not _nonempty(entry.get("source")) or not _nonempty(entry.get("target")) or not isinstance(entry.get("scope"), dict):
            raise ValueError("parent snapshot entry contract is invalid")
    return parent


def _session_barrier(lease_state: str, lease_session_id: str | None, session_stop_evidence: Mapping[str, Any] | None, operator_attestation: Mapping[str, Any] | None) -> None:
    if lease_state == "active":
        raise ValueError("active dispatch session blocks terminology publish")
    if lease_state == "expired":
        valid = isinstance(session_stop_evidence, Mapping) and _nonempty(lease_session_id) and session_stop_evidence.get("session_id") == lease_session_id and session_stop_evidence.get("status") == "stopped" and _hash(session_stop_evidence.get("evidence_sha256"))
        if not valid:
            raise ValueError("expired lease requires matching session-stop evidence")
    elif lease_state == "none":
        valid = (
            isinstance(operator_attestation, Mapping)
            and operator_attestation.get("no_active_sessions") is True
            and operator_attestation.get("book_id")
            and _hash(operator_attestation.get("authority_sha256"))
            and _hash(operator_attestation.get("evidence_sha256"))
            and _nonempty(operator_attestation.get("checked_at"))
        )
        if not valid:
            raise ValueError("no-lease publish requires operator no-active-session attestation")
    else:
        raise ValueError("unknown dispatch lease state")


def publish_memory(
    root: Path,
    packet: Mapping[str, Any],
    review: Any,
    *,
    authority: MemoryAuthority,
    current_snapshot_sha256: str | None,
    lease_state: str,
    lease_session_id: str | None = None,
    session_stop_evidence: Mapping[str, Any] | None = None,
    operator_attestation: Mapping[str, Any] | None = None,
) -> str:
    """Verify live evidence, then atomically publish a cumulative snapshot."""
    errors = _validate_packet(packet, authority)
    if errors:
        raise ValueError("; ".join(errors))
    if current_snapshot_sha256 != packet.get("parent_snapshot_sha256"):
        raise ValueError("parent snapshot compare-and-swap failed")
    errors = _validate_review(packet, review)
    if errors:
        raise ValueError("; ".join(errors))
    _session_barrier(lease_state, lease_session_id, session_stop_evidence, operator_attestation)
    if lease_state == "none" and (
        operator_attestation.get("book_id") != authority.book_id
        or operator_attestation.get("authority_sha256") != authority.authority_sha256
    ):
        raise ValueError("operator attestation does not match book and assignment")
    approved = {r["candidate_id"] for r in review["candidates"] if r["decision"] == "approve"}
    additions = [dict(row) for row in packet["candidates"] if row["candidate_id"] in approved]
    parent = _load_parent(Path(root), current_snapshot_sha256, authority)
    parent_entries = list(parent["entries"]) if parent else []
    entries = parent_entries + additions
    by_id: dict[str, Mapping[str, Any]] = {}
    merged: list[dict[str, Any]] = []
    for entry in entries:
        cid = str(entry.get("candidate_id", entry.get("id", "")))
        previous = by_id.get(cid)
        if previous is not None:
            if dict(previous) != dict(entry):
                raise ValueError(f"duplicate terminology id conflict: {cid}")
            continue
        by_id[cid] = entry
        merged.append(dict(entry))
    entries = merged
    _check_entry_conflicts(entries)
    publication_key = _sha({"packet": packet["prepared_packet_sha256"], "review": _sha(review)})
    snapshot = {
        "schema_version": 1,
        "book_id": authority.book_id,
        "language_pair": packet["language_pair"],
        "parent_snapshot_sha256": current_snapshot_sha256,
        "entries": entries,
        "review": review,
        "affected_targets": packet["affected_targets"],
        "publication_key": publication_key,
    }
    digest = _sha(snapshot)
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    pointer_path, lock_path = root / "current.json", root / "current.lock"
    resolved_root, snapshots_root = _assert_memory_store_confined(root)
    try:
        lock_fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError as exc:
        raise ValueError("snapshot pointer is locked by another publisher") from exc
    try:
        os.close(lock_fd)
        # Live evidence can change while waiting for the publication lock.
        # Recheck all authority-bound inputs under the lock before CAS and
        # before creating any content-addressed snapshot file.
        errors = _validate_packet(packet, authority)
        if errors:
            raise ValueError("; ".join(errors))
        errors = _validate_review(packet, review)
        if errors:
            raise ValueError("; ".join(errors))
        actual = _current_parent(root)
        if actual == digest:
            current = _load_parent(root, actual, authority)
            if current and current.get("publication_key") == publication_key:
                return digest
            raise ValueError("current snapshot digest collision")
        if actual != current_snapshot_sha256:
            # The same publication may have won a concurrent call.
            if actual is not None:
                current = _load_parent(root, actual, authority)
                if current and current.get("publication_key") == publication_key:
                    return actual
            raise ValueError("parent snapshot compare-and-swap failed")
        snapshots = root / "snapshots"
        # Repeat immediately before any storage mutation, including first
        # publication where there is no current pointer to validate.
        resolved_root, snapshots_root = _assert_memory_store_confined(root)
        snapshots.mkdir(parents=True, exist_ok=True)
        snapshot_path = snapshots / f"{digest}.json"
        if not snapshot_path.resolve().is_relative_to(snapshots_root):
            raise ValueError("snapshot path escapes memory root")
        created_snapshot = False
        if snapshot_path.exists() and snapshot_path.read_bytes() != _canonical(snapshot):
            raise ValueError("content-addressed snapshot path collision")
        if not snapshot_path.exists():
            temp = snapshot_path.with_name(snapshot_path.name + f".{os.getpid()}.tmp")
            if not temp.resolve().is_relative_to(snapshots_root):
                raise ValueError("snapshot temporary path escapes memory root")
            try:
                temp.write_bytes(_canonical(snapshot))
                os.replace(temp, snapshot_path)
                created_snapshot = True
            finally:
                temp.unlink(missing_ok=True)
        try:
            atomic_write_json(pointer_path, {"snapshot_sha256": digest, "path": f"snapshots/{digest}.json"})
        except Exception:
            if created_snapshot:
                snapshot_path.unlink(missing_ok=True)
            raise
    finally:
        lock_path.unlink(missing_ok=True)
    return digest
