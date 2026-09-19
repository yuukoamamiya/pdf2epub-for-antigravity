"""Shared file-based workspace task contracts.

Translation itself remains outside this package.  These helpers centralize
the deterministic part shared by PDF, EPUB, novel, and TeX hand-offs:
loading JSON reports, hashing source material, and deciding whether a target
is a safe resumable checkpoint.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any, Dict, Iterable, Mapping, Optional, Tuple


def sha256_file(path: Path) -> str:
    """Return the SHA-256 digest of a workspace file."""
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def atomic_write_text(path: Path, content: str, *, encoding: str = "utf-8") -> None:
    """Write text through a sibling temporary file and replace the target atomically."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{target.name}.", suffix=".tmp", dir=target.parent
    )
    try:
        with os.fdopen(descriptor, "w", encoding=encoding, newline="") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, target)
    except BaseException:
        try:
            os.unlink(temporary_name)
        except OSError:
            pass
        raise


def load_json_object(path: Path, *, default: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Load a JSON object, returning ``default`` for missing/invalid files."""
    fallback = dict(default or {})
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return fallback
    return value if isinstance(value, dict) else fallback


def validated_checkpoint_data(
    report: Mapping[str, Any],
    *,
    valid_key: str = "valid_files",
    hash_key: str = "source_sha256",
) -> Tuple[set[str], Dict[str, str]]:
    """Normalize the valid names and source hashes from a validation report."""
    valid_values = report.get(valid_key, [])
    if not isinstance(valid_values, (list, tuple, set)):
        valid_values = []
    valid_names = {
        str(value)
        for value in valid_values
        if value is not None and str(value).strip()
    }
    hashes = report.get(hash_key, {})
    if not isinstance(hashes, Mapping):
        hashes = {}
    normalized_hashes = {
        str(name): str(value)
        for name, value in hashes.items()
        if name is not None and value is not None and str(name).strip() and str(value).strip()
    }
    return valid_names, normalized_hashes


def is_reusable_checkpoint(
    target_path: Path,
    item_id: str,
    source_sha256: str,
    validated_ids: Iterable[str],
    recorded_hashes: Mapping[str, str],
) -> bool:
    """Return whether a target is non-empty and backed by matching validation.

    A target file by itself is never considered complete.  This invariant is
    shared by all workspace Subagent workflows so interrupted writes cannot
    become resume checkpoints.
    """
    target = Path(target_path)
    if (
        not target.is_file()
        or str(item_id) not in set(validated_ids)
        or recorded_hashes.get(str(item_id)) != source_sha256
    ):
        return False
    try:
        return bool(target.read_bytes().decode("utf-8").strip())
    except (OSError, UnicodeError):
        # A partially written or truncated target is never a resumable
        # checkpoint, even if an old validation report says it was valid.
        return False
