"""EPUB package extraction, resource replacement, CSS, archive, and EPUBCheck."""
from __future__ import annotations

import re
import json
import os
import shutil
import subprocess
import tempfile
import zipfile
from collections import Counter
from pathlib import Path
from typing import List, Set
from loguru import logger

from pdf2epub.utils.paths import sanitize_filename
from pdf2epub.utils.html_safety import sanitize_html_document
from .package_identity import PACKAGE_PATH_MANIFEST, validate_package_relative_path


def extract_epub(original_epub: Path, extract_dir: Path) -> None:
    extract_dir.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(original_epub, 'r') as zf:
        root = Path(extract_dir).resolve()
        seen_members = {}
        for info in zf.infolist():
            raw_name = info.filename.rstrip("/") if info.is_dir() else info.filename
            member_name = validate_package_relative_path(raw_name)
            target = root.joinpath(*Path(member_name).parts).resolve()
            try:
                target.relative_to(root)
            except ValueError as exc:
                raise ValueError(f"EPUB member escapes extraction directory: {info.filename!r}") from exc
            if info.is_dir():
                continue
            folded = member_name.casefold()
            previous = seen_members.get(folded)
            if previous is not None:
                raise ValueError(
                    f"EPUB members collide on Windows: {previous!r} and {member_name!r}"
                )
            seen_members[folded] = member_name
        zf.extractall(extract_dir)
    logger.debug(f"Extracted EPUB to {extract_dir}")


class EPUBFilePackager:
    """Replace XHTML and package extracted resources using explicit paths."""
    def __init__(self, output_path: Path, translated_dir: Path):
        self.output_path = Path(output_path)
        self.translated_dir = Path(translated_dir)

    def replace_xhtml_files(self, extract_dir: Path) -> int:
        """
        Replace XHTML files with translated versions.

        Returns:
            Number of files replaced
        """
        document_suffixes = {".xhtml", ".html", ".htm"}
        extract_root = Path(extract_dir).resolve()
        translated_root = self.translated_dir.resolve()
        target_files = {}
        targets_by_basename = {}
        for html_file in extract_root.rglob("*"):
            if not html_file.is_file() or html_file.suffix.lower() not in document_suffixes:
                continue
            try:
                relative = html_file.resolve().relative_to(extract_root).as_posix()
            except ValueError as exc:
                raise ValueError(f"Extracted XHTML escapes package directory: {html_file}") from exc
            relative = validate_package_relative_path(relative)
            folded = relative.casefold()
            if folded in target_files:
                raise ValueError(
                    f"Extracted EPUB has document paths that collide on Windows: "
                    f"{target_files[folded]!r} and {relative!r}"
                )
            target_files[folded] = relative
            targets_by_basename.setdefault(html_file.name.casefold(), []).append(relative)

        translations = {}
        for translated_file in translated_root.rglob("*"):
            if not translated_file.is_file() or translated_file.suffix.lower() not in document_suffixes:
                continue
            try:
                relative = translated_file.resolve().relative_to(translated_root).as_posix()
            except ValueError as exc:
                raise ValueError(f"Translated XHTML escapes translated directory: {translated_file}") from exc
            relative = validate_package_relative_path(relative)
            folded_relative = relative.casefold()
            if folded_relative in translations:
                raise ValueError(
                    f"Translated document paths collide on Windows: "
                    f"{translations[folded_relative][0]!r} and {relative!r}"
                )
            translations[folded_relative] = (relative, translated_file)

        if not translations:
            logger.warning("No translated files found in translated_dir")
            return 0

        identity_manifest_path = translated_root / PACKAGE_PATH_MANIFEST
        explicit_targets = {}
        if identity_manifest_path.is_file():
            try:
                identity_manifest = json.loads(identity_manifest_path.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError) as exc:
                raise ValueError(f"Invalid translated package identity manifest: {exc}") from exc
            if not isinstance(identity_manifest, dict) or identity_manifest.get("schema_version") != 1:
                raise ValueError("Unsupported translated package identity manifest")
            documents = identity_manifest.get("documents")
            if not isinstance(documents, list):
                raise ValueError("Translated package identity manifest has no documents list")
            seen_source_paths = set()
            for document in documents:
                if not isinstance(document, dict):
                    raise ValueError("Invalid document entry in translated package identity manifest")
                translated_path = validate_package_relative_path(document.get("translated_path"))
                package_path = validate_package_relative_path(document.get("package_relative_path"))
                folded_source = translated_path.casefold()
                folded_target = package_path.casefold()
                if folded_source in seen_source_paths:
                    raise ValueError(f"Duplicate translated path in identity manifest: {translated_path}")
                seen_source_paths.add(folded_source)
                if folded_source not in translations:
                    raise ValueError(f"Identity manifest references missing translated file: {translated_path}")
                if folded_target not in target_files:
                    raise ValueError(f"Identity manifest target is absent from EPUB: {package_path}")
                exact_target = target_files[folded_target]
                if exact_target != package_path:
                    raise ValueError(
                        f"Identity manifest package path case differs from EPUB member: {package_path}"
                    )
                explicit_targets[folded_source] = folded_target

        assignments = []
        assigned_targets = set()
        for folded_source, (relative, translated_file) in translations.items():
            if folded_source in explicit_targets:
                folded_target = explicit_targets[folded_source]
            elif "/" in relative:
                # Nested files are self-identifying by their exact path.
                if folded_source in target_files and target_files[folded_source] != relative:
                    raise ValueError(
                        f"Translated package path case differs from EPUB member: {relative}"
                    )
                folded_target = folded_source if folded_source in target_files else None
            else:
                # Legacy flat outputs have only a basename. Never choose among
                # duplicate targets, even when one happens to be at package root.
                candidates = targets_by_basename.get(Path(relative).name.casefold(), [])
                if len(candidates) > 1:
                    raise ValueError(
                        f"Ambiguous legacy translated file {relative!r}: matches "
                        f"{', '.join(candidates)}. Re-run extraction with package path identities."
                    )
                folded_target = candidates[0].casefold() if candidates else None
            if folded_target is None:
                continue
            if folded_target in assigned_targets:
                raise ValueError(
                    f"Multiple translated files target the same EPUB member: "
                    f"{target_files[folded_target]}"
                )
            assigned_targets.add(folded_target)
            target = extract_root.joinpath(*Path(target_files[folded_target]).parts)
            assignments.append((target, translated_file))

        # All ambiguity and path checks complete before the first mutation.
        for target, translated_file in assignments:
            self.replace_file(target, translated_file)
        return len(assignments)

    def replace_file(self, target: Path, source: Path):
        """Replace target file with source content."""
        content = source.read_text(encoding='utf-8')
        target.write_text(sanitize_html_document(content), encoding='utf-8')
        logger.debug(f"Replaced {target.name}")

    def normalize_css(self, extract_dir: Path):
        """Patch fragile source CSS that can break after XHTML reconstruction."""
        block_wrapper_classes = self._classes_used_as_inline_block_wrappers(extract_dir)
        if not block_wrapper_classes:
            return

        for css_file in extract_dir.rglob("*.css"):
            content = css_file.read_text(encoding='utf-8')
            normalized = self._normalize_css_content(content, block_wrapper_classes)
            if normalized != content:
                css_file.write_text(normalized, encoding='utf-8')
                logger.debug(f"Normalized CSS: {css_file.name}")

    @staticmethod
    def _class_names(attrs: str) -> List[str]:
        match = re.search(
            r'\bclass\s*=\s*(["\'])(.*?)\1',
            attrs,
            re.IGNORECASE | re.DOTALL,
        )
        if not match:
            return []
        return [name for name in re.split(r'\s+', match.group(2).strip()) if name]

    @classmethod
    def _classes_used_as_inline_block_wrappers(cls, extract_dir: Path) -> Set[str]:
        """
        Find classes exclusively used by inline spans that wrap block content.

        These wrappers are invalid-but-common in converted EPUBs. We only
        normalize classes that are not also used on ordinary inline spans or
        non-span elements, so a general selector does not accidentally change
        unrelated inline styling.
        """
        tag_pattern = re.compile(
            r'<([A-Za-z][\w:-]*)\b([^>]*)>',
            re.IGNORECASE | re.DOTALL,
        )
        span_pattern = re.compile(
            r'<span\b([^>]*)>((?:(?!</span>).){0,5000})</span>',
            re.IGNORECASE | re.DOTALL,
        )
        block_child_pattern = re.compile(
            r'<\s*(?:div|table|p|section|article|ul|ol|li|tr|td|blockquote|h[1-6])\b',
            re.IGNORECASE,
        )

        span_counts: Counter[str] = Counter()
        block_wrapper_counts: Counter[str] = Counter()
        nonspan_counts: Counter[str] = Counter()
        html_files = (
            list(extract_dir.rglob("*.xhtml"))
            + list(extract_dir.rglob("*.html"))
            + list(extract_dir.rglob("*.htm"))
        )
        for html_file in html_files:
            content = html_file.read_text(encoding='utf-8', errors='ignore')

            for match in tag_pattern.finditer(content):
                tag = match.group(1).lower()
                for class_name in cls._class_names(match.group(2)):
                    if tag == "span":
                        span_counts[class_name] += 1
                    else:
                        nonspan_counts[class_name] += 1

            for match in span_pattern.finditer(content):
                if match.group(1).strip().endswith('/'):
                    continue
                if not block_child_pattern.search(match.group(2)):
                    continue
                for class_name in cls._class_names(match.group(1)):
                    block_wrapper_counts[class_name] += 1

        return {
            class_name
            for class_name, count in block_wrapper_counts.items()
            if count == span_counts.get(class_name, 0)
            and nonspan_counts.get(class_name, 0) == 0
        }

    @staticmethod
    def _normalize_css_content(content: str, block_wrapper_classes: Set[str]) -> str:
        """
        Make section-level wrappers render consistently.

        Some converted EPUBs use inline spans as wrappers around block elements.
        EPUB readers may repair that invalid markup by breaking inheritance at
        nested tables, causing sudden font-size jumps.
        """
        normalized = content

        def normalize(match: re.Match) -> str:
            body = match.group(2)
            if re.search(r'\bdisplay\s*:', body):
                return match.group(0)
            return f"{match.group(1)}\n    display: block;{body}}}"

        for class_name in sorted(block_wrapper_classes):
            pattern = re.compile(
                r'(\.' + re.escape(class_name) + r'\s*\{)([^}]*)\}',
                re.MULTILINE,
            )
            normalized = pattern.sub(normalize, normalized)

        return normalized

    def package_epub(self, content_dir: Path, output_path: Path | None = None):
        """
        Package directory contents as EPUB.

        EPUB packaging rules:
        1. mimetype must be first file, stored uncompressed
        2. Other files use DEFLATE compression
        """
        destination = Path(output_path) if output_path is not None else self.output_path
        destination.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary_name = tempfile.mkstemp(
            prefix=f".{destination.name}.", suffix=".tmp", dir=destination.parent
        )
        os.close(fd)
        temporary_path = Path(temporary_name)

        try:
            with zipfile.ZipFile(temporary_path, 'w') as zf:
                # 1. Write mimetype first, uncompressed
                mimetype_path = content_dir / "mimetype"
                if mimetype_path.exists():
                    zf.write(
                        mimetype_path,
                        "mimetype",
                        compress_type=zipfile.ZIP_STORED
                    )
                else:
                    zf.writestr(
                        "mimetype",
                        "application/epub+zip",
                        compress_type=zipfile.ZIP_STORED
                    )

                # 2. Write all other files with compression
                for file_path in content_dir.rglob("*"):
                    if file_path.is_file() and file_path.name != "mimetype":
                        arcname = file_path.relative_to(content_dir).as_posix()
                        zf.write(
                            file_path,
                            arcname,
                            compress_type=zipfile.ZIP_DEFLATED
                        )
            os.replace(temporary_path, destination)
        except BaseException:
            temporary_path.unlink(missing_ok=True)
            raise

        logger.debug(f"Packaged EPUB to {destination}")


def validate_output_epub(output_path: Path, mode: str, executable_path: str | None = None) -> None:
    mode = mode.lower().strip()
    if mode not in {"off", "warn", "strict"}:
        raise ValueError(
            f"Invalid epubcheck_mode {mode!r}; "
            "expected 'off', 'warn', or 'strict'"
        )
    if mode == "off":
        return

    executable = executable_path or shutil.which("epubcheck")
    if not executable:
        message = "EPUBCheck is not installed; skipping final EPUB validation"
        if mode == "strict":
            raise RuntimeError(message)
        logger.info(message)
        return

    try:
        result = subprocess.run(
            [executable, str(output_path)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=180,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        message = f"EPUBCheck could not validate {output_path.name}: {exc}"
        if mode == "strict":
            raise RuntimeError(message) from exc
        logger.warning(message)
        return

    if result.returncode == 0:
        logger.info(f"EPUBCheck passed: {output_path.name}")
        return

    details = "\n".join(
        part.strip() for part in (result.stdout, result.stderr) if part.strip()
    )
    message = f"EPUBCheck failed for {output_path.name}"
    if details:
        message = f"{message}:\n{details}"
    if mode == "strict":
        raise ValueError(message)
    logger.warning(message)
