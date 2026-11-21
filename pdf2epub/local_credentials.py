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


class LocalCredentialsError(RuntimeError):
    """Raised when a credential file exists but cannot be read or parsed.

    A missing file is a supported "no local credentials" state; a corrupt or
    unreadable file is not, because silently treating it as absent turns a
    configuration mistake into repeated provider 401s.
    """


def read_json_file(path: Path) -> Optional[dict[str, Any]]:
    """Read a credential JSON object.

    Returns ``None`` only when the file does not exist. Unreadable files,
    invalid JSON, and non-object roots raise :class:`LocalCredentialsError`.
    """
    resolved = Path(path)
    try:
        text = resolved.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except (OSError, UnicodeError) as exc:
        raise LocalCredentialsError(
            f"cannot read local credentials file {resolved}: {exc}"
        ) from exc
    try:
        value = json.loads(text)
    except json.JSONDecodeError as exc:
        raise LocalCredentialsError(
            f"local credentials file is not valid JSON: {resolved}: {exc}"
        ) from exc
    if not isinstance(value, dict):
        raise LocalCredentialsError(
            f"local credentials file must contain a JSON object: {resolved}"
        )
    return value


def read_local_json(
    config: Optional[Mapping[str, Any]], filename: str
) -> Optional[dict[str, Any]]:
    """Read a local JSON credential object without logging its contents."""
    return read_json_file(local_credential_path(config, filename))


__all__ = [
    "DEFAULT_LOCAL_CREDENTIALS_DIR",
    "LocalCredentialsError",
    "local_credential_path",
    "local_credentials_dir",
    "read_json_file",
    "read_local_json",
]
