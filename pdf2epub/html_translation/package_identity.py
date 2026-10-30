"""Stable identity and path validation for EPUB package documents."""
from __future__ import annotations

import hashlib
import posixpath
import re
from pathlib import PurePosixPath, PureWindowsPath


MAPPING_SCHEMA_VERSION = 2
PACKAGE_PATH_MANIFEST = ".epub_package_paths.json"


def validate_package_relative_path(value: object) -> str:
    """Validate a decoded, normalized EPUB ZIP member path."""
    if not isinstance(value, str) or not value:
        raise ValueError("package_relative_path must be a non-empty string")
    if "\x00" in value or "\\" in value or value.startswith("/"):
        raise ValueError(f"unsafe package-relative path: {value!r}")
    path = PurePosixPath(value)
    windows_path = PureWindowsPath(value)
    if path.is_absolute() or windows_path.is_absolute() or windows_path.drive:
        raise ValueError(f"unsafe package-relative path: {value!r}")
    parts = value.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise ValueError(f"unsafe package-relative path: {value!r}")
    if any(any(character in '<>:"|?*' for character in part) for part in parts):
        raise ValueError(f"package path is not portable to Windows: {value!r}")
    if any(part.endswith((".", " ")) or PureWindowsPath(part).is_reserved() for part in parts):
        raise ValueError(f"package path would be normalized by Windows: {value!r}")
    if posixpath.normpath(value) != value:
        raise ValueError(f"package-relative path is not normalized: {value!r}")
    return value


def unit_id_for_package_path(package_path: object) -> str:
    """Make a stable unit basename unique to the full package member path."""
    normalized = validate_package_relative_path(package_path)
    stem = PurePosixPath(normalized).stem
    slug = re.sub(r"[^\w.-]+", "_", stem, flags=re.UNICODE).strip("._")
    slug = (slug or "unit")[:48]
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
    return f"{slug}--{digest}"
