"""Recoverable snapshots and conservative polish synchronization for footnote apply."""
from __future__ import annotations

import difflib
import json
import hashlib
import shutil
import tempfile
import os
import re
from pathlib import Path
from typing import Any, Mapping

from ..workflow_contracts import atomic_write_json, atomic_write_text, sha256_file

VERSION_DIR = "footnote_normalization_versions"
JOURNAL = "footnote_normalization_changes.json"
ATTEMPT_MARKER = "footnote_normalization_apply_marker.json"


def _is_link(path: Path) -> bool:
    is_junction = getattr(path, "is_junction", None)
    return path.is_symlink() or bool(is_junction and is_junction())


def _version_id(receipt: Mapping[str, Any]) -> str:
    stable = {key: value for key, value in receipt.items() if key not in {"version_id", "previous_version_id"}}
    payload = json.dumps(stable, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _atomic_bytes(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, raw = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    tmp = Path(raw)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink()


def _read(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    return value if isinstance(value, dict) else {}


def _safe_workspace_path(output_dir: Path, relative: str) -> Path | None:
    """Resolve a workspace child without following a symlink/junction escape."""
    root = Path(output_dir).resolve()
    candidate = root / relative
    try:
        candidate.resolve().relative_to(root)
    except (OSError, ValueError):
        return None
    current = root
    for part in Path(relative).parts:
        if part in {".", ".."}:
            return None
        current = current / part
        if _is_link(current):
            return None
    return candidate


def capture_last_good(output_dir: Path) -> str | None:
    """Snapshot a receipt-bound normalized tree; return its stable version id."""
    output_dir = Path(output_dir)
    receipt_path = output_dir / "footnote_normalization.json"
    try:
        receipt = _read(receipt_path)
        if receipt.get("valid") is not True or receipt.get("status") != "validated":
            return None
        from .footnote_source import markdown_inventory

        target = _safe_workspace_path(output_dir, "footnote_normalized")
        version_root = _safe_workspace_path(output_dir, VERSION_DIR)
        if target is None or version_root is None or _is_link(target):
            return None
        inventory = markdown_inventory(target)
        if inventory != receipt.get("target_sha256"):
            return None
        if any(Path(name).name != name or not name.endswith(".md") for name in inventory):
            return None
        version_id = _version_id(receipt)
        directory = version_root / version_id
        if _is_link(directory):
            return None
        if not directory.is_dir():
            directory.parent.mkdir(parents=True, exist_ok=True)
            staging = Path(tempfile.mkdtemp(prefix=version_id + ".", dir=directory.parent))
            (staging / "files").mkdir(parents=True)
            for name in inventory:
                if _is_link(target / name):
                    return None
                dest = staging / "files" / name
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(target / name, dest)
            atomic_write_json(staging / "receipt.json", receipt)
            staging.replace(directory)
        return version_id
    except (OSError, UnicodeError, ValueError, TypeError):
        return None


def recover_interrupted_apply(output_dir: Path) -> bool:
    """Restore current receipt's exact files if interrupted materialization changed them."""
    output_dir = Path(output_dir)
    try:
        receipt_path = output_dir / "footnote_normalization.json"
        receipt = _read(receipt_path)
        if receipt.get("valid") is not True or receipt.get("status") != "validated":
            return False
        from .footnote_source import markdown_inventory

        target = _safe_workspace_path(output_dir, "footnote_normalized")
        version_root = _safe_workspace_path(output_dir, VERSION_DIR)
        if target is None or version_root is None:
            return False
        current_inventory = markdown_inventory(target)
        old_inventory = receipt.get("target_sha256", {})
        if current_inventory == old_inventory:
            return True
        marker_path = output_dir / ATTEMPT_MARKER
        marker = _read(marker_path)
        if (marker.get("status") != "running"
                or marker.get("baseline_version") != _version_id(receipt)):
            return False
        expected_inventory = marker.get("expected_target_sha256", {})
        if set(current_inventory) != set(old_inventory) or set(expected_inventory) != set(old_inventory):
            return False
        if any(current_inventory[name] not in {old_inventory[name], expected_inventory[name]} for name in old_inventory):
            return False
        if current_inventory == old_inventory:
            return True
        version = _version_id(receipt)
        base = version_root / version
        if _is_link(base):
            return False
        saved = _read(base / "receipt.json")
        if _version_id(saved) != version or _version_id(receipt) != version:
            return False
        for name, digest in receipt.get("target_sha256", {}).items():
            if Path(name).name != name or not name.endswith(".md"):
                return False
            source = base / "files" / name
            if _is_link(source) or not source.is_file() or sha256_file(source) != digest:
                return False
        target.mkdir(parents=True, exist_ok=True)
        for name in receipt.get("target_sha256", {}):
            dest = target / name
            dest.parent.mkdir(parents=True, exist_ok=True)
            _atomic_bytes(dest, (base / "files" / name).read_bytes())
        marker["status"] = "recovered"
        atomic_write_json(marker_path, marker)
        return True
    except (OSError, UnicodeError, ValueError, TypeError):
        return False


def preserve_failed_attempt(output_dir: Path) -> str | None:
    """Keep staged invalid text and its audit beside the failure receipt."""
    import time

    output_dir = Path(output_dir)
    attempt_id = f"attempt-{time.time_ns()}"
    attempt_dir = output_dir / "footnote_normalization_attempts" / attempt_id
    target = output_dir / "footnote_normalized"
    try:
        (attempt_dir / "footnote_normalized").mkdir(parents=True, exist_ok=False)
        for path in target.glob("*.md"):
            if path.is_file() and not _is_link(path):
                shutil.copyfile(path, attempt_dir / "footnote_normalized" / path.name)
        audit = output_dir / "footnote_result_audit.json"
        if audit.is_file() and not audit.is_symlink():
            shutil.copyfile(audit, attempt_dir / audit.name)
        return attempt_dir.relative_to(output_dir).as_posix()
    except OSError:
        return None


def mark_apply_running(output_dir: Path, transformed: Mapping[str, str], baseline_version: str | None) -> None:
    """Durably identify the exact target inventory an apply is about to publish."""
    expected = {
        name: hashlib.sha256(text.encode("utf-8")).hexdigest()
        for name, text in transformed.items()
    }
    atomic_write_json(Path(output_dir) / ATTEMPT_MARKER, {
        "schema_version": 1,
        "status": "running",
        "baseline_version": baseline_version,
        "expected_target_sha256": expected,
    })


def mark_apply_complete(output_dir: Path, version_id: str | None) -> None:
    path = Path(output_dir) / ATTEMPT_MARKER
    if not path.is_file():
        return
    try:
        marker = _read(path)
    except (OSError, UnicodeError, ValueError, TypeError):
        return
    marker["status"] = "complete"
    marker["version_id"] = version_id
    atomic_write_json(path, marker)


def _blocks(text: str) -> list[str]:
    return [part.rstrip("\r\n") for part in re.split(r"(?:\r?\n){2,}", text) if part.strip()]


def _exact_block_edit(text: str, old: str, new: str) -> tuple[str, str]:
    """Replace a unique whole-block sequence, never a substring of prose/code."""
    def in_code_region(offset: int) -> bool:
        cursor = 0
        fence_char = ""
        fence_len = 0
        for line in text.splitlines(keepends=True):
            stripped = line.lstrip(" ")
            indent = len(line) - len(stripped)
            if cursor <= offset < cursor + len(line):
                return bool(fence_char) or indent >= 4 or line.startswith("\t")
            marker = re.match(r"(`{3,}|~{3,})", stripped)
            if marker:
                token = marker.group(1)
                if not fence_char:
                    fence_char, fence_len = token[0], len(token)
                elif token[0] == fence_char and len(token) >= fence_len:
                    fence_char, fence_len = "", 0
            cursor += len(line)
        return bool(fence_char)

    if old.startswith("\0B") or old.startswith("\0A"):
        anchor = old[2:]
        eol = "\r\n" if "\r\n" in text else "\n"
        anchor = anchor.replace("\n", eol)
        new = new.replace("\n", eol)
        locations = []
        cursor = 0
        while True:
            start = text.find(anchor, cursor)
            if start < 0:
                break
            end = start + len(anchor)
            left_ok = start == 0 or text[:start].endswith(("\n\n", "\r\n\r\n"))
            right_ok = end == len(text) or not text[end:].strip() or text[end:].startswith(("\n\n", "\r\n\r\n"))
            if left_ok and right_ok and not in_code_region(start):
                locations.append((start, end))
            cursor = start + 1
        if len(locations) != 1:
            if new and new in text:
                return text, "already_applied"
            return text, "missing_anchor" if not locations else "ambiguous_anchor"
        start, end = locations[0]
        separator = "\r\n\r\n" if "\r\n" in text else "\n\n"
        if old.startswith("\0B"):
            insertion = end
            if text[insertion:].startswith(separator + new):
                return text, "already_applied"
            return text[:insertion] + separator + new + text[insertion:], "applied"
        if text[:start].endswith(new + separator):
            return text, "already_applied"
        return text[:start] + new + separator + text[start:], "applied"
    if not old:
        return text, "missing"
    eol = "\r\n" if "\r\n" in text else "\n"
    old = old.replace("\n", eol)
    new = new.replace("\n", eol)
    # Avoid automatic edits to fenced code; paragraph boundaries in such blocks
    # are not semantic prose boundaries.
    if "```" in old or "~~~" in old or "```" in new or "~~~" in new:
        return text, "ambiguous"
    starts = []
    cursor = 0
    while True:
        start = text.find(old, cursor)
        if start < 0:
            break
        end = start + len(old)
        left_ok = start == 0 or text[:start].endswith(("\n\n", "\r\n\r\n"))
        right_ok = end == len(text) or not text[end:].strip() or text[end:].startswith(("\n\n", "\r\n\r\n"))
        if left_ok and right_ok and not in_code_region(start):
            starts.append(start)
        cursor = start + 1
    if len(starts) == 1:
        start = starts[0]
        return text[:start] + new + text[start + len(old):], "applied"
    if not starts and new and text.count(new) == 1:
        return text, "already_applied"
    return text, "missing" if not starts else "ambiguous"


def publish_success(output_dir: Path, previous_version: str | None) -> str | None:
    """Snapshot a newly successful apply and append a source-bound delta journal."""
    output_dir = Path(output_dir)
    current = capture_last_good(output_dir)
    if not current:
        return None
    if not previous_version or previous_version == current:
        return current
    root = output_dir / VERSION_DIR
    try:
        old = _read(root / previous_version / "receipt.json")
        new = _read(root / current / "receipt.json")
        changes: dict[str, Any] = {}
        for name in sorted(set(old.get("target_sha256", {})) | set(new.get("target_sha256", {}))):
            old_path = root / previous_version / "files" / name
            new_path = root / current / "files" / name
            old_text = old_path.read_text(encoding="utf-8") if old_path.is_file() else ""
            new_text = new_path.read_text(encoding="utf-8") if new_path.is_file() else ""
            before, after = _blocks(old_text), _blocks(new_text)
            matcher = difflib.SequenceMatcher(a=before, b=after, autojunk=False)
            ops = []
            for tag, i1, i2, j1, j2 in matcher.get_opcodes():
                if tag != "equal":
                    old_blocks, new_blocks = before[i1:i2], after[j1:j2]
                    if tag == "replace" and len(old_blocks) == len(new_blocks):
                        pairs = [([old_block], [new_block]) for old_block, new_block in zip(old_blocks, new_blocks)]
                    else:
                        pairs = [(old_blocks, new_blocks)]
                    for old_part, new_part in pairs:
                        ops.append({"kind": tag, "old": old_part, "new": new_part,
                                    "anchor_before": before[i1 - 1] if i1 else "",
                                    "anchor_after": before[i2] if i2 < len(before) else ""})
            if ops:
                changes[name] = {
                    "old_source_sha256": old.get("target_sha256", {}).get(name),
                    "new_source_sha256": new.get("target_sha256", {}).get(name),
                    "operations": ops,
                }
        journal_path = output_dir / JOURNAL
        try:
            journal = _read(journal_path)
        except (OSError, ValueError, TypeError):
            journal = {"schema_version": 1, "transitions": []}
        transitions = journal.get("transitions") if isinstance(journal.get("transitions"), list) else []
        transition = {"from_version": previous_version, "to_version": current,
                      "from_source_sha256": old.get("target_sha256", {}),
                      "to_source_sha256": new.get("target_sha256", {}),
                      "resolved_blocks": list(new.get("resolved_blocks", [])), "files": changes}
        if not any(item.get("to_version") == current for item in transitions if isinstance(item, Mapping)):
            transitions.append(transition)
        receipt_path = output_dir / "footnote_normalization.json"
        receipt = _read(receipt_path)
        receipt["version_id"] = current
        receipt["previous_version_id"] = previous_version
        atomic_write_json(receipt_path, receipt)
        atomic_write_json(journal_path, {"schema_version": 1, "transitions": transitions})
        return current
    except (OSError, UnicodeError, ValueError, TypeError):
        return current


def sync_polished_targets(output_dir: Path) -> dict[str, list[dict[str, Any]]]:
    """Apply only unique exact paragraph patches; return explicit review hints otherwise."""
    output_dir = Path(output_dir)
    hints: dict[str, list[dict[str, Any]]] = {}
    try:
        journal = _read(output_dir / JOURNAL)
        receipt = _read(output_dir / "footnote_normalization.json")
        transitions = journal.get("transitions", [])
        old_manifest = _read(output_dir / "polish_subagent_manifest.json")
    except (OSError, UnicodeError, ValueError, TypeError):
        return {}
    polished = _safe_workspace_path(output_dir, "polished_markdown")
    target_hashes = receipt.get("target_sha256", {})
    if polished is None:
        return {}
    if receipt.get("valid") is not True or receipt.get("status") != "validated":
        return {}
    try:
        from .footnote_source import markdown_inventory
        if markdown_inventory(output_dir / "footnote_normalized") != target_hashes:
            return {}
        attempt_path = output_dir / "footnote_normalization_attempt.json"
        if attempt_path.is_file():
            attempt = _read(attempt_path)
            if attempt.get("valid") is not True or attempt.get("status") != "resolved":
                return {}
    except (OSError, UnicodeError, ValueError, TypeError):
        return {}
    cursor = old_manifest.get("source_sha256", {})
    if old_manifest.get("source_dir") != "footnote_normalized":
        return {}
    prior_hints = old_manifest.get("footnote_change_hints", {})
    if cursor == target_hashes and isinstance(prior_hints, Mapping):
        current_hints = {}
        for name, rows in prior_hints.items():
            target_path = polished / str(name)
            if name not in target_hashes or not isinstance(rows, list) or not target_path.is_file():
                continue
            polished_hash = sha256_file(target_path)
            rebound = []
            for item in rows:
                if (not isinstance(item, Mapping) or not item.get("new_source_sha256")
                        or item.get("new_source_sha256") != target_hashes.get(name)):
                    continue
                updated = dict(item)
                if not updated.get("new_target_sha256") or updated.get("new_target_sha256") != polished_hash:
                    updated["status"] = "review_required"
                    updated["reason"] = "polished target changed after the prior sync; review this block in the current assigned file"
                    updated["new_target_sha256"] = polished_hash
                rebound.append(updated)
            if rebound:
                current_hints[str(name)] = rebound
        current_hints = {name: rows for name, rows in current_hints.items() if rows}
        if current_hints:
            return current_hints
    # Select only transitions forming an exact source-version chain to the
    # current receipt. An incomplete chain is retained as review context.
    chain: list[Mapping[str, Any]] = []
    current_hashes = cursor
    for transition in transitions if isinstance(transitions, list) else []:
        if not isinstance(transition, Mapping):
            continue
        if transition.get("from_source_sha256") == current_hashes:
            chain.append(transition)
            current_hashes = transition.get("to_source_sha256")
    if current_hashes != target_hashes:
        # Exact source lineage unavailable: do not make edits. Caller can use
        # the journal itself for a locally scoped review hint.
        changed = [name for name, digest in target_hashes.items() if cursor.get(name) != digest]
        return {name: [{"status": "review_required", "reason": "successful version history is incomplete; compare only this assigned file",
                        "old_source_sha256": cursor.get(name), "new_source_sha256": target_hashes.get(name),
                        "old_target_sha256": cursor.get(name), "new_target_sha256": sha256_file(polished / name),
                        "origin_new_source_sha256": target_hashes.get(name),
                        "old_block": "", "new_block": ""}] for name in changed if (polished / name).is_file()}
    changed_names = sorted({str(name) for row in chain for name in (row.get("files", {}) or {})})
    applied_path = _safe_workspace_path(output_dir, f"{VERSION_DIR}/sync_applied.json")
    if applied_path is None:
        return {}
    try:
        applied = _read(applied_path)
    except (OSError, UnicodeError, ValueError, TypeError):
        applied = {"files": {}}
    applied_files = applied.get("files") if isinstance(applied.get("files"), dict) else {}
    for name in changed_names:
        target = _safe_workspace_path(output_dir, f"polished_markdown/{name}")
        if target is None or not target.is_file() or _is_link(target):
            continue
        original_hash = sha256_file(target)
        prior = applied_files.get(name, {})
        if (prior.get("target_sha256") == original_hash
                and prior.get("to_source_sha256") == target_hashes):
            rows = prior.get("hints")
            if isinstance(rows, list) and rows:
                hints[name] = rows
            continue
        try:
            text = target.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            continue
        file_hints = []
        for transition in chain:
            delta = (transition.get("files", {}) or {}).get(name)
            if not isinstance(delta, Mapping):
                continue
            for op in delta.get("operations", []):
                if not isinstance(op, Mapping):
                    continue
                old = "\n\n".join(op.get("old") or [])
                new = "\n\n".join(op.get("new") or [])
                if not old:
                    anchor = str(op.get("anchor_before") or op.get("anchor_after") or "")
                    old = ("\0B" if op.get("anchor_before") else "\0A") + anchor
                text, edit_status = _exact_block_edit(text, old, new)
                origin = {
                    "from_version": transition.get("from_version"),
                    "to_version": transition.get("to_version"),
                    "origin_old_source_sha256": (transition.get("from_source_sha256", {}) or {}).get(name),
                    "origin_new_source_sha256": (transition.get("to_source_sha256", {}) or {}).get(name),
                    "old_target_sha256": delta.get("old_source_sha256"),
                    "origin_new_target_sha256": delta.get("new_source_sha256"),
                    "anchor_before": op.get("anchor_before", ""),
                    "anchor_after": op.get("anchor_after", ""),
                    "footnote_identities": sorted(set(re.findall(r"\[\^([^\]\r\n]+)\]", old + "\n" + new))),
                    "old_block": old, "new_block": new,
                }
                if edit_status in {"applied", "already_applied"}:
                    file_hints.append({**origin, "status": "synchronized"})
                    continue
                reason = {
                    "missing": "old block is missing from polished target",
                    "ambiguous": "old block is repeated in polished target",
                    "missing_anchor": "insertion anchor is missing from polished target",
                    "ambiguous_anchor": "insertion anchor is repeated in polished target",
                }.get(edit_status, "exact block patch is ambiguous")
                file_hints.append({**origin, "status": "review_required", "reason": reason})
        if text != target.read_text(encoding="utf-8"):
            backup = _safe_workspace_path(output_dir, f"polished_markdown/{name}.footnote-sync.bak")
            if backup is None or _is_link(backup):
                continue
            if not backup.exists():
                shutil.copyfile(target, backup)
            atomic_write_text(target, text)
        polished_hash = sha256_file(target)
        for row in file_hints:
            row["new_source_sha256"] = target_hashes.get(name)
            row["new_target_sha256"] = polished_hash
        if file_hints:
            hints[name] = file_hints
        applied_files[name] = {"target_sha256": polished_hash, "to_source_sha256": target_hashes,
                               "hints": file_hints}
    applied["files"] = applied_files
    atomic_write_json(applied_path, applied)
    return hints
