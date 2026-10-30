"""Pure construction of compact terminology contexts for worker assignments."""
from __future__ import annotations

import json
from typing import Any, Dict, List, Mapping


def compact_glossary_entry(entry: Mapping[str, Any]) -> Dict[str, Any]:
    """Keep the source/target fields needed by a worker, dropping audit data."""
    compact: Dict[str, Any] = {}
    for key in (
        "source", "original", "target", "kind", "policy", "allow_short",
        "variants", "aliases", "aliases_by_language",
    ):
        value = entry.get(key)
        if value in (None, "", [], False):
            continue
        compact[key] = value
    note = entry.get("note")
    if isinstance(note, str) and note.strip() and len(note) <= 320:
        compact["note"] = note.strip()
    return compact


def build_worker_glossary_context(
    context_entries: Mapping[str, List[Mapping[str, Any]]],
    *,
    manifest: Mapping[str, Any],
    group: Mapping[str, Any],
    worker_id: str,
    files: List[str],
    chapter_mode: bool,
    chapter_pack: bool,
) -> Dict[str, Any] | None:
    """Build a worker context payload from already-loaded unit entries.

    This function has no filesystem access. The caller owns loading source
    contexts and persisting the returned payload.
    """
    file_entries: Dict[str, List[Dict[str, Any]]] = {}
    entry_by_key: Dict[str, Dict[str, Any]] = {}
    entry_files: Dict[str, set[str]] = {}
    chapter_entries: List[Dict[str, Any]] = []
    seen_chapter_entries: set[str] = set()

    for filename, raw_entries in context_entries.items():
        entries: List[Dict[str, Any]] = []
        seen_entries: set[str] = set()
        for entry in raw_entries:
            if not isinstance(entry, Mapping):
                continue
            key = json.dumps(
                entry, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            )
            if key in seen_entries:
                continue
            seen_entries.add(key)
            value = dict(entry)
            entries.append(value)
            entry_by_key[key] = value
            entry_files.setdefault(key, set()).add(filename)
            if key not in seen_chapter_entries:
                seen_chapter_entries.add(key)
                chapter_entries.append(value)
        if not chapter_mode or chapter_pack:
            file_entries[filename] = entries

    if chapter_pack:
        has_context = any(file_entries.values())
    elif chapter_mode and group.get("chapter_split"):
        has_context = any(set(files) & names for names in entry_files.values())
    elif chapter_mode:
        has_context = bool(chapter_entries)
    else:
        has_context = any(file_entries.values())
    if not has_context:
        return None

    shared_entries: List[Dict[str, Any]] = []
    local_entries: List[Dict[str, Any]] = []
    if chapter_pack:
        file_entries = {
            filename: [compact_glossary_entry(entry) for entry in entries]
            for filename, entries in file_entries.items()
        }
    elif chapter_mode:
        from .glossary import sort_glossary_entries

        if group.get("chapter_split"):
            assigned_set = set(files)
            shared_keys = {
                key
                for key, names in entry_files.items()
                if len(names) > 1 and names & assigned_set
            }
            shared_entries = [
                compact_glossary_entry(entry)
                for entry in sort_glossary_entries(
                    [entry_by_key[key] for key in shared_keys]
                )
            ]
            local_entries = [
                compact_glossary_entry(entry)
                for entry in sort_glossary_entries(
                    [
                        entry_by_key[key]
                        for key, names in entry_files.items()
                        if names & assigned_set and key not in shared_keys
                    ]
                )
            ]
            has_context = bool(shared_entries or local_entries)
        else:
            chapter_entries = [
                compact_glossary_entry(entry)
                for entry in sort_glossary_entries(chapter_entries)
            ]
    else:
        file_entries = {
            filename: [compact_glossary_entry(entry) for entry in entries]
            for filename, entries in file_entries.items()
        }

    if not has_context:
        return None
    if chapter_pack:
        return {
            "schema_version": 4,
            "worker_id": worker_id,
            "selection": "adjacent_chapter_direct_file_contexts",
            "chapter_ids": group.get("chapter_ids", []),
            "chapter_file_counts": group.get("chapter_file_counts", {}),
            "files": file_entries,
        }
    if chapter_mode and group.get("chapter_split"):
        return {
            "schema_version": 3,
            "worker_id": worker_id,
            "selection": "chapter_shared_local_direct_context",
            "chapter_id": group.get("chapter_id"),
            "chapter_file_count": group.get("chapter_file_count", len(files)),
            "assigned_files": files,
            "shared_entries": shared_entries,
            "local_entries": local_entries,
        }
    if chapter_mode:
        return {
            "schema_version": 2,
            "worker_id": worker_id,
            "selection": "chapter_sparse_direct_context",
            "chapter_id": group.get("chapter_id"),
            "chapter_file_count": group.get("chapter_file_count", len(files)),
            "assigned_files": files,
            "entries": chapter_entries,
        }
    return {
        "schema_version": 1,
        "worker_id": worker_id,
        "selection": "worker_sparse_direct_file_contexts",
        "files": file_entries,
    }
