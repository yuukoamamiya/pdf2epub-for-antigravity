#!/usr/bin/env python3
"""Create a sanitized export copy without changing the source tree."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
import uuid
from pathlib import Path

import yaml


TEXT_SUFFIXES = {".json", ".txt", ".log", ".yaml", ".yml", ".md", ".csv", ".tsv"}
EXCLUDED_NAMES = {".DS_Store", "input.epub", "input_original.pdf"}
API_KEY_PATTERN = re.compile(
    r"(?i)(\bapi[_-]?key\b\s*[\"']?\s*[:=]\s*[\"']?)"
    r"([A-Za-z0-9_.-]{20,}|[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})"
)
AUTHORIZATION_PATTERN = re.compile(
    r"(?i)(\b(?:authorization|bearer)\b\s*[\"']?\s*[:=]?\s*[\"']?\s*)"
    r"([A-Za-z0-9_.-]{20,})"
)
BEARER_PATTERN = re.compile(r"(?i)(\bBearer\s+)([A-Za-z0-9_.-]{20,})")
KEY_TOKEN_PATTERN = re.compile(r"\bsk-(?:ant-)?[A-Za-z0-9_-]{20,}\b")
PROXY_URL_PATTERN = re.compile(
    r"https?://[A-Za-z0-9_-]*\.(?:shenshei\.fans|zzhou\.info)[^\s\"'<> ,{}\[\]]*",
    re.IGNORECASE,
)
LOCAL_PATH_PATTERN = re.compile(
    r"(?:/Users/|/home/|/root/)[^\s\"'<> ,{}\[\]]+|C:(?:\\\\|\\)Users(?:\\\\|\\)[^\s\"'<> ,{}\[\]]+",
    re.IGNORECASE,
)


class ExportError(Exception):
    """A validation or export error safe to show to the caller."""


def _is_reparse_point(path: Path) -> bool:
    if path.is_symlink():
        return True
    is_junction = getattr(os.path, "isjunction", None)
    return bool(is_junction and is_junction(path))


def _resolve_source(raw: Path) -> Path:
    if _is_reparse_point(raw):
        raise ExportError(f"Source directory is a symlink or junction: {raw}")
    try:
        source = raw.resolve(strict=True)
    except OSError as exc:
        raise ExportError(f"Cannot resolve source directory: {raw}") from exc
    if not source.is_dir():
        raise ExportError(f"Source is not a directory: {source}")
    return source


def _resolve_destination(raw: Path) -> Path:
    if _is_reparse_point(raw):
        raise ExportError(f"Destination is a symlink or junction: {raw}")
    try:
        destination = raw.resolve(strict=False)
    except OSError as exc:
        raise ExportError(f"Cannot resolve destination directory: {raw}") from exc
    if destination.exists() or destination.is_symlink():
        raise ExportError(f"Destination already exists; choose a fresh path: {destination}")
    return destination


def _validate_tree(source: Path) -> None:
    """Reject links/reparse points so traversal cannot escape the supplied tree."""
    def fail_on_walk_error(error: OSError) -> None:
        raise ExportError(f"Cannot inspect source tree at {error.filename or source}") from error

    for root, directories, files in os.walk(
        source, topdown=True, followlinks=False, onerror=fail_on_walk_error
    ):
        root_path = Path(root)
        _assert_within_source(root_path, source)
        for name in [*directories, *files]:
            candidate = root_path / name
            if _is_reparse_point(candidate):
                raise ExportError(f"Source contains a symlink or junction; refusing external data: {candidate}")
            _assert_within_source(candidate, source)
            if candidate.exists() and not (candidate.is_dir() or candidate.is_file()):
                raise ExportError(f"Unsupported non-regular source entry: {candidate}")


def _assert_within_source(path: Path, source: Path) -> Path:
    try:
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise ExportError(f"Cannot resolve source entry: {path}") from exc
    if resolved != source and source not in resolved.parents:
        raise ExportError(f"Source entry resolves outside the source directory: {path}")
    return resolved


def _is_excluded(relative: Path) -> bool:
    name = relative.name
    return (
        name in EXCLUDED_NAMES
        or name == "config.yaml"
        or (name.startswith("config_") and name.endswith(".yaml"))
    )


def _redact_text(text: str) -> str:
    # This text-level pass is used for unstructured UTF-8 formats.
    text = API_KEY_PATTERN.sub(r"\1[REDACTED]", text)
    text = AUTHORIZATION_PATTERN.sub(r"\1[REDACTED]", text)
    text = BEARER_PATTERN.sub(r"\1[REDACTED]", text)
    text = KEY_TOKEN_PATTERN.sub("[REDACTED]", text)
    text = PROXY_URL_PATTERN.sub("[REDACTED_URL]", text)
    return LOCAL_PATH_PATTERN.sub("[REDACTED_PATH]", text)


def _is_sensitive_key(key: object) -> bool:
    if not isinstance(key, str):
        return False
    normalized = re.sub(r"[^a-z0-9]", "", key.lower())
    suffixes = (
        "apikey",
        "authorization",
        "authtoken",
        "accesstoken",
        "refreshtoken",
        "clientsecret",
        "apisecret",
        "secret",
        "token",
    )
    return normalized.endswith(suffixes)


def _redact_value(
    value: object,
    memo: dict[int, object] | None = None,
    sensitive_context: bool = False,
) -> object:
    if memo is None:
        memo = {}
    if isinstance(value, str):
        return "[REDACTED]" if sensitive_context else _redact_text(value)
    if isinstance(value, list):
        if id(value) in memo:
            return memo[id(value)]
        cleaned: list[object] = []
        memo[id(value)] = cleaned
        cleaned.extend(_redact_value(item, memo, sensitive_context) for item in value)
        return cleaned
    if isinstance(value, dict):
        if id(value) in memo:
            return memo[id(value)]
        cleaned_dict: dict[object, object] = {}
        memo[id(value)] = cleaned_dict
        for key, item in value.items():
            cleaned_key = _redact_value(key, memo)
            cleaned_dict[cleaned_key] = _redact_value(
                item, memo, sensitive_context or _is_sensitive_key(key)
            )
        return cleaned_dict
    return value


def _redact(data: bytes, suffix: str) -> bytes:
    structured_suffix = suffix.lower()
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        if structured_suffix in {".json", ".yaml", ".yml"}:
            raise ExportError(f"Cannot sanitize non-UTF-8 structured file ({structured_suffix})")
        return data

    if structured_suffix == ".json":
        try:
            def reject_nonstandard_constant(value: str) -> None:
                raise ValueError(f"Non-standard JSON constant: {value}")

            value = json.loads(text, parse_constant=reject_nonstandard_constant)
        except (json.JSONDecodeError, ValueError) as exc:
            raise ExportError("Cannot sanitize invalid JSON file") from exc
        return (json.dumps(_redact_value(value), ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    if structured_suffix in {".yaml", ".yml"}:
        try:
            documents = list(yaml.safe_load_all(text))
            cleaned = [_redact_value(document) for document in documents]
        except yaml.YAMLError as exc:
            raise ExportError(f"Cannot sanitize invalid YAML file ({suffix})") from exc
        return yaml.safe_dump_all(
            cleaned, allow_unicode=True, sort_keys=False, explicit_start=len(cleaned) > 1
        ).encode("utf-8")
    return _redact_text(text).encode("utf-8")


def _copy_export(source: Path, destination: Path) -> tuple[int, int]:
    """Populate a newly-created destination. Return copied and redacted counts."""
    destination.mkdir(parents=True, exist_ok=False)
    copied = redacted = 0
    def fail_on_walk_error(error: OSError) -> None:
        raise ExportError(f"Cannot copy source tree at {error.filename or source}") from error

    for root, directories, files in os.walk(
        source, topdown=True, followlinks=False, onerror=fail_on_walk_error
    ):
        source_root = Path(root)
        _assert_within_source(source_root, source)
        relative_root = source_root.relative_to(source)
        for name in directories:
            relative = relative_root / name
            if not _is_excluded(relative):
                directory = source_root / name
                if _is_reparse_point(directory):
                    raise ExportError(f"Source changed during export and now contains a symlink or junction: {directory}")
                _assert_within_source(directory, source)
                (destination / relative).mkdir(exist_ok=True)
        for name in files:
            relative = relative_root / name
            if _is_excluded(relative):
                continue
            source_file = source_root / name
            if _is_reparse_point(source_file):
                raise ExportError(f"Source changed during export and now contains a symlink or junction: {source_file}")
            _assert_within_source(source_file, source)
            target_file = destination / relative
            target_file.parent.mkdir(parents=True, exist_ok=True)
            if source_file.suffix.lower() in TEXT_SUFFIXES:
                original = source_file.read_bytes()
                sanitized = _redact(original, source_file.suffix)
                target_file.write_bytes(sanitized)
                shutil.copystat(source_file, target_file, follow_symlinks=False)
                copied += 1
                redacted += sanitized != original
            else:
                shutil.copy2(source_file, target_file, follow_symlinks=False)
                copied += 1
    return copied, redacted


def create_export(source_arg: Path, destination_arg: Path | None) -> tuple[Path, int, int]:
    source = _resolve_source(source_arg)
    if destination_arg is None:
        parent = source.parent / ".work" / "sanitized-exports"
        destination_arg = parent / f"export-{uuid.uuid4().hex}"
    destination = _resolve_destination(destination_arg)

    if source == destination or source in destination.parents or destination in source.parents:
        raise ExportError("Source and destination must be disjoint directories; ancestor/descendant paths are unsafe")

    _validate_tree(source)
    destination.parent.mkdir(parents=True, exist_ok=True)
    copied, redacted = _copy_export(source, destination)
    return destination, copied, redacted


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Create a sanitized export copy while preserving the source directory."
    )
    parser.add_argument("source", nargs="?", type=Path, default=Path("output"), help="source directory (default: output)")
    parser.add_argument("destination", nargs="?", type=Path, help="new destination directory (default: fresh .work/sanitized-exports path beside source)")
    args = parser.parse_args(argv)
    try:
        destination, copied, redacted = create_export(args.source, args.destination)
    except (ExportError, OSError, shutil.Error) as exc:
        print(f"[sanitize] Error: {exc}", file=sys.stderr)
        return 1
    print(f"[sanitize] Export created at {destination}")
    print(f"[sanitize] Copied {copied} files; redacted {redacted} text files")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
