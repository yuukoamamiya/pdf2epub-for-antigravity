"""Shared, source-bound book-entity hand-off and reuse contract."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any, Mapping

from loguru import logger

from .document_roles import infer_translation_file_role
from .workflow_contracts import atomic_write_json, atomic_write_text, sha256_file

ENTITY_SOURCE_FILTER_VERSION = 1
ENTITY_BASELINE_FILE = "entity_completed_snapshot.json"
EPUB_ENTITY_EXCLUDED_ROLES = frozenset({"index", "bibliography", "copyright", "cover"})


def entity_source_selection(source_dir: Path, source_stage: str) -> tuple[list[Path], list[Path]]:
    """Select entity-extraction units and deterministic EPUB exclusions."""
    source_dir = Path(source_dir)
    all_files = sorted(source_dir.glob("*.md"))
    if source_stage != "epub-compressed":
        return all_files, []
    active_names: set[str] = set()
    for mapping_path in sorted(source_dir.glob("*.mapping.json")):
        stem = mapping_path.name[: -len(".mapping.json")]
        try:
            mapping = json.loads(mapping_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            mapping = {}
        parts = mapping.get("translation_parts")
        if isinstance(parts, list) and parts:
            active_names.update(str(name) for name in parts)
        else:
            active_names.add(f"{stem}.md")
    if active_names:
        all_files = [path for path in all_files if path.name in active_names]
    included, excluded = [], []
    for path in all_files:
        (excluded if infer_translation_file_role(path.name) in EPUB_ENTITY_EXCLUDED_ROLES else included).append(path)
    return included, excluded


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _safe_source_path(output_dir: Path, source_dir: Path, name: str) -> Path | None:
    """Accept only selected direct Markdown children inside the book workspace."""
    if Path(name).name != name or name in {".", ".."} or not name.endswith(".md"):
        return None
    root = Path(output_dir).resolve()
    parent = Path(source_dir)
    try:
        parent.resolve().relative_to(root)
        candidate = parent / name
        candidate.resolve().relative_to(root)
    except (OSError, ValueError):
        return None
    if candidate.is_symlink():
        return None
    is_junction = getattr(candidate, "is_junction", None)
    if is_junction and is_junction():
        return None
    return candidate


def _safe_output_path(output_dir: Path, relative: str) -> Path | None:
    if Path(relative).is_absolute() or ".." in Path(relative).parts:
        return None
    root = Path(output_dir).resolve()
    candidate = root / relative
    try:
        candidate.resolve().relative_to(root)
    except (OSError, ValueError):
        return None
    current = root
    for part in Path(relative).parts:
        current = current / part
        if current.is_symlink() or (getattr(current, "is_junction", None) and current.is_junction()):
            return None
    return candidate


def _format_identity(path: Path) -> str:
    """Hash ordered Markdown content with line-ending/spacing-only differences removed.

    Paragraph order, paragraph boundaries, headings, list markers, and markup remain
    significant. This intentionally does not treat reordered prose as equivalent.
    """
    text = path.read_text(encoding="utf-8").replace("\r\n", "\n").replace("\r", "\n")
    blocks: list[str] = []
    current: list[str] = []
    fence_char = ""
    fence_length = 0
    in_footnote = False

    def prose_line(line: str) -> str:
        # Keep inline-code payload byte-for-byte while normalizing surrounding prose.
        chunks = re.split(r"(`+[^`]*`+)", line)
        return "".join(chunk if chunk.startswith("`") else re.sub(r"\s+", " ", chunk) for chunk in chunks)

    for line in text.split("\n"):
        stripped = line.strip()
        fence_match = re.match(r"^(`{3,}|~{3,})", stripped)
        if fence_char:
            blocks.append(line)
            if fence_match:
                marker = fence_match.group(1)
                if marker[0] == fence_char and len(marker) >= fence_length:
                    fence_char, fence_length = "", 0
            continue
        if fence_match:
            if current:
                blocks.append(" ".join(current))
                current = []
            marker = fence_match.group(1)
            fence_char, fence_length = marker[0], len(marker)
            blocks.append(line)
            continue
        if not stripped:
            if current:
                blocks.append(" ".join(current))
                current = []
            in_footnote = False
            continue
        indent = len(line) - len(line.lstrip(" \t"))
        is_footnote_def = bool(re.match(r"^\[\^[^]]+\]:", stripped))
        if indent >= 4 or (indent >= 2 and in_footnote):
            if current:
                blocks.append(" ".join(current))
                current = []
            blocks.append(line)
            in_footnote = False
        elif is_footnote_def:
            if current:
                blocks.append(" ".join(current))
                current = []
            blocks.append(line[:indent] + prose_line(stripped))
            in_footnote = True
        elif re.match(r"^(#{1,6}\s|>\s?|[-+*]\s|\d+[.)]\s|\|)", stripped):
            if current:
                blocks.append(" ".join(current))
                current = []
            blocks.append(line[:indent] + prose_line(stripped))
            in_footnote = False
        else:
            current.append(prose_line(stripped))
            in_footnote = False
    if current:
        blocks.append(" ".join(current))
    return hashlib.sha256("\n\n".join(blocks).encode("utf-8")).hexdigest()


def _inventory(output_dir: Path, source_dir: Path, source_stage: str) -> tuple[dict[str, str], dict[str, str], list[str], list[str]]:
    included, excluded = entity_source_selection(source_dir, source_stage)
    raw: dict[str, str] = {}
    semantic: dict[str, str] = {}
    for path in included:
        safe = _safe_source_path(output_dir, source_dir, path.name)
        if safe is None or not safe.is_file():
            raise ValueError(f"unsafe or missing entity source unit: {path.name}")
        raw[path.name] = sha256_file(safe)
        semantic[path.name] = _format_identity(safe)
    return raw, semantic, [p.name for p in included], [p.name for p in excluded]


def _snapshot_id(source_dir: str, source_stage: str, source_language: str, target_language: str, semantic: Mapping[str, str]) -> str:
    payload = {"source_dir": source_dir, "source_stage": source_stage, "source_language": source_language,
               "target_language": target_language, "semantic_source_sha256": dict(sorted(semantic.items()))}
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()


def _load_entities(output_dir: Path) -> dict[str, Any] | None:
    return _read_json(output_dir / "translation_entities.json")


def _store_accepted_entities(output_dir: Path, entity_path: Path) -> dict[str, str] | None:
    try:
        raw = entity_path.read_bytes()
        data = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    # Bind the accepted snapshot to the exact validated bytes. Reformatting here
    # would silently change the accepted hash and break recovery of legacy output.
    digest = hashlib.sha256(raw).hexdigest()
    relative = f"entity_completed/{digest}.json"
    saved = _safe_output_path(output_dir, relative)
    if saved is None:
        return None
    if saved.is_file():
        if sha256_file(saved) != digest:
            return None
    else:
        atomic_write_text(saved, raw.decode("utf-8"))
        if sha256_file(saved) != digest:
            return None
    return {"accepted_file": relative, "accepted_sha256": digest}


def _load_accepted_entities(output_dir: Path, baseline: Mapping[str, Any]) -> dict[str, Any] | None:
    relative = baseline.get("accepted_file")
    digest = baseline.get("accepted_sha256")
    if not isinstance(relative, str) or not isinstance(digest, str):
        return None
    if Path(relative).as_posix() != f"entity_completed/{digest}.json":
        return None
    path = _safe_output_path(output_dir, relative)
    if path is None or not path.is_file() or sha256_file(path) != digest:
        return None
    return _read_json(path)


def _write_bounded_contexts(output_dir: Path, source_dir: Path, names: list[str], pending: list[str],
                            removed: list[str]) -> dict[str, list[str]]:
    """Write at most 800 characters from each adjacent unit for each changed unit."""
    result: dict[str, list[str]] = {}
    for name in [*pending, *removed]:
        if name in names:
            index = names.index(name)
        else:
            index = next((i for i, candidate in enumerate(names) if candidate > name), len(names))
        snippets: list[str] = []
        if index > 0:
            previous = names[index - 1]
            path = _safe_source_path(output_dir, source_dir, previous)
            if path:
                text = path.read_text(encoding="utf-8")
                snippets.append(f"[Previous unit {previous}; trailing excerpt]\n{text[-800:]}")
        if index + 1 < len(names):
            following = names[index + 1]
            path = _safe_source_path(output_dir, source_dir, following)
            if path:
                text = path.read_text(encoding="utf-8")
                snippets.append(f"[Next unit {following}; leading excerpt]\n{text[:800]}")
        if not snippets:
            continue
        digest = hashlib.sha256(name.encode("utf-8")).hexdigest()[:12]
        relative = f"entity_review_contexts/{digest}.md"
        atomic_write_text(output_dir / relative, "\n\n---\n\n".join(snippets))
        result[name] = [relative]
    return result


def _footnote_payloads(block: str) -> list[tuple[str, str]]:
    """Extract complete footnote definition blocks for exact move conservation."""
    lines = block.replace("\r\n", "\n").replace("\r", "\n").splitlines()
    result: list[tuple[str, str]] = []
    current_id: str | None = None
    current: list[str] = []
    for line in lines:
        match = re.match(r"^\s*\[\^([^\]]+)\]:\s*(.*)$", line)
        if match:
            if current_id is not None:
                result.append((current_id, "\n".join(current)))
            current_id, current = match.group(1), [match.group(2)]
        elif current_id is not None and (line.startswith((" ", "\t")) or not line.strip()):
            current.append(line)
        elif current_id is not None:
            result.append((current_id, "\n".join(current)))
            current_id, current = None, []
    if current_id is not None:
        result.append((current_id, "\n".join(current)))
    return result


def _footnote_block_parts(block: str) -> tuple[list[tuple[str, str]], list[str]]:
    """Separate definition payloads from ordinary prose in one sync block.

    A version diff may contain both a body paragraph and a note definition.
    Treating such a block as note-only loses proof that the paragraph was
    conserved when it becomes a note continuation.
    """
    lines = block.replace("\r\n", "\n").replace("\r", "\n").splitlines()
    notes: list[tuple[str, str]] = []
    body: list[str] = []
    body_lines: list[str] = []
    note_id: str | None = None
    note_lines: list[str] = []

    def flush_body() -> None:
        nonlocal body_lines
        value = "\n".join(body_lines).strip()
        if value:
            body.append(value)
        body_lines = []

    def flush_note() -> None:
        nonlocal note_id, note_lines
        if note_id is not None:
            notes.append((note_id, "\n".join(note_lines).strip()))
        note_id, note_lines = None, []

    for line in lines:
        match = re.match(r"^\s*\[\^([^\]]+)\]:\s*(.*)$", line)
        if match:
            flush_body()
            flush_note()
            note_id, note_lines = match.group(1), [match.group(2)]
        elif note_id is not None and (line.startswith((" ", "\t")) or not line.strip()):
            note_lines.append(line)
        else:
            if note_id is not None:
                flush_note()
            if not line.strip():
                flush_body()
            else:
                body_lines.append(line)
    flush_body()
    flush_note()
    return notes, body


def _same_footnote_coverage(old_notes: list[tuple[str, str]], new_notes: list[tuple[str, str]],
                           moved_body: list[str]) -> bool:
    """Prove exact notes/body conservation, including one body paragraph folded into a note."""
    normalize = lambda value: value.replace("\r\n", "\n").replace("\r", "\n").strip()
    old_by_id: dict[str, list[str]] = {}
    new_by_id: dict[str, list[str]] = {}
    for key, text in old_notes:
        old_by_id.setdefault(key, []).append(normalize(text))
    for key, text in new_notes:
        new_by_id.setdefault(key, []).append(normalize(text))
    unused_bodies = [normalize(value) for value in moved_body]
    for key in set(old_by_id) | set(new_by_id):
        old_values = list(old_by_id.get(key, []))
        new_values = list(new_by_id.get(key, []))
        for value in list(new_values):
            if value in old_values:
                old_values.remove(value)
                new_values.remove(value)
        for value in new_values:
            matched = False
            for old_value in list(old_values):
                # A note can absorb multiple separately reviewed body
                # paragraphs. Only contiguous paragraphs in their original
                # order may be folded into the definition.
                for start in range(len(unused_bodies)):
                    for end in range(start + 1, len(unused_bodies) + 1):
                        moved = unused_bodies[start:end]
                        if any(not item for item in moved):
                            continue
                        flat = " ".join(moved)
                        indented = "\n    ".join(moved)
                        additions = {
                            f"{old_value} {flat}".strip(),
                            f"{old_value}\n    {indented}".strip(),
                            f"{flat} {old_value}".strip(),
                            f"{indented}\n    {old_value}".strip(),
                        }
                        if value not in additions:
                            continue
                        old_values.remove(old_value)
                        del unused_bodies[start:end]
                        matched = True
                        break
                    if matched:
                        break
            if not matched:
                for body in list(unused_bodies):
                    if body and value == body:
                        unused_bodies.remove(body)
                        matched = True
                        break
            if not matched:
                return False
        if old_values:
            return False
    return not unused_bodies


def _verified_footnote_relocation_reuse(
    output_dir: Path, source_dir: Path, manifest: Mapping[str, Any], baseline: Mapping[str, Any],
    raw: Mapping[str, str], semantic: Mapping[str, str], snapshot_id: str, config: Mapping[str, Any],
) -> bool:
    """Carry coverage across exact, validated footnote moves in polished files only.

    The reuse path requires a source-hash-matched pre-sync backup, the recorded
    exact successful normalization-version chain, unchanged OCR provenance, all
    machine patches marked synchronized, exact patch reconstruction, and global
    conservation of footnote definition payloads. It never treats arbitrary
    paragraph reordering as equivalent.
    """
    if (manifest.get("source_stage") != "polished"
            or baseline.get("origin") != "validated"
            or not baseline.get("footnote_version_id")
            or baseline.get("source_dir") != manifest.get("source_dir")):
        return False
    entity_path = Path(output_dir) / "translation_entities.json"
    if not entity_path.is_file() or sha256_file(entity_path) != baseline.get("entity_sha256"):
        return False
    try:
        from .refine.footnote_status import footnote_normalization_is_current
        from .validation_receipts import validation_receipt_is_current

        if not footnote_normalization_is_current(output_dir, config=config):
            return False
        normalized_source = Path(output_dir) / "footnote_normalized"
        if not validation_receipt_is_current(
            Path(output_dir) / "polish_validation.json", normalized_source, source_dir,
            task="polish", output_dir=output_dir, config=config,
        ):
            return False
    except (OSError, TypeError, ValueError):
        return False
    changed = {name for name, digest in raw.items() if baseline.get("source_sha256", {}).get(name) != digest}
    if not changed or set(raw) != set(baseline.get("source_sha256", {})):
        return False
    root = Path(output_dir)
    receipt_path = _safe_output_path(root, "footnote_normalization.json")
    journal_path = _safe_output_path(root, "footnote_normalization_changes.json")
    applied_path = _safe_output_path(root, "footnote_normalization_versions/sync_applied.json")
    if receipt_path is None or journal_path is None or applied_path is None:
        return False
    receipt = _read_json(receipt_path)
    journal = _read_json(journal_path)
    applied = _read_json(applied_path)
    current_version = receipt.get("version_id") if receipt else None
    start_version = baseline.get("footnote_version_id")
    if not receipt or receipt.get("status") != "validated" or receipt.get("valid") is not True or not current_version:
        return False
    if not re.fullmatch(r"[0-9a-f]{64}", str(start_version)) or not re.fullmatch(r"[0-9a-f]{64}", str(current_version)):
        return False
    versions_root = _safe_output_path(root, "footnote_normalization_versions")
    normalized_root = _safe_output_path(root, "footnote_normalized")
    if versions_root is None or normalized_root is None:
        return False
    try:
        old_receipt_path = _safe_output_path(root, f"footnote_normalization_versions/{start_version}/receipt.json")
        if old_receipt_path is None:
            return False
        old_receipt = _read_json(old_receipt_path)
        if not old_receipt:
            return False
        from .refine.footnote_source import markdown_inventory
        from .refine.footnote_versions import _version_id
        if (_version_id(old_receipt) != start_version or _version_id(receipt) != current_version
                or markdown_inventory(normalized_root) != receipt.get("target_sha256")):
            return False
        current_receipt = receipt
        provenance_keys = ("source_sha256", "source_kind", "ocr_evidence_mode", "tree_progress_sha256")
        if any(old_receipt.get(key) != current_receipt.get(key) for key in provenance_keys):
            return False
        transitions = journal.get("transitions", []) if journal else []
        chain: list[Mapping[str, Any]] = []
        cursor = start_version
        while cursor != current_version:
            matches = [row for row in transitions if isinstance(row, Mapping) and row.get("from_version") == cursor]
            if len(matches) != 1:
                return False
            transition = matches[0]
            next_id = transition.get("to_version")
            if not isinstance(next_id, str) or not next_id:
                return False
            if not transition.get("resolved_blocks"):
                return False
            saved_path = _safe_output_path(root, f"footnote_normalization_versions/{next_id}/receipt.json")
            if saved_path is None:
                return False
            saved = _read_json(saved_path)
            if not saved or _version_id(saved) != next_id:
                return False
            if any(old_receipt.get(key) != saved.get(key) for key in provenance_keys):
                return False
            chain.append(transition)
            cursor = next_id
            if len(chain) > len(transitions):
                return False
        if not chain:
            return False
        from .refine.footnote_versions import _exact_block_edit
        applied_files = applied.get("files", {}) if applied else {}
        old_payloads: list[tuple[str, str]] = []
        new_payloads: list[tuple[str, str]] = []
        old_body: list[str] = []
        new_body: list[str] = []
        saw_move = False
        for name in sorted(changed):
            source = _safe_source_path(root, source_dir, name)
            backup = _safe_output_path(root, f"polished_markdown/{name}.footnote-sync.bak")
            if source is None or backup is None or not backup.is_file():
                return False
            old_hash = sha256_file(backup)
            if old_hash != baseline["source_sha256"].get(name):
                return False
            current_hash = sha256_file(source)
            applied_row = applied_files.get(name, {})
            polished_target = _safe_output_path(root, f"polished_markdown/{name}")
            if (polished_target is None or not polished_target.is_file()
                    or applied_row.get("target_sha256") != sha256_file(polished_target)
                    or applied_row.get("to_source_sha256") != receipt.get("target_sha256")
                    or _format_identity(source) != _format_identity(polished_target)):
                return False
            hints = applied_row.get("hints")
            if not isinstance(hints, list) or not hints:
                return False
            old_text = backup.read_text(encoding="utf-8")
            new_text = old_text
            for row in hints:
                if not isinstance(row, Mapping) or row.get("status") != "synchronized":
                    return False
                if row.get("to_version") not in {t.get("to_version") for t in chain}:
                    return False
                before, after = str(row.get("old_block") or ""), str(row.get("new_block") or "")
                old_notes, old_body_parts = _footnote_block_parts(before)
                new_notes, new_body_parts = _footnote_block_parts(after)
                old_payloads.extend(old_notes)
                new_payloads.extend(new_notes)
                if not before.startswith("\0"):
                    old_body.extend(old_body_parts)
                if not after.startswith("\0"):
                    new_body.extend(new_body_parts)
                if old_notes or new_notes:
                    saw_move = True
                new_text, status = _exact_block_edit(new_text, before, after)
                if status not in {"applied", "already_applied"}:
                    return False
            if new_text != source.read_text(encoding="utf-8"):
                return False
        # Retained body paragraphs must stay in order. Removed body paragraphs
        # become the only candidates that may be folded into a note payload.
        moved_body: list[str] = []
        new_index = 0
        for paragraph in old_body:
            if new_index < len(new_body) and paragraph == new_body[new_index]:
                new_index += 1
            else:
                moved_body.append(paragraph)
        if new_index != len(new_body):
            return False
        if not saw_move or not _same_footnote_coverage(old_payloads, new_payloads, moved_body):
            return False
        entities = _load_entities(root)
        if entities is None:
            return False
        entities.setdefault("metadata", {})["extraction_snapshot_id"] = snapshot_id
        entities["metadata"]["extraction_complete"] = True
        atomic_write_json(entity_path, entities)
        accepted = _store_accepted_entities(root, entity_path)
        if not accepted:
            return False
        atomic_write_json(root / ENTITY_BASELINE_FILE, {
            "schema_version": 1, "origin": "validated", "reuse_reason": "verified_footnote_relocation",
            "source_dir": manifest["source_dir"], "source_stage": manifest["source_stage"],
            "source_language": manifest["source_language"], "target_language": manifest["target_language"],
            "source_sha256": dict(raw), "semantic_source_sha256": dict(semantic),
            "entity_snapshot_id": snapshot_id, "entity_sha256": sha256_file(entity_path),
            **accepted,
            "footnote_version_id": current_version,
        })
        return True
    except (OSError, UnicodeError, ValueError, TypeError, KeyError):
        return False


def _migrate_legacy_baseline(output_dir: Path, source_dir: Path, source_stage: str,
                             source_language: str, target_language: str) -> dict[str, Any] | None:
    """Capture only an already validated legacy output, before prepare replaces its manifest."""
    path = output_dir / ENTITY_BASELINE_FILE
    existing = _read_json(path)
    if existing:
        return existing
    manifest = _read_json(output_dir / "entity_subagent_manifest.json")
    validation = _read_json(output_dir / "translation_entities_validation.json")
    entity_path = output_dir / "translation_entities.json"
    if not manifest or not validation or validation.get("valid") is not True or not entity_path.is_file():
        return None
    output_hash = sha256_file(entity_path)
    if validation.get("entity_sha256") != output_hash:
        return None
    old_hashes = manifest.get("source_sha256")
    if not isinstance(old_hashes, dict) or not old_hashes:
        return None
    current_raw, current_semantic, _, _ = _inventory(output_dir, source_dir, source_stage)
    # For legacy changed files the prior semantic body is unknowable. Retain raw
    # hashes for localization and semantic identities only where exact bytes match.
    semantic = {name: current_semantic[name] for name, digest in old_hashes.items()
                if name in current_raw and current_raw[name] == digest}
    accepted = _store_accepted_entities(output_dir, entity_path)
    if not accepted:
        return None
    baseline = {
        "schema_version": 1,
        "origin": "legacy_validated",
        "legacy_semantic_coverage_unverified": True,
        "source_dir": manifest.get("source_dir"),
        "source_stage": manifest.get("source_stage", source_stage),
        "source_language": manifest.get("source_language"),
        "target_language": manifest.get("target_language"),
        "source_sha256": dict(old_hashes),
        "semantic_source_sha256": semantic,
        "entity_sha256": output_hash,
        "entity_snapshot_id": None,
        **accepted,
    }
    atomic_write_json(path, baseline)
    return baseline


def prepare_entity_subagent_task(output_dir: Path, book_title: str, source_dir: Path, source_stage: str,
                                 source_language: str, target_language: str, config: dict) -> Path:
    """Prepare an incremental hand-off without advancing the completed baseline."""
    from .subagent_runtime import resolve_subagent_model
    from .entity_extractor import create_entity_extraction_prompt, create_entity_template

    output_dir, source_dir = Path(output_dir), Path(source_dir)
    source_files, excluded_files = entity_source_selection(source_dir, source_stage)
    if not source_files:
        raise ValueError(f"No eligible Markdown source found in {source_dir}; all units were excluded")
    try:
        relative_source_dir = source_dir.resolve().relative_to(output_dir.resolve()).as_posix()
    except ValueError as exc:
        raise ValueError("entity source directory is outside the book output directory") from exc
    output_dir.mkdir(parents=True, exist_ok=True)
    old_baseline = _migrate_legacy_baseline(output_dir, source_dir, source_stage, source_language, target_language)
    raw, semantic, names, excluded_names = _inventory(output_dir, source_dir, source_stage)
    snapshot_id = _snapshot_id(relative_source_dir, source_stage, source_language, target_language, semantic)
    entities = _load_entities(output_dir)
    prior_hashes = (old_baseline or {}).get("source_sha256", {})
    prior_semantic = (old_baseline or {}).get("semantic_source_sha256", {})
    baseline_matches_context = bool(old_baseline and old_baseline.get("source_dir") == relative_source_dir
                                   and old_baseline.get("source_stage", source_stage) == source_stage
                                   and old_baseline.get("source_language", source_language) == source_language
                                   and old_baseline.get("target_language", target_language) == target_language)
    relocation_reused = False
    if baseline_matches_context and old_baseline and old_baseline.get("origin") == "validated":
        proof_manifest = {"source_stage": source_stage, "source_dir": relative_source_dir,
                          "source_language": source_language, "target_language": target_language}
        if _verified_footnote_relocation_reuse(
            output_dir, source_dir, proof_manifest, old_baseline, raw, semantic, snapshot_id, config
        ):
            relocation_reused = True
            old_baseline = _read_json(output_dir / ENTITY_BASELINE_FILE)
            prior_hashes = (old_baseline or {}).get("source_sha256", {})
            prior_semantic = (old_baseline or {}).get("semantic_source_sha256", {})
            entities = _load_entities(output_dir)
    if old_baseline and old_baseline.get("entity_sha256"):
        entity_path = output_dir / "translation_entities.json"
        current_hash = sha256_file(entity_path) if entity_path.is_file() else None
        if current_hash != old_baseline.get("entity_sha256"):
            if entity_path.is_file():
                try:
                    raw_output = entity_path.read_bytes()
                    digest = hashlib.sha256(raw_output).hexdigest()
                    archived = _safe_output_path(output_dir, f"entity_unvalidated/{digest}.json")
                    if archived and not archived.exists():
                        atomic_write_text(archived, raw_output.decode("utf-8"))
                except (OSError, UnicodeError):
                    pass
            accepted_entities = _load_accepted_entities(output_dir, old_baseline)
            if accepted_entities is not None:
                entities = accepted_entities
                accepted_file = _safe_output_path(output_dir, str(old_baseline.get("accepted_file", "")))
                if accepted_file is not None:
                    try:
                        atomic_write_text(entity_path, accepted_file.read_bytes().decode("utf-8"))
                    except (OSError, UnicodeError):
                        pass
    if baseline_matches_context:
        pending = [name for name in names if name not in prior_hashes or
                   (prior_hashes.get(name) != raw[name] and prior_semantic.get(name) != semantic[name])]
        pending = sorted(set(pending))
        removed = sorted(set(prior_hashes) - set(raw))
    else:
        pending = list(names)
        removed = []
    legacy_exact = bool(old_baseline and old_baseline.get("origin") == "legacy_validated"
                        and baseline_matches_context and old_baseline.get("entity_sha256")
                        == (sha256_file(output_dir / "translation_entities.json")
                            if (output_dir / "translation_entities.json").is_file() else None)
                        and old_baseline.get("source_sha256") == raw and not pending and not removed)
    current_baseline = bool(old_baseline and old_baseline.get("entity_snapshot_id") == snapshot_id
                            and old_baseline.get("entity_sha256") ==
                            (sha256_file(output_dir / "translation_entities.json")
                             if (output_dir / "translation_entities.json").is_file() else None))
    footnote_receipt = _read_json(output_dir / "footnote_normalization.json")
    stable_relocation_reuse = bool(
        baseline_matches_context and old_baseline
        and old_baseline.get("reuse_reason") == "verified_footnote_relocation"
        and current_baseline and not pending and not removed
        and old_baseline.get("footnote_version_id")
        == (footnote_receipt or {}).get("version_id")
    )
    mode = "bootstrap" if not old_baseline else ("incremental_review" if pending or removed else "refresh")
    if legacy_exact or current_baseline:
        mode = "legacy_reuse" if legacy_exact else "refresh"
    template = create_entity_template(book_title, source_language, target_language, names)
    if entities:
        for key in template:
            if key not in {"metadata", "schema_version"} and isinstance(entities.get(key), list):
                template[key] = entities[key]
    template["metadata"].update({"extraction_snapshot_id": snapshot_id,
                                  "review_source_files": pending,
                                  "removed_source_files": removed,
                                  "extraction_mode": mode})
    if not pending and not removed:
        template["metadata"]["extraction_complete"] = True
    atomic_write_json(output_dir / "translation_entities.template.json", template)
    manifest = {
        "schema_version": 2, "workflow": "antigravity-subagent", "task": "extract-entities",
        "source_language": source_language, "target_language": target_language,
        "model": resolve_subagent_model(config, "extract-entities"), "source_dir": relative_source_dir,
        "source_stage": source_stage, "files": names, "excluded_files": excluded_names,
        "source_filter_version": ENTITY_SOURCE_FILTER_VERSION,
        "excluded_roles": sorted(EPUB_ENTITY_EXCLUDED_ROLES) if source_stage == "epub-compressed" else [],
        "source_sha256": raw, "semantic_source_sha256": semantic,
        "snapshot_id": snapshot_id, "pending_files": pending, "removed_files": removed, "mode": mode,
        "reuse_reason": "verified_footnote_relocation" if relocation_reused or stable_relocation_reuse else None,
        "baseline_origin": (old_baseline or {}).get("origin", "none"),
        "output_file": "translation_entities.json", "template_file": "translation_entities.template.json",
    }
    atomic_write_json(output_dir / "entity_subagent_manifest.json", manifest)
    context_files = _write_bounded_contexts(output_dir, source_dir, names, pending, removed)
    manifest["assigned_files"] = pending
    manifest["context_files"] = context_files
    manifest["context_sha256"] = {
        path: sha256_file(output_dir / path)
        for paths in context_files.values() for path in paths
    }
    atomic_write_json(output_dir / "entity_subagent_manifest.json", manifest)
    prompt = create_entity_extraction_prompt(book_title, (source_language, target_language), pending_files=pending,
                                              context_files=[path for paths in context_files.values() for path in paths],
                                              removed_files=removed,
                                              existing_entities=bool(entities), mode=mode)
    atomic_write_text(output_dir / "entity_subagent_prompt.md", prompt +
                      f'\n\nPrepared entity snapshot: `{snapshot_id}`\n'
                      f'Recommended Antigravity model: `{manifest["model"]}`\n')
    logger.info(f"Entity task prepared ({mode}; {len(pending)} units to review): {output_dir / 'entity_subagent_prompt.md'}")
    return output_dir / "translation_entities.json"


def validate_entity_source_manifest(manifest: Mapping[str, Any], output_dir: Path, source_dir: Path) -> list[str]:
    """Validate the exact prepared inventory and source snapshot."""
    output_dir, source_dir = Path(output_dir), Path(source_dir)
    try:
        expected_dir = source_dir.resolve().relative_to(output_dir.resolve()).as_posix()
    except ValueError:
        return ["entity source directory is outside the book output directory"]
    errors: list[str] = []
    if Path(str(manifest.get("source_dir") or "")).as_posix() != expected_dir:
        errors.append("entity source directory changed after extraction")
    if manifest.get("source_stage") == "epub-compressed" and manifest.get("source_filter_version") != ENTITY_SOURCE_FILTER_VERSION:
        errors.append("entity source filter version is stale")
    try:
        raw, semantic, names, excluded = _inventory(output_dir, source_dir, str(manifest.get("source_stage") or ""))
    except (OSError, UnicodeError, ValueError) as exc:
        return errors + [str(exc)]
    if set(manifest.get("files", [])) != set(names) or set(manifest.get("excluded_files", [])) != set(excluded):
        errors.append("entity source file set changed after extraction")
    if manifest.get("source_sha256") != raw:
        errors.append("entity source snapshot changed after extraction")
    if manifest.get("semantic_source_sha256") != semantic:
        errors.append("entity semantic source snapshot changed after extraction")
    pending = manifest.get("pending_files", [])
    if not isinstance(pending, list) or manifest.get("assigned_files") != pending or not set(pending).issubset(names):
        errors.append("entity review assignment is invalid")
    context_files = manifest.get("context_files", {})
    context_hashes = manifest.get("context_sha256", {})
    if not isinstance(context_files, Mapping) or not isinstance(context_hashes, Mapping):
        errors.append("entity bounded context manifest is invalid")
    else:
        for relative in (path for paths in context_files.values() if isinstance(paths, list) for path in paths):
            safe = _safe_output_path(output_dir, str(relative))
            if safe is None or not safe.is_file() or context_hashes.get(relative) != sha256_file(safe):
                errors.append(f"entity bounded context changed or escapes workspace: {relative}")
    return errors


def record_completed_entity_snapshot(output_dir: Path, manifest: Mapping[str, Any], entity_path: Path) -> None:
    """Advance the completed baseline after output and prepared snapshot validate."""
    completed = {
        "schema_version": 1, "origin": "validated", "source_dir": manifest["source_dir"],
        "source_stage": manifest["source_stage"], "source_language": manifest["source_language"],
        "target_language": manifest["target_language"], "source_sha256": manifest["source_sha256"],
        "semantic_source_sha256": manifest["semantic_source_sha256"],
        "entity_snapshot_id": manifest["snapshot_id"], "entity_sha256": sha256_file(entity_path),
    }
    accepted = _store_accepted_entities(output_dir, entity_path)
    if not accepted:
        raise ValueError("could not preserve the validated entity output as a completed snapshot")
    completed.update(accepted)
    footnote_receipt = _read_json(Path(output_dir) / "footnote_normalization.json")
    if footnote_receipt and footnote_receipt.get("valid") is True and footnote_receipt.get("status") == "validated":
        version_id = footnote_receipt.get("version_id")
        if not version_id:
            from .refine.footnote_versions import _version_id

            version_id = _version_id(footnote_receipt)
        completed["footnote_version_id"] = version_id
    atomic_write_json(Path(output_dir) / ENTITY_BASELINE_FILE, completed)


def entity_context_status(output_dir: Path, source_dir: Path, source_language: str | None = None,
                          target_language: str | None = None) -> tuple[bool, str]:
    """Return currentness and an operator-facing refresh/review reason."""
    output_dir, source_dir = Path(output_dir), Path(source_dir)
    entity_path = output_dir / "translation_entities.json"
    manifest = _read_json(output_dir / "entity_subagent_manifest.json")
    baseline = _read_json(output_dir / ENTITY_BASELINE_FILE)
    if not entity_path.is_file() or manifest is None or baseline is None:
        return False, "entity baseline or validated output is missing; prepare incremental/bootstrap review"
    try:
        from .entity_extractor import validate_entities
        data = _read_json(entity_path)
        if data is None or validate_entities(data, source_language=source_language, target_language=target_language):
            return False, "entity output is invalid"
        if validate_entity_source_manifest(manifest, output_dir, source_dir):
            return False, "entity source changed after preparation; rerun extract-entities"
        digest = sha256_file(entity_path)
        if baseline.get("entity_sha256") != digest:
            return False, "entity output changed after its completed validation"
        if _load_accepted_entities(output_dir, baseline) is None:
            return False, "completed entity snapshot is missing or corrupt"
        if baseline.get("origin") == "legacy_validated":
            if manifest.get("mode") == "legacy_reuse" and baseline.get("source_sha256") == manifest.get("source_sha256"):
                return True, "legacy validated entity baseline reused"
            return False, "legacy entity baseline requires incremental review"
        if data.get("metadata", {}).get("extraction_snapshot_id") != manifest.get("snapshot_id"):
            return False, "entity output is not bound to the prepared source snapshot"
        if baseline.get("entity_snapshot_id") != manifest.get("snapshot_id"):
            return False, "entity output has not been validated for the prepared source snapshot"
        return True, "entity glossary matches the completed source snapshot"
    except (OSError, UnicodeError, TypeError, ValueError):
        return False, "entity source or output snapshot is invalid"


def entity_context_is_current(output_dir: Path, source_dir: Path, source_language: str | None = None,
                              target_language: str | None = None) -> bool:
    return entity_context_status(output_dir, source_dir, source_language, target_language)[0]


__all__ = ["ENTITY_BASELINE_FILE", "ENTITY_SOURCE_FILTER_VERSION", "EPUB_ENTITY_EXCLUDED_ROLES",
           "entity_context_is_current", "entity_context_status", "entity_source_selection",
           "prepare_entity_subagent_task", "record_completed_entity_snapshot", "validate_entity_source_manifest"]
