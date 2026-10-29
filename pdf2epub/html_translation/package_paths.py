"""Safe path and XML primitives for package navigation documents."""
from __future__ import annotations
import re
from pathlib import Path, PurePosixPath
from typing import Any, Mapping, Optional
from urllib.parse import unquote_to_bytes, urlsplit
from lxml import etree as LET

class UnsafePackagePath(ValueError):
    """Raised when an EPUB metadata path escapes its extracted package."""

def package_relative_href(extract_dir: Path, base_dir: Path, href: str) -> Optional[str]:
    """Normalize a local EPUB href without treating its fragment as a path.

    ``Path.relative_to`` is sensitive to Windows path spelling, including
    8.3 aliases.  Resolve both sides before comparing and append the URI
    fragment only after the filesystem-relative path has been computed.
    """
    if not isinstance(href, str) or not href or "\\" in href or "\x00" in href:
        return None
    try:
        uri = urlsplit(href)
        if uri.scheme or uri.netloc or uri.path.startswith("/"):
            return None
        if re.search(r"%(?![0-9a-fA-F]{2})", uri.path):
            return None
        path_part = unquote_to_bytes(uri.path).decode("utf-8", errors="strict")
    except (UnicodeDecodeError, ValueError):
        return None
    if "\\" in path_part or "\x00" in path_part:
        return None
    if not path_part:
        candidate = Path(base_dir).resolve()
    else:
        posix_path = PurePosixPath(path_part)
        if posix_path.is_absolute():
            return None
        candidate = (Path(base_dir) / Path(*posix_path.parts)).resolve()
    try:
        relative = candidate.relative_to(Path(extract_dir).resolve()).as_posix()
    except ValueError:
        return None
    return relative + (f"#{uri.fragment}" if uri.fragment else "")

def safe_package_path(
    package_root: Path,
    value: str,
    *,
    base: Optional[Path] = None,
    label: str = "EPUB path",
) -> Path:
    """Resolve an EPUB metadata path while keeping it inside ``package_root``.

    EPUB paths use POSIX separators even on Windows.  We still resolve with
    the host platform's ``Path`` so drive-qualified and backslash paths are
    rejected by the final containment check as well.
    """
    if not isinstance(value, str) or not value or "\x00" in value:
        raise UnsafePackagePath(f"Invalid {label}: {value!r}")

    root = Path(package_root).resolve()
    parent = Path(base or root).resolve()
    try:
        parent.relative_to(root)
    except ValueError as exc:
        raise UnsafePackagePath(f"{label} base escapes extracted EPUB: {parent}") from exc

    posix_value = PurePosixPath(value)
    if posix_value.is_absolute():
        raise UnsafePackagePath(f"Absolute {label} is not allowed: {value!r}")

    candidate = (parent / Path(*posix_value.parts)).resolve()
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise UnsafePackagePath(f"{label} escapes extracted EPUB: {value!r}") from exc
    return candidate

def safe_existing_package_file(package_root: Path, candidate: Path) -> Optional[Path]:
    """Return an existing file only when its resolved path stays in the package."""
    root = Path(package_root).resolve()
    resolved = Path(candidate).resolve()
    try:
        resolved.relative_to(root)
    except ValueError:
        return None
    return resolved if resolved.is_file() else None

def safe_lxml_parser() -> LET.XMLParser:
    """Parse untrusted package XML without external entities or network I/O."""
    return LET.XMLParser(
        resolve_entities=False,
        load_dtd=False,
        no_network=True,
        huge_tree=False,
    )

def has_local_tag(element: Any, local_name: str) -> bool:
    """Return whether an XML element has the requested local tag name."""
    tag = getattr(element, "tag", None)
    return isinstance(tag, str) and (
        tag == local_name or tag.endswith(f"}}{local_name}")
    )

def toc_entry_href(entry: Mapping[str, Any]) -> str:
    """Return a TOC entry href in the same form used by EPUB navigation.

    Metadata hand-offs intentionally store the file path and fragment in
    separate fields.  Navigation documents, however, use the combined
    ``path#fragment`` form, so all matching must use one canonical key.
    """
    href = str(entry.get("href") or "")
    anchor = str(entry.get("anchor") or "").lstrip("#")
    if anchor:
        href = f"{href.split('#', 1)[0]}#{anchor}"
    return href


# Compatibility aliases for the former private helper names.
_package_relative_href = package_relative_href
_safe_package_path = safe_package_path
_safe_existing_package_file = safe_existing_package_file
_safe_lxml_parser = safe_lxml_parser
_has_local_tag = has_local_tag
_toc_entry_href = toc_entry_href
