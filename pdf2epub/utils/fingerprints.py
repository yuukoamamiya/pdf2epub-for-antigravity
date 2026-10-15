"""Stable fingerprints for workflow input artifacts."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Optional


def pages_fingerprint(pages_dir: Path) -> Optional[str]:
    """Hash page Markdown and layout sidecars used by refinement.

    Refinement checkpoints and packaging receipts must attest to the same
    inputs.  Keep the file selection and byte ordering in one place so a
    native-text sidecar cannot be included by one stage and omitted by
    another.
    """
    pages_dir = Path(pages_dir)
    if not pages_dir.is_dir():
        return None

    pages = sorted(
        [*pages_dir.glob("page_*.md"), *pages_dir.glob("page_*.ocr.json")]
    )
    if not pages:
        return None

    digest = hashlib.sha256()
    try:
        for page in pages:
            digest.update(page.name.encode("utf-8"))
            digest.update(hashlib.sha256(page.read_bytes()).digest())
    except OSError:
        return None
    return digest.hexdigest()


__all__ = ["pages_fingerprint"]
