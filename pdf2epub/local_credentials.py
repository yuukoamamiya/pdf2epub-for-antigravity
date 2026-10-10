"""Local, git-ignored credential files for OCR providers.

Secrets are intentionally kept outside YAML configuration and checkpoints.
The default directory is ``.secrets`` in the current repository. A config may
override it with ``credentials.local_dir``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping, Optional


_DEFAULT_LOCAL_DIR_NAME = ".secrets"
# Keep the historical public constant for callers, but make it explicit that
# this is a directory name rather than a secret value.
DEFAULT_LOCAL_CREDENTIALS_DIR = Path(_DEFAULT_LOCAL_DIR_NAME).name


def local_credentials_dir(config: Optional[Mapping[str, Any]] = None) -> Path:
    """Return the configured local credential directory."""
    credentials = config.get("credentials", {}) if isinstance(config, Mapping) else {}
    raw_dir = (
        credentials.get("local_dir", DEFAULT_LOCAL_CREDENTIALS_DIR)
        if isinstance(credentials, Mapping)
        else DEFAULT_LOCAL_CREDENTIALS_DIR
    )
    path = Path(str(raw_dir or DEFAULT_LOCAL_CREDENTIALS_DIR)).expanduser()
    return path if path.is_absolute() else Path.cwd() / path


def local_credential_path(
    config: Optional[Mapping[str, Any]], filename: str
) -> Path:
    """Resolve one file below the local credential directory."""
    candidate = Path(str(filename or "")).expanduser()
    if candidate.is_absolute() or ".." in candidate.parts:
        raise ValueError("local credential filename must stay inside credentials.local_dir")
    return local_credentials_dir(config) / candidate


def read_local_json(
    config: Optional[Mapping[str, Any]], filename: str
) -> Optional[dict[str, Any]]:
    """Read a local JSON credential object without logging its contents."""
    path = local_credential_path(config, filename)
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


__all__ = [
    "DEFAULT_LOCAL_CREDENTIALS_DIR",
    "local_credential_path",
    "local_credentials_dir",
    "read_local_json",
]
