"""EPUB package extraction, resource replacement, CSS, archive, and EPUBCheck."""
from __future__ import annotations

import re
import shutil
import subprocess
import zipfile
from collections import Counter
from pathlib import Path
from typing import List, Set
from loguru import logger

from pdf2epub.utils.paths import sanitize_filename
from pdf2epub.utils.html_safety import sanitize_html_document


def extract_epub(original_epub: Path, extract_dir: Path) -> None:
    extract_dir.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(original_epub, 'r') as zf:
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
        replaced = 0

        # Get list of translated files
        translated_files = {}
        for ext in ("*.xhtml", "*.html", "*.htm"):
            translated_files.update({f.name: f for f in self.translated_dir.glob(ext)})

        if not translated_files:
            logger.warning("No translated files found in translated_dir")
            return 0

        # Find all XHTML/HTML files in extracted EPUB
        for ext in ("*.xhtml", "*.html", "*.htm"):
            for html_file in extract_dir.rglob(ext):
                if html_file.name in translated_files:
                    self.replace_file(html_file, translated_files[html_file.name])
                    replaced += 1

        return replaced

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

    def package_epub(self, content_dir: Path):
        """
        Package directory contents as EPUB.

        EPUB packaging rules:
        1. mimetype must be first file, stored uncompressed
        2. Other files use DEFLATE compression
        """
        self.output_path.parent.mkdir(parents=True, exist_ok=True)

        with zipfile.ZipFile(self.output_path, 'w') as zf:
            # 1. Write mimetype first, uncompressed
            mimetype_path = content_dir / "mimetype"
            if mimetype_path.exists():
                zf.write(
                    mimetype_path,
                    "mimetype",
                    compress_type=zipfile.ZIP_STORED
                )
            else:
                # Create mimetype if missing
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

        logger.debug(f"Packaged EPUB to {self.output_path}")


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
