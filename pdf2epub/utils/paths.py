"""Filesystem path helpers that do not depend on workflow policy."""

from pathlib import Path
from typing import Union


def sanitize_filename(filename: str) -> str:
    if not isinstance(filename, str):
        filename = str(filename)
    sanitized = filename.replace("\x00", "_")
    sanitized = "".join("_" if c in '<>:\"/\\|?*' else c for c in sanitized)
    sanitized = " ".join(sanitized.split()).strip(" .")
    reserved = {"CON", "PRN", "AUX", "NUL"}
    reserved.update(f"COM{i}" for i in range(1, 10))
    reserved.update(f"LPT{i}" for i in range(1, 10))
    if sanitized.split(".", 1)[0].upper() in reserved:
        sanitized = f"_{sanitized}"
    return sanitized[:200].rstrip(" .") or "untitled"


def book_output_dir(book_title: str, root: Union[str, Path] = "output") -> Path:
    root_path = Path(root)
    resolved_root = root_path.resolve()
    candidate = root_path / sanitize_filename(book_title)
    try:
        candidate.resolve().relative_to(resolved_root)
    except ValueError as exc:
        raise ValueError("Book title escapes the output directory") from exc
    return candidate
