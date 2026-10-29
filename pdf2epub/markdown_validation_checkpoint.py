"""Filesystem checkpoint operations for Markdown validation retries."""
from __future__ import annotations
import hashlib
import shutil
from pathlib import Path
from typing import Iterable

def discard_abandoned_temp_targets(
    target_dir: Path, source_names: Iterable[str]
) -> list[str]:
    """Remove only the temp siblings named by the atomic-output contract."""
    discarded: list[str] = []
    target_dir = Path(target_dir)
    for name in source_names:
        target = target_dir / str(name)
        candidates = [
            target_dir / f".{target.name}.tmp",
            *target_dir.glob(f".{target.name}.*.tmp"),
        ]
        for candidate in candidates:
            if not candidate.is_file():
                continue
            try:
                candidate.unlink()
            except OSError:
                continue
            discarded.append(candidate.name)
    return sorted(set(discarded))


def quarantine_invalid_targets(
    output_dir: Path,
    task: str,
    target_dir: Path,
    invalid_names: Iterable[str],
) -> dict[str, str]:
    """Move invalid outputs to a recoverable scratch area before retry.

    A validator failure must not leave a half-written file looking like an
    active translation output. The original bytes remain available under the
    task scratch directory for diagnosis, while the hand-off target becomes
    safely pending on the next ``--resume``.
    """
    quarantine_dir = Path(output_dir) / "scratch" / task / "invalid_outputs"
    moved: dict[str, str] = {}
    for raw_name in sorted({str(name) for name in invalid_names if str(name).strip()}):
        if Path(raw_name).name != raw_name or Path(raw_name).suffix.lower() != ".md":
            continue
        target = Path(target_dir) / raw_name
        if not target.is_file():
            continue
        quarantine_dir.mkdir(parents=True, exist_ok=True)
        destination = quarantine_dir / raw_name
        if destination.exists():
            digest = hashlib.sha256(target.read_bytes()).hexdigest()[:12]
            destination = quarantine_dir / f"{Path(raw_name).stem}.{digest}.md"
        try:
            shutil.move(str(target), str(destination))
        except OSError:
            continue
        moved[raw_name] = destination.relative_to(output_dir).as_posix()
    return moved
