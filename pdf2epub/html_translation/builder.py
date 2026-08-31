"""
HTML EPUB Builder.

Rebuilds EPUB by replacing translated XHTML content while preserving
all other resources (CSS, fonts, images, metadata).
"""

import zipfile
import tempfile
import json
import hashlib
import re
import shutil
import subprocess
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Set
from dataclasses import dataclass
from loguru import logger
from lxml import etree as LET
from xml.etree import ElementTree as ET

from .epub_parser import EPUBParser
from .validation import nonempty_lines, tag_mismatch_count
from pdf2epub.subagent_workflow import resolve_subagent_model


PART_FILE_RE = re.compile(r'^(.+)\.part(\d+)\.md$')


def _has_local_tag(element: Any, local_name: str) -> bool:
    """Return whether an XML element has the requested local tag name."""
    tag = getattr(element, "tag", None)
    return isinstance(tag, str) and (
        tag == local_name or tag.endswith(f"}}{local_name}")
    )


def _make_json_safe(obj: Any) -> Any:
    """Convert nested compressor metadata to JSON-serializable values."""
    from collections.abc import Iterable, Mapping

    if isinstance(obj, (bool, int, float, str, type(None))):
        return obj
    if isinstance(obj, Mapping):
        return {str(_make_json_safe(k)): _make_json_safe(v) for k, v in obj.items()}
    if isinstance(obj, Iterable) and not isinstance(obj, (str, bytes)):
        return [_make_json_safe(v) for v in obj]
    return str(obj)


def sanitize_filename(name: str) -> str:
    """Sanitize a string for use as a filename."""
    # Remove or replace characters that are problematic in filenames
    # Windows: \ / : * ? " < > |
    # Also handle other common issues
    sanitized = re.sub(r'[\\/:*?"<>|]', '_', name)
    # Replace multiple underscores/spaces with single
    sanitized = re.sub(r'[_\s]+', ' ', sanitized)
    # Trim and limit length
    sanitized = sanitized.strip()[:200]
    return sanitized


@dataclass
class BuildConfig:
    """Configuration for EPUB building."""
    original_epub: Path
    translated_dir: Path
    output_path: Path
    book_title: str
    translated_metadata: Optional[Dict] = None  # Contains translated_title and toc
    epubcheck_mode: str = "warn"  # off, warn, or strict
    epubcheck_path: Optional[str] = None


class HTMLEpubBuilder:
    """
    Build translated EPUB by replacing XHTML content.

    Process:
    1. Extract original EPUB to temp directory
    2. Replace XHTML files with translated versions
    3. Repackage as new EPUB

    Preserves:
    - mimetype (uncompressed, first file)
    - META-INF/container.xml
    - content.opf (manifest, spine, metadata)
    - toc.ncx / nav.xhtml (navigation)
    - CSS, fonts, images
    """

    def __init__(self, config: BuildConfig):
        self.config = config
        self.original_epub = config.original_epub
        self.translated_dir = config.translated_dir
        self.output_path = config.output_path

    def build(self) -> Path:
        """
        Build the translated EPUB.

        Returns:
            Path to the output EPUB file
        """
        logger.info(f"Building translated EPUB from {self.original_epub}")

        # Create temp directory for extraction
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            extract_dir = temp_path / "epub_content"

            # Step 1: Extract original EPUB
            self._extract_epub(extract_dir)

            # Step 2: Find and replace XHTML files
            replaced_count = self._replace_xhtml_files(extract_dir)
            logger.info(f"Replaced {replaced_count} XHTML files")

            # Step 3: Update metadata if provided
            if self.config.translated_metadata:
                self._update_content_opf(extract_dir, self.config.translated_metadata)
                self._update_toc_ncx(extract_dir, self.config.translated_metadata)
                self._update_nav_xhtml(extract_dir, self.config.translated_metadata)

            # Step 4: Normalize fragile source CSS before packaging
            self._normalize_css(extract_dir)

            # Step 5: Repackage as EPUB
            self._package_epub(extract_dir)

        self._validate_output_epub()
        logger.info(f"Built EPUB: {self.output_path}")
        return self.output_path

    def _validate_output_epub(self) -> None:
        """Run EPUBCheck when available, optionally failing the build."""
        mode = self.config.epubcheck_mode.lower().strip()
        if mode not in {"off", "warn", "strict"}:
            raise ValueError(
                f"Invalid epubcheck_mode {self.config.epubcheck_mode!r}; "
                "expected 'off', 'warn', or 'strict'"
            )
        if mode == "off":
            return

        executable = self.config.epubcheck_path or shutil.which("epubcheck")
        if not executable:
            message = "EPUBCheck is not installed; skipping final EPUB validation"
            if mode == "strict":
                raise RuntimeError(message)
            logger.info(message)
            return

        try:
            result = subprocess.run(
                [executable, str(self.output_path)],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=180,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            message = f"EPUBCheck could not validate {self.output_path.name}: {exc}"
            if mode == "strict":
                raise RuntimeError(message) from exc
            logger.warning(message)
            return

        if result.returncode == 0:
            logger.info(f"EPUBCheck passed: {self.output_path.name}")
            return

        details = "\n".join(
            part.strip() for part in (result.stdout, result.stderr) if part.strip()
        )
        message = f"EPUBCheck failed for {self.output_path.name}"
        if details:
            message = f"{message}:\n{details}"
        if mode == "strict":
            raise ValueError(message)
        logger.warning(message)

    def _extract_epub(self, extract_dir: Path):
        """Extract original EPUB to directory."""
        extract_dir.mkdir(parents=True, exist_ok=True)

        with zipfile.ZipFile(self.original_epub, 'r') as zf:
            zf.extractall(extract_dir)

        logger.debug(f"Extracted EPUB to {extract_dir}")

    def _replace_xhtml_files(self, extract_dir: Path) -> int:
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
                    self._replace_file(html_file, translated_files[html_file.name])
                    replaced += 1

        return replaced

    def _replace_file(self, target: Path, source: Path):
        """Replace target file with source content."""
        content = source.read_text(encoding='utf-8')
        target.write_text(content, encoding='utf-8')
        logger.debug(f"Replaced {target.name}")

    def _normalize_css(self, extract_dir: Path):
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

    def _package_epub(self, content_dir: Path):
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

    def _find_opf_path(self, extract_dir: Path) -> Optional[Path]:
        """
        Find the OPF package document by reading META-INF/container.xml.

        The container.xml is the standard entry point for EPUB - it specifies
        where the actual OPF file is located (could be content.opf, package.opf,
        or any other name in any subdirectory).
        """
        container_path = extract_dir / "META-INF" / "container.xml"

        if not container_path.exists():
            # Fallback: glob for any .opf file
            logger.debug("container.xml not found, falling back to glob")
            opf_candidates = list(extract_dir.rglob("*.opf"))
            return opf_candidates[0] if opf_candidates else None

        try:
            tree = ET.parse(container_path)
            root = tree.getroot()

            # container.xml uses the container namespace
            ns = {'container': 'urn:oasis:names:tc:opendocument:xmlns:container'}

            # Find rootfile element
            rootfile = root.find('.//container:rootfile', ns)
            if rootfile is None:
                # Try without namespace
                for elem in root.iter():
                    if _has_local_tag(elem, 'rootfile'):
                        rootfile = elem
                        break

            if rootfile is not None:
                full_path = rootfile.get('full-path')
                if full_path:
                    opf_path = extract_dir / full_path
                    if opf_path.exists():
                        return opf_path

            # Fallback to glob
            opf_candidates = list(extract_dir.rglob("*.opf"))
            return opf_candidates[0] if opf_candidates else None

        except Exception as e:
            logger.debug(f"Error parsing container.xml: {e}, falling back to glob")
            opf_candidates = list(extract_dir.rglob("*.opf"))
            return opf_candidates[0] if opf_candidates else None

    def _find_toc_files(self, extract_dir: Path, opf_path: Path) -> Dict[str, Optional[Path]]:
        """
        Find NCX and Nav files by parsing the OPF manifest.

        Returns dict with keys:
        - 'ncx': Path to NCX file (EPUB 2 TOC, media-type="application/x-dtbncx+xml")
        - 'nav': Path to Nav document (EPUB 3 TOC, properties="nav")
        """
        result = {'ncx': None, 'nav': None}
        opf_dir = opf_path.parent

        try:
            tree = ET.parse(opf_path)
            root = tree.getroot()

            # OPF namespace
            ns = {'opf': 'http://www.idpf.org/2007/opf'}

            # Find manifest element
            manifest = root.find('.//opf:manifest', ns)
            if manifest is None:
                # Try without namespace
                for elem in root.iter():
                    if _has_local_tag(elem, 'manifest'):
                        manifest = elem
                        break

            if manifest is None:
                logger.debug("No manifest found in OPF")
                return result

            # Search all items in manifest
            for item in manifest:
                if not _has_local_tag(item, 'item'):
                    continue

                media_type = item.get('media-type', '')
                properties = item.get('properties', '')
                href = item.get('href', '')

                if not href:
                    continue

                # NCX: identified by media-type
                if media_type == 'application/x-dtbncx+xml':
                    ncx_path = opf_dir / href
                    if ncx_path.exists():
                        result['ncx'] = ncx_path
                        logger.debug(f"Found NCX via manifest: {ncx_path}")

                # Nav: identified by properties="nav"
                if 'nav' in properties.split():
                    nav_path = opf_dir / href
                    if nav_path.exists():
                        result['nav'] = nav_path
                        logger.debug(f"Found Nav via manifest: {nav_path}")

            # Also check spine for toc attribute (alternative NCX reference)
            if result['ncx'] is None:
                spine = root.find('.//opf:spine', ns)
                if spine is None:
                    for elem in root.iter():
                        if _has_local_tag(elem, 'spine'):
                            spine = elem
                            break

                if spine is not None:
                    toc_id = spine.get('toc')
                    if toc_id:
                        # Find the item with this id
                        for item in manifest:
                            if item.get('id') == toc_id:
                                href = item.get('href', '')
                                if href:
                                    ncx_path = opf_dir / href
                                    if ncx_path.exists():
                                        result['ncx'] = ncx_path
                                        logger.debug(f"Found NCX via spine toc attr: {ncx_path}")
                                break

        except Exception as e:
            logger.debug(f"Error parsing OPF for TOC files: {e}")

        return result

    def _update_content_opf(self, extract_dir: Path, metadata: Dict):
        """Update content.opf with translated title and language."""
        opf_path = self._find_opf_path(extract_dir)
        if not opf_path:
            logger.warning("No OPF file found")
            return

        translated_title = metadata.get('translated_title')
        target_lang_code = metadata.get('target_language_code')

        if not translated_title and not target_lang_code:
            return

        try:
            # lxml preserves the source namespace map when serializing. The
            # stdlib ElementTree can emit OPF namespaced attributes such as
            # opf:role as invalid unqualified attributes when the OPF namespace
            # is also the document's default namespace.
            tree = LET.parse(str(opf_path))
            root = tree.getroot()

            # Handle namespaces - OPF uses default namespace
            namespaces = {
                'opf': 'http://www.idpf.org/2007/opf',
                'dc': 'http://purl.org/dc/elements/1.1/'
            }

            # Find and update dc:title
            if translated_title:
                # Try with namespace first
                title_elem = root.find('.//dc:title', namespaces)
                if title_elem is None:
                    # Try without namespace (some EPUBs don't use namespaces properly)
                    for elem in root.iter():
                        if _has_local_tag(elem, 'title'):
                            title_elem = elem
                            break

                if title_elem is not None:
                    title_elem.text = translated_title
                    logger.debug(f"Updated content.opf title: {translated_title}")
                else:
                    logger.warning("dc:title element not found in content.opf")

            # Find and update dc:language
            if target_lang_code:
                lang_elem = root.find('.//dc:language', namespaces)
                if lang_elem is None:
                    for elem in root.iter():
                        if _has_local_tag(elem, 'language'):
                            lang_elem = elem
                            break

                if lang_elem is not None:
                    old_lang = lang_elem.text
                    lang_elem.text = target_lang_code
                    logger.debug(f"Updated content.opf language: {old_lang} -> {target_lang_code}")
                else:
                    logger.warning("dc:language element not found in content.opf")

            # dc:creator and dc:publisher are intentionally untouched.  Names
            # and publisher imprints are bibliographic identity, not prose;
            # changing them would also break EPUB 3 creator refinements.

            # Update dc:description
            if metadata.get('translated_description'):
                desc_elem = root.find('.//dc:description', namespaces)
                if desc_elem is None:
                    for e in root.iter():
                        if _has_local_tag(e, 'description'):
                            desc_elem = e
                            break
                if desc_elem is not None:
                    desc_elem.text = metadata['translated_description']
                    logger.debug("Updated description")

            # Update dc:rights
            if metadata.get('translated_rights'):
                rights_elem = root.find('.//dc:rights', namespaces)
                if rights_elem is None:
                    for e in root.iter():
                        if _has_local_tag(e, 'rights'):
                            rights_elem = e
                            break
                if rights_elem is not None:
                    rights_elem.text = metadata['translated_rights']
                    logger.debug("Updated rights")

            # Update calibre:title_sort
            if metadata.get('translated_title_sort'):
                for elem in root.iter():
                    if _has_local_tag(elem, 'meta'):
                        if elem.get('name') == 'calibre:title_sort':
                            elem.set('content', metadata['translated_title_sort'])
                            logger.debug(f"Updated title_sort: {metadata['translated_title_sort']}")
                            break

            tree.write(str(opf_path), encoding='utf-8', xml_declaration=True)

        except Exception as e:
            logger.warning(f"Failed to update content.opf: {e}")

    def _update_toc_ncx(self, extract_dir: Path, metadata: Dict):
        """Update toc.ncx with translated chapter titles."""
        # Find NCX file via OPF manifest (proper EPUB way)
        opf_path = self._find_opf_path(extract_dir)
        if not opf_path:
            logger.debug("No OPF found, cannot locate NCX")
            return

        toc_files = self._find_toc_files(extract_dir, opf_path)
        ncx_path = toc_files.get('ncx')

        if not ncx_path:
            # Fallback to glob (for malformed EPUBs)
            ncx_candidates = list(extract_dir.rglob("*.ncx"))
            if not ncx_candidates:
                logger.debug("No NCX file found (EPUB 3 only?)")
                return
            ncx_path = ncx_candidates[0]

        ncx_dir = ncx_path.parent  # For resolving relative paths
        toc_entries = metadata.get('toc', [])
        if not toc_entries:
            return

        # Build multiple mappings for robust matching
        # 1. Full href -> title (exact match)
        # 2. Basename + fragment -> title (for when paths differ)
        # 3. Normalized href -> title
        href_to_title = {}
        basename_to_title = {}
        for entry in toc_entries:
            href = entry.get('href', '')
            translated = entry.get('translated', '')
            if href and translated:
                # Full href
                href_to_title[href] = translated
                # Basename with fragment (e.g., "chapter1.html#section1")
                basename = Path(href.split('#')[0]).name
                if '#' in href:
                    basename += '#' + href.split('#')[1]
                basename_to_title[basename] = translated
                # Also store without fragment for base file matching
                basename_no_frag = Path(href.split('#')[0]).name
                if basename_no_frag not in basename_to_title:
                    basename_to_title[basename_no_frag] = translated

        def find_translation(src: str) -> str:
            """Try multiple strategies to find translation for src."""
            # 1. Exact match
            if src in href_to_title:
                return href_to_title[src]

            # 2. Resolve relative to NCX dir and try again
            if ncx_dir != extract_dir:
                resolved = (ncx_dir / src).relative_to(extract_dir)
                resolved_str = resolved.as_posix()
                if resolved_str in href_to_title:
                    return href_to_title[resolved_str]

            # 3. Match by basename + fragment
            basename = Path(src.split('#')[0]).name
            if '#' in src:
                basename += '#' + src.split('#')[1]
            if basename in basename_to_title:
                return basename_to_title[basename]

            return None

        try:
            ET.register_namespace('', 'http://www.daisy.org/z3986/2005/ncx/')
            tree = ET.parse(ncx_path)
            root = tree.getroot()

            # Update docTitle (try multiple namespace approaches)
            translated_title = metadata.get('translated_title')
            if translated_title:
                # Try with namespace
                ns = {'ncx': 'http://www.daisy.org/z3986/2005/ncx/'}
                doc_title = root.find('.//ncx:docTitle/ncx:text', ns)
                if doc_title is None:
                    # Try without namespace
                    for elem in root.iter():
                        if _has_local_tag(elem, 'text'):
                            parent = elem.getparent() if hasattr(elem, 'getparent') else None
                            if (
                                parent is not None
                                and _has_local_tag(parent, 'docTitle')
                            ):
                                doc_title = elem
                                break
                if doc_title is not None:
                    doc_title.text = translated_title

            # Update navPoints
            updated_count = 0
            for nav_point in root.iter():
                if _has_local_tag(nav_point, 'navPoint'):
                    # Find content src
                    content = None
                    for child in nav_point:
                        if _has_local_tag(child, 'content'):
                            content = child
                            break

                    if content is not None:
                        src = content.get('src', '')
                        translated = find_translation(src)

                        if translated:
                            # Find navLabel/text and update
                            for child in nav_point:
                                if _has_local_tag(child, 'navLabel'):
                                    for text_elem in child:
                                        if _has_local_tag(text_elem, 'text'):
                                            text_elem.text = translated
                                            updated_count += 1
                                            break
                                    break

            tree.write(ncx_path, encoding='utf-8', xml_declaration=True)
            logger.debug(f"Updated {updated_count} navPoints in toc.ncx")

        except Exception as e:
            logger.warning(f"Failed to update toc.ncx: {e}")

    def _update_nav_xhtml(self, extract_dir: Path, metadata: Dict):
        """Update nav.xhtml with translated chapter titles (EPUB 3)."""
        # Find nav document via OPF manifest (proper EPUB 3 way)
        opf_path = self._find_opf_path(extract_dir)
        nav_path = None

        if opf_path:
            toc_files = self._find_toc_files(extract_dir, opf_path)
            nav_path = toc_files.get('nav')

        if not nav_path:
            # Fallback to glob (for malformed EPUBs or when OPF doesn't specify nav)
            nav_candidates = (
                list(extract_dir.rglob("nav.xhtml")) +
                list(extract_dir.rglob("nav.html")) +
                list(extract_dir.rglob("*nav*.xhtml")) +
                list(extract_dir.rglob("*nav*.html"))
            )
            # Deduplicate
            nav_candidates = list(dict.fromkeys(nav_candidates))

            if not nav_candidates:
                logger.debug("No nav document found (EPUB 2 only?)")
                return

            nav_path = nav_candidates[0]
        nav_dir = nav_path.parent
        toc_entries = metadata.get('toc', [])
        if not toc_entries:
            return

        # Build multiple mappings for robust matching (like NCX)
        href_to_title = {}
        basename_to_title = {}
        original_to_title = {}
        for entry in toc_entries:
            href = entry.get('href', '')
            translated = entry.get('translated', '')
            original = entry.get('original', '')
            if original and translated:
                original_to_title[original.strip()] = translated
            if href and translated:
                href_to_title[href] = translated
                # Basename with fragment
                basename = Path(href.split('#')[0]).name
                if '#' in href:
                    basename += '#' + href.split('#')[1]
                basename_to_title[basename] = translated

        try:
            # Use BeautifulSoup for more robust HTML parsing
            from bs4 import BeautifulSoup

            content = nav_path.read_text(encoding='utf-8')
            soup = BeautifulSoup(content, 'html.parser')

            updated_count = 0
            structural_updated_count = 0
            for a_tag in soup.find_all('a', href=True):
                href = a_tag.get('href', '')

                # Try multiple matching strategies
                translated = None

                # 1. Exact match
                if href in href_to_title:
                    translated = href_to_title[href]

                # 2. Resolve relative to nav dir
                if not translated and nav_dir != extract_dir:
                    try:
                        resolved = (nav_dir / href.split('#')[0]).relative_to(extract_dir)
                        resolved_str = resolved.as_posix()
                        if '#' in href:
                            resolved_str += '#' + href.split('#')[1]
                        if resolved_str in href_to_title:
                            translated = href_to_title[resolved_str]
                    except ValueError:
                        pass

                # 3. Match by basename + fragment
                if not translated:
                    basename = Path(href.split('#')[0]).name
                    if '#' in href:
                        basename += '#' + href.split('#')[1]
                    if basename in basename_to_title:
                        translated = basename_to_title[basename]

                if translated:
                    # Update the link text, handling both simple and nested cases
                    if a_tag.string is not None:
                        # Simple case: direct text content
                        a_tag.string = translated
                        updated_count += 1
                    else:
                        # Complex case: nested elements - replace all text content
                        # Clear existing content and set new text
                        a_tag.clear()
                        a_tag.string = translated
                        updated_count += 1

            def _nav_type_value(tag) -> str:
                values = []
                for attr in ("epub:type", "type", "role", "id", "class"):
                    value = tag.get(attr)
                    if isinstance(value, list):
                        values.extend(str(v) for v in value)
                    elif value:
                        values.append(str(value))
                return " ".join(values).lower()

            toc_navs = [
                nav for nav in soup.find_all("nav")
                if "toc" in _nav_type_value(nav)
            ]
            if not toc_navs:
                toc_navs = soup.find_all("nav")

            # Update structural headings inside the EPUB navigation document.
            # This is intentionally scoped to nav headings and exact TOC labels;
            # it is not a global string replacement.
            for nav in toc_navs:
                for heading in nav.find_all(["h1", "h2", "h3", "h4", "h5", "h6"]):
                    original = heading.get_text(strip=True)
                    translated = original_to_title.get(original)
                    if translated:
                        heading.clear()
                        heading.string = translated
                        structural_updated_count += 1

            title_tag = soup.find("title")
            if title_tag and title_tag.string:
                original = title_tag.string.strip()
                translated = original_to_title.get(original)
                if not translated and original == metadata.get("original_title"):
                    translated = metadata.get("translated_title")
                if translated:
                    title_tag.string.replace_with(translated)
                    structural_updated_count += 1

            # Write back, preserving original structure as much as possible
            nav_path.write_text(str(soup), encoding='utf-8')
            logger.debug(
                f"Updated {updated_count} links and {structural_updated_count} "
                f"structural labels in nav.xhtml"
            )

        except ImportError:
            # Fallback to regex if BeautifulSoup not available
            logger.debug("BeautifulSoup not available, using regex fallback")
            content = nav_path.read_text(encoding='utf-8')
            updated_count = 0
            for href, translated in href_to_title.items():
                pattern = rf'(<a[^>]*href=["\']){re.escape(href)}(["\'][^>]*>)([^<]*)(<\/a>)'
                new_content = re.sub(pattern, rf'\g<1>{href}\g<2>{translated}\g<4>', content)
                if new_content != content:
                    updated_count += 1
                    content = new_content
            nav_path.write_text(content, encoding='utf-8')
            logger.debug(f"Updated {updated_count} entries in nav.xhtml (regex)")

        except Exception as e:
            logger.warning(f"Failed to update nav.xhtml: {e}")


def build_html_epub(
    original_epub: Path,
    translated_dir: Path,
    output_path: Optional[Path] = None,
    book_title: Optional[str] = None,
    translated_metadata: Optional[Dict] = None,
    epubcheck_mode: str = "warn",
    epubcheck_path: Optional[str] = None,
) -> Path:
    """
    Convenience function to build translated EPUB.

    Args:
        original_epub: Path to original EPUB file
        translated_dir: Directory containing translated XHTML files
        output_path: Optional output path (defaults to translated_{original}.epub)
        book_title: Optional book title
        translated_metadata: Optional dict with translated_title and toc entries
        epubcheck_mode: Final validation mode: off, warn, or strict
        epubcheck_path: Optional explicit path to the EPUBCheck executable

    Returns:
        Path to the built EPUB file
    """
    if output_path is None:
        output_path = original_epub.parent / f"translated_{original_epub.name}"

    config = BuildConfig(
        original_epub=original_epub,
        translated_dir=translated_dir,
        output_path=output_path,
        book_title=book_title or original_epub.stem,
        translated_metadata=translated_metadata,
        epubcheck_mode=epubcheck_mode,
        epubcheck_path=epubcheck_path,
    )

    builder = HTMLEpubBuilder(config)
    return builder.build()


class HTMLEpubPipeline:
    """
    Complete HTML translation pipeline using HTMLCompressor.

    Orchestrates the full workflow:
    1. Extract XHTML from EPUB
    2. Compress HTML (strip outer structure, save mapping)
    3. Translate compressed content (one line per translation unit)
    4. Translate title and TOC
    5. Decompress (reconstruct full HTML from translated lines + mapping)
    6. Build new EPUB

    Directory structure:
    - compressed_units/  : .md (compressed content) + .mapping.json
    - translated_compressed/ : .md (translated lines)
    - final_xhtml/ : .xhtml (fully reconstructed HTML)
    """

    def __init__(
        self,
        epub_path: Path,
        output_dir: Path,
        config: Dict
    ):
        self.epub_path = epub_path
        self.output_dir = output_dir
        self.config = config

        # Parse EPUB
        self.parser = EPUBParser(str(epub_path))

        # Extract metadata
        self.metadata = self.parser.metadata
        self.book_title = self.metadata.get('title', epub_path.stem)
        self.source_language = self._detect_language()

        # Setup directories (using new compressor-based workflow)
        self.compressed_units_dir = output_dir / "compressed_units"  # .md + .mapping.json
        self.translated_dir = output_dir / "translated_compressed"   # .md
        self.final_dir = output_dir / "final_xhtml"                  # .xhtml

        for d in [self.compressed_units_dir, self.translated_dir, self.final_dir]:
            d.mkdir(parents=True, exist_ok=True)

    def _detect_language(self) -> str:
        """Detect source language from EPUB metadata."""
        lang_code = self.metadata.get('language', 'en')

        # Map language codes to full names
        lang_map = {
            'en': 'English',
            'ja': 'Japanese',
            'zh': 'Chinese',
            'de': 'German',
            'fr': 'French',
            'es': 'Spanish',
            'ko': 'Korean',
            'ru': 'Russian',
        }

        # Handle codes like 'en-US', 'zh-CN'
        base_code = lang_code.split('-')[0].lower()
        return lang_map.get(base_code, 'English')

    def _get_language_code(self, language: str) -> str:
        """Convert language name to ISO 639-1 code for EPUB metadata."""
        # Map full names to language codes (reverse of _detect_language)
        name_to_code = {
            'english': 'en',
            'japanese': 'ja',
            'chinese': 'zh',
            'german': 'de',
            'french': 'fr',
            'spanish': 'es',
            'korean': 'ko',
            'russian': 'ru',
            '中文': 'zh',
            '日本語': 'ja',
            '한국어': 'ko',
        }

        lang_lower = language.lower()
        return name_to_code.get(lang_lower, 'zh')  # Default to 'zh' for Chinese

    def extract_and_preprocess(self, target_language: Optional[str] = None) -> int:
        """
        Extract XHTML from EPUB and compress for translation.

        Uses HTMLCompressor to strip outer structure and save mapping.
        Only translatable content is output (no empty placeholders).

        Returns:
            Number of files extracted
        """
        import json
        from .compressor import HTMLCompressor
        from .verified_compactor import VerifiedCompactor

        # Get all CSS content for compactor
        css_content = ""
        for css_item in self.parser.resources.get('css', []):
            try:
                content = css_item.get('content', b'')
                if isinstance(content, bytes):
                    content = content.decode('utf-8')
                css_content += content + "\n"
            except Exception as e:
                logger.warning(f"Failed to read CSS: {e}")

        # Create compactor with CSS for DOM optimization (oracle-verified)
        compactor = VerifiedCompactor(css_content) if css_content else None
        compressor = HTMLCompressor(compactor=compactor)

        if compactor:
            logger.info(
                f"VerifiedCompactor: {compactor.oracle.selector_count} selectors, "
                f"{len(compactor.oracle.failed_selectors)} failed"
            )

        extracted = 0

        for item in self.parser.spine:
            href = item.get('href', '')
            if not href:
                continue

            try:
                # Get raw content directly from EPUB ZIP
                # (bypasses ebooklib which modifies HTML - removes body class, head content)
                content = self.parser.get_raw_content(href)
                if content is None:
                    logger.warning(f"Item not found: {href}")
                    continue

                # Decode if bytes
                if isinstance(content, bytes):
                    content = content.decode('utf-8')

                # Compress HTML
                compressed, mapping = compressor.compress(content, author_css=css_content)

                # Save compressed content (.md - one translation unit per line)
                href_path = Path(href)
                file_stem = href_path.stem
                original_extension = href_path.suffix  # .html or .xhtml
                compressed_path = self.compressed_units_dir / f"{file_stem}.md"
                compressed_path.write_text(compressed, encoding='utf-8')

                # Save mapping for decompression (.mapping.json)
                # Include original extension for correct output filename
                mapping['original_extension'] = original_extension
                mapping_path = self.compressed_units_dir / f"{file_stem}.mapping.json"
                # Serialize first, then write — prevents truncated files if
                # json.dumps fails mid-serialization (e.g. non-serializable lxml objects)
                mapping_path.write_text(
                    json.dumps(_make_json_safe(mapping), ensure_ascii=False),
                    encoding='utf-8'
                )

                extracted += 1
                lines = len(compressed.splitlines()) if compressed else 0
                logger.debug(f"Compressed: {file_stem} ({lines} translatable lines)")

            except Exception as e:
                logger.warning(f"Failed to extract {href}: {e}")

        # Metadata is deliberately prepared as a separate, small work item.
        # The Antigravity subagent can translate it without touching OPF/XML,
        # while the builder remains responsible for applying only validated
        # values.  This also makes metadata translation resumable and auditable.
        self.create_metadata_translation_source(target_language=target_language)

        logger.info(f"Compressed {extracted} XHTML files to {self.compressed_units_dir}")
        return extracted

    def create_metadata_translation_source(
        self,
        target_language: Optional[str] = None,
    ) -> Path:
        """Write the metadata input contract for the workspace subagent.

        Author and publisher are intentionally placed in ``preserved_metadata``
        and are never included in the translatable payload.  The generated
        prompt asks the subagent to write ``translated_metadata.json`` next to
        this file.
        """
        from .toc_extractor import TOCExtractor

        runtime_config = getattr(self, "config", {})
        target_language = target_language or runtime_config.get("translation", {}).get(
            "target_language", "Chinese"
        )
        model = resolve_subagent_model(runtime_config, "metadata-translation")
        description = self.metadata.get("description") or ""
        rights = self.metadata.get("rights") or ""

        # OPF descriptions can contain markup.  Metadata translation is plain
        # text, so do not make the subagent reproduce arbitrary XML/HTML.
        description = re.sub(r"<[^>]+>", "", description).strip()
        rights = re.sub(r"<[^>]+>", "", rights).strip()

        toc = []
        for entry in TOCExtractor(self.parser).get_flat_toc():
            toc.append({
                "original": entry.get("title", ""),
                "href": entry.get("href", ""),
                "anchor": entry.get("anchor"),
                "level": entry.get("level", 1),
            })

        source = {
            "schema_version": 1,
            "workflow": "antigravity-subagent",
            "model": model,
            "source_language": self.source_language,
            "target_language": target_language,
            "target_language_code": self._get_language_code(target_language),
            "original_title": self.book_title,
            "preserved_metadata": {
                "author": self.metadata.get("author") or "",
                "publisher": self.metadata.get("publisher") or "",
            },
            "translatable_metadata": {
                "description": description,
                "rights": rights,
            },
            "toc": toc,
        }

        source_path = self.output_dir / "metadata_translation_source.json"
        source_path.write_text(
            json.dumps(source, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

        prompt_path = self.output_dir / "metadata_translation_prompt.md"
        prompt_path.write_text(
            self._metadata_translation_prompt(source_path.name, model),
            encoding="utf-8",
        )
        logger.info(f"Wrote metadata translation source: {source_path}")
        logger.info(f"Wrote metadata translation prompt: {prompt_path}")
        return source_path

    @staticmethod
    def _metadata_translation_prompt(source_filename: str, model: str = "gemini-3.1-pro-preview") -> str:
        """Return a concise, copyable subagent instruction."""
        return f"""# EPUB metadata translation

Recommended Antigravity model: `{model}`

Read `{source_filename}` and write `translated_metadata.json` in the same directory.

Rules:

1. Keep `original_title` exactly matching the `original_title` value in `{source_filename}` (do NOT translate `original_title`).
2. Translate title to `translated_title`.
3. Translate every `toc[].original` to `toc[].translated`; keep each entry's
   `href`, `anchor`, `level`, and order exactly unchanged.
4. Always include top-level `translated_description` and `translated_rights`.
   Translate non-empty `translatable_metadata.description` and `rights` into
   those fields (for example, "保留所有权利"); use an empty string when the
   corresponding source field is empty.
5. Copy `preserved_metadata.author` and `preserved_metadata.publisher` exactly
   into the output's `preserved_metadata` object. Never translate, transliterate,
   normalize, or omit these two fields.
6. Return valid JSON only. Do not wrap it in Markdown fences or add commentary.

The output must have this shape:

```json
{{
  "schema_version": 1,
  "original_title": "...",
  "translated_title": "...",
  "target_language": "...",
  "target_language_code": "...",
  "preserved_metadata": {{"author": "...", "publisher": "..."}},
  "toc": [{{"original": "...", "translated": "...", "href": "...", "anchor": null, "level": 1}}],
  "translated_description": "...",
  "translated_rights": "..."
}}
```
"""

    def _merge_part_files(self) -> Dict[str, str]:
        """
        Merge split part files (*.part1.md, *.part2.md, etc.) into combined content.

        When files are split for translation due to size limits, they produce
        files like split_023.part1.md, split_023.part2.md. This method merges
        them back together.

        Returns:
            Dict mapping base_name -> merged_content
        """
        merged = self._merge_part_files_in_dir(self.translated_dir)
        part_groups = self._group_part_files(self.translated_dir)
        for base_name in merged:
            parts = part_groups.get(base_name, [])
            logger.debug(f"Merged {len(parts)} parts for {base_name}")

        return merged

    @staticmethod
    def _group_part_files(directory: Path) -> Dict[str, List[tuple[int, Path]]]:
        """Group *.partN.md files by logical base stem."""
        from collections import defaultdict

        part_files: Dict[str, List[tuple[int, Path]]] = defaultdict(list)
        for file_path in directory.glob("*.part*.md"):
            match = PART_FILE_RE.match(file_path.name)
            if match:
                part_files[match.group(1)].append((int(match.group(2)), file_path))

        for parts in part_files.values():
            parts.sort(key=lambda item: item[0])

        return dict(part_files)

    @classmethod
    def _merge_part_files_in_dir(cls, directory: Path) -> Dict[str, str]:
        """Merge split part files in a directory into base-stem content."""
        merged = {}
        for base_name, parts in cls._group_part_files(directory).items():
            merged[base_name] = '\n'.join(
                part_file.read_text(encoding='utf-8')
                for _part_num, part_file in parts
            )
        return merged

    def _logical_unit_records(self, merged_translated: Optional[Dict[str, str]] = None) -> List[Dict[str, Any]]:
        """Return logical compressed units using mapping files as the source of truth."""
        merged_translated = merged_translated or {}
        merged_sources = self._merge_part_files_in_dir(self.compressed_units_dir)
        records = []

        for mapping_path in sorted(self.compressed_units_dir.glob("*.mapping.json")):
            base_stem = mapping_path.name[:-len(".mapping.json")]
            try:
                mapping = json.loads(mapping_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                mapping = {}

            source_path = self.compressed_units_dir / f"{base_stem}.md"
            translated_path = self.translated_dir / f"{base_stem}.md"

            source_content = ""
            if source_path.exists():
                source_content = source_path.read_text(encoding="utf-8")
            elif base_stem in merged_sources:
                source_content = merged_sources[base_stem]

            translated_content = ""
            translated_exists = False
            if base_stem in merged_translated:
                translated_content = merged_translated[base_stem]
                translated_exists = True
            elif translated_path.exists():
                translated_content = translated_path.read_text(encoding="utf-8")
                translated_exists = True

            original_ext = mapping.get("original_extension", ".xhtml")
            records.append({
                "base_stem": base_stem,
                "mapping_path": mapping_path,
                "mapping": mapping,
                "source_path": source_path,
                "source_content": source_content,
                "translated_path": translated_path,
                "translated_content": translated_content,
                "translated_exists": translated_exists,
                "final_path": self.final_dir / f"{base_stem}{original_ext}",
            })

        return records

    @staticmethod
    def _read_jsonl(path: Path) -> List[Dict[str, Any]]:
        """Read a JSONL file, skipping malformed lines."""
        if not path.exists():
            return []
        rows = []
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return rows

    def _trace_stats_for_file(self, trace_rows: List[Dict[str, Any]], file_stem: str) -> Dict[str, Any]:
        """Summarize LLM trace entries for one compressed HTML unit."""
        operation = f"Translate {file_stem}"
        rows = [row for row in trace_rows if row.get("operation") == operation]
        if not rows:
            return {
                "requests": 0,
                "errors": 0,
                "input_tokens": 0,
                "output_tokens": 0,
                "cache_read_tokens": 0,
                "cache_write_tokens": 0,
                "usage_statuses": {},
            }

        usage_statuses: Dict[str, int] = {}
        for row in rows:
            status = row.get("usage_status", "legacy")
            usage_statuses[status] = usage_statuses.get(status, 0) + 1

        return {
            "requests": len(rows),
            "errors": sum(1 for row in rows if row.get("error")),
            "input_tokens": sum(int(row.get("input_tokens") or 0) for row in rows),
            "output_tokens": sum(int(row.get("output_tokens") or 0) for row in rows),
            "cache_read_tokens": sum(int(row.get("cache_read_tokens") or 0) for row in rows),
            "cache_write_tokens": sum(int(row.get("cache_write_tokens") or 0) for row in rows),
            "usage_statuses": usage_statuses,
            "last_duration_ms": rows[-1].get("duration_ms"),
            "last_error": rows[-1].get("error"),
        }

    def _agent_stats_for_file(self, file_stem: str) -> Dict[str, Any]:
        """Summarize agent-loop artifacts for one translated unit."""
        artifact_dir = self.output_dir / "logs" / "agent_artifacts" / file_stem
        originals_dir = artifact_dir / "originals"
        workspace_dir = artifact_dir / "workspace"
        metrics_path = workspace_dir / "_agent_round_metrics.jsonl"
        metrics = self._read_jsonl(metrics_path)

        return {
            "artifact_dir": str(artifact_dir) if artifact_dir.exists() else None,
            "raw_output_exists": (originals_dir / "raw_output.txt").exists(),
            "continuation_count": len(list(originals_dir.glob("continuation_*.txt"))),
            "rounds": len(metrics),
            "actions": [row.get("decision_action") for row in metrics if row.get("decision_action")],
            "final_status": metrics[-1].get("status") if metrics else None,
            "final_action": metrics[-1].get("decision_action") if metrics else None,
        }

    def write_translation_report(
        self,
        output_epub: Optional[Path] = None,
        phase: str = "translation",
    ) -> Path:
        """Write a machine-readable report for compressed HTML translation state."""
        metadata_path = self.output_dir / "translated_metadata.json"
        translated_metadata = {}
        if metadata_path.exists():
            try:
                translated_metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                translated_metadata = {}

        trace_path = self.output_dir / "logs" / "llm_trace.jsonl"
        trace_rows = self._read_jsonl(trace_path)

        files = []
        summary = {
            "total_files": 0,
            "translated_files": 0,
            "missing_translations": 0,
            "line_mismatch_files": 0,
            "tag_mismatch_files": 0,
            "total_continuations": 0,
        }

        merged_parts = self._merge_part_files_in_dir(self.translated_dir)
        for record in self._logical_unit_records(merged_parts):
            file_stem = record["base_stem"]
            source_text = record["source_content"]
            translated_text = record["translated_content"]
            source_lines = nonempty_lines(source_text)
            translated_lines = nonempty_lines(translated_text)

            compared = min(len(source_lines), len(translated_lines))
            tag_mismatches = tag_mismatch_count(
                source_lines[:compared],
                translated_lines[:compared],
            )

            agent_stats = self._agent_stats_for_file(file_stem)
            trace_stats = self._trace_stats_for_file(trace_rows, file_stem)
            continuation_count = agent_stats.get("continuation_count", 0)

            file_report = {
                "file": file_stem,
                "source_lines": len(source_lines),
                "translated_lines": len(translated_lines),
                "line_count_match": len(source_lines) == len(translated_lines),
                "tag_mismatches": tag_mismatches,
                "translated_exists": record["translated_exists"],
                "final_xhtml_exists": record["final_path"].exists(),
                "agent": agent_stats,
                "llm_trace": trace_stats,
            }
            files.append(file_report)

            summary["total_files"] += 1
            if record["translated_exists"]:
                summary["translated_files"] += 1
            else:
                summary["missing_translations"] += 1
            if record["translated_exists"] and len(source_lines) != len(translated_lines):
                summary["line_mismatch_files"] += 1
            if tag_mismatches:
                summary["tag_mismatch_files"] += 1
            summary["total_continuations"] += continuation_count

        report = {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "phase": phase,
            "book_title": self.book_title,
            "source_epub": str(self.epub_path),
            "output_epub": str(output_epub) if output_epub else None,
            "translated_title": translated_metadata.get("translated_title"),
            "target_language": translated_metadata.get("target_language"),
            "target_language_code": translated_metadata.get("target_language_code"),
            "metadata_validation": self.validate_translated_metadata(),
            "summary": summary,
            "files": files,
            "logs": {
                "llm_trace": str(trace_path),
                "prepare_log": str(self.output_dir / "logs" / "html-prepare.log"),
                "build_log": str(self.output_dir / "logs" / "build-html-epub.log"),
            },
        }

        report_path = self.output_dir / "translation_report.json"
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        logger.info(f"Wrote translation report: {report_path}")
        return report_path

    def validate_translated_units(self) -> Dict[str, Any]:
        """
        Validate all translated compressed units against their sources.
        Checks:
        1. Translated file exists
        2. Non-empty line count matches original
        3. HTML tag sequence matches on each line
        4. Target language content exists (Chinese characters)
        5. Content omission checks

        Returns:
            Dict summary with keys: total, completed, valid, invalid (list), missing (list), all_passed (bool)
        """
        all_sources = sorted(self.compressed_units_dir.glob("*.md"))
        total = len(all_sources)
        missing = []
        invalid = []
        valid = 0
        valid_files = []
        source_sha256 = {}

        for src_file in all_sources:
            tgt_file = self.translated_dir / src_file.name
            if not tgt_file.exists():
                missing.append(src_file.name)
                continue

            src_content = src_file.read_text(encoding="utf-8")
            tgt_content = tgt_file.read_text(encoding="utf-8")
            source_sha256[src_file.name] = hashlib.sha256(src_content.encode("utf-8")).hexdigest()

            src_lines = nonempty_lines(src_content)
            tgt_lines = nonempty_lines(tgt_content)
            if len(src_lines) != len(tgt_lines):
                invalid.append({
                    "file": src_file.name,
                    "reason": f"Line count mismatch: expected {len(src_lines)}, got {len(tgt_lines)}",
                })
                continue
            mismatches = tag_mismatch_count(src_lines, tgt_lines)
            if mismatches:
                invalid.append({
                    "file": src_file.name,
                    "reason": f"HTML tag structure mismatch in {mismatches} line(s)",
                })
                continue
            if not tgt_content.strip() and src_content.strip():
                invalid.append({"file": src_file.name, "reason": "target is empty"})
                continue
            if "```" in tgt_content:
                invalid.append({"file": src_file.name, "reason": "Markdown code fence is not allowed"})
                continue
            else:
                valid += 1
                valid_files.append(src_file.name)

        metadata_report = self.validate_translated_metadata()

        return {
            "total": total,
            "completed": total - len(missing),
            "valid": valid,
            "invalid": invalid,
            "missing": missing,
            "valid_files": valid_files,
            "source_sha256": source_sha256,
            "metadata": metadata_report,
            "all_passed": (
                len(missing) == 0
                and len(invalid) == 0
                and total > 0
                and metadata_report["valid"]
            )
        }

    def validate_translated_metadata(self) -> Dict[str, Any]:
        """Validate the subagent metadata hand-off without contacting an LLM."""
        source_path = self.output_dir / "metadata_translation_source.json"
        translated_path = self.output_dir / "translated_metadata.json"
        errors: List[str] = []

        if not source_path.exists():
            errors.append(
                "metadata_translation_source.json is missing; run html-prepare again"
            )
        if not translated_path.exists():
            errors.append(
                "translated_metadata.json is missing; ask the subagent to create it"
            )
        if errors:
            return {"valid": False, "errors": errors}

        try:
            source = json.loads(source_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            return {"valid": False, "errors": [f"invalid metadata source JSON: {exc}"]}
        try:
            translated = json.loads(translated_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            return {"valid": False, "errors": [f"invalid translated metadata JSON: {exc}"]}

        if not isinstance(translated, dict):
            errors.append("translated metadata must be a JSON object")
            return {"valid": False, "errors": errors}

        if translated.get("schema_version") != source.get("schema_version", 1):
            errors.append("schema_version does not match metadata source")
        if translated.get("original_title") != source.get("original_title"):
            errors.append("original_title does not match metadata source")
        if not str(translated.get("translated_title") or "").strip():
            errors.append("translated_title is missing or empty")
        for key in ("target_language", "target_language_code"):
            if translated.get(key) != source.get(key):
                errors.append(f"{key} does not match metadata source")

        source_preserved = source.get("preserved_metadata", {})
        translated_preserved = translated.get("preserved_metadata", {})
        if not isinstance(translated_preserved, dict):
            errors.append("preserved_metadata must be an object")
        else:
            for key in ("author", "publisher"):
                if translated_preserved.get(key) != source_preserved.get(key, ""):
                    errors.append(f"preserved_metadata.{key} was changed or omitted")

        source_toc = source.get("toc", [])
        translated_toc = translated.get("toc")
        if not isinstance(translated_toc, list):
            errors.append("toc must be an array")
        elif len(translated_toc) != len(source_toc):
            errors.append(
                f"toc entry count mismatch: expected {len(source_toc)}, "
                f"got {len(translated_toc)}"
            )
        else:
            for index, (expected, actual) in enumerate(zip(source_toc, translated_toc)):
                if not isinstance(actual, dict):
                    errors.append(f"toc[{index}] must be an object")
                    continue
                actual_href = actual.get("href", "")
                actual_anchor = actual.get("anchor")
                # Accept the legacy writer's combined ``href#anchor`` form,
                # but compare the canonical path and fragment separately.
                if actual_anchor is None and isinstance(actual_href, str) and "#" in actual_href:
                    actual_href, actual_anchor = actual_href.split("#", 1)
                for key, value in (
                    ("original", actual.get("original")),
                    ("href", actual_href),
                    ("anchor", actual_anchor),
                    ("level", actual.get("level")),
                ):
                    if value != expected.get(key):
                        errors.append(f"toc[{index}].{key} was changed")
                if not str(actual.get("translated") or "").strip():
                    errors.append(f"toc[{index}].translated is missing or empty")

        source_extra = source.get("translatable_metadata", {})
        for key, source_value in (
            ("translated_description", source_extra.get("description")),
            ("translated_rights", source_extra.get("rights")),
        ):
            if key not in translated:
                errors.append(f"{key} field is missing")
            elif not isinstance(translated.get(key), str):
                errors.append(f"{key} must be a string")
            elif source_value and not translated[key].strip():
                errors.append(f"{key} is missing or empty")

        return {
            "valid": not errors,
            "errors": errors,
            "source": str(source_path),
            "translated": str(translated_path),
        }

    def postprocess_and_build(
        self,
        output_epub: Optional[Path] = None,
        allow_partial: bool = False,
    ) -> Path:
        """
        Decompress translated content and build final EPUB.

        Uses HTMLCompressor to reconstruct full HTML from translated
        compressed content and saved mappings.

        Args:
            output_epub: Optional output path

        Returns:
            Path to built EPUB
        """
        from .compressor import HTMLCompressor

        validation = self.validate_translated_units()
        if not validation["all_passed"] and not allow_partial:
            metadata_errors = validation.get("metadata", {}).get("errors", [])
            detail = list(metadata_errors)
            detail.extend(
                f"{item['file']}: {item['reason']}"
                for item in validation.get("invalid", [])
            )
            detail.extend(
                f"missing unit: {name}" for name in validation.get("missing", [])
            )
            raise ValueError(
                "Translation validation failed; refusing to build EPUB. "
                "Run html-validate or use --allow-partial explicitly. "
                + "; ".join(detail[:10])
            )
        if not validation["all_passed"]:
            logger.warning("Building partial EPUB because allow_partial=True")

        compressor = HTMLCompressor()

        # First, merge any split part files
        merged_parts = self._merge_part_files()
        if merged_parts:
            logger.info(f"Merged {len(merged_parts)} split files")

        for record in self._logical_unit_records(merged_parts):
            base_stem = record["base_stem"]
            if not record["translated_exists"]:
                logger.warning(f"No translation found for {base_stem}")
                continue

            if base_stem in merged_parts:
                logger.debug(f"Using merged content for {base_stem}")

            restored = compressor.decompress(record["translated_content"], record["mapping"])
            record["final_path"].write_text(restored, encoding='utf-8')
            logger.debug(f"Decompressed: {base_stem}")

        # Load translated metadata.  A complete build always has a validated
        # metadata hand-off; partial builds may intentionally omit it.
        metadata_path = self.output_dir / "translated_metadata.json"
        translated_metadata = None
        if metadata_path.exists():
            with open(metadata_path, 'r', encoding='utf-8') as f:
                translated_metadata = json.load(f)

        # Build EPUB with translated title as filename
        if output_epub is None:
            if translated_metadata and translated_metadata.get('translated_title'):
                safe_title = sanitize_filename(translated_metadata['translated_title'])
                output_epub = self.output_dir / f"{safe_title}.epub"
            else:
                output_epub = self.output_dir / f"translated_{self.epub_path.name}"

        result_path = build_html_epub(
            original_epub=self.epub_path,
            translated_dir=self.final_dir,
            output_path=output_epub,
            book_title=self.book_title,
            translated_metadata=translated_metadata,
            epubcheck_mode=self.config.get('html_translation', {}).get(
                'epubcheck_mode', 'warn'
            ),
            epubcheck_path=self.config.get('html_translation', {}).get(
                'epubcheck_path'
            ),
        )
        self.write_translation_report(output_epub=result_path, phase="build")
        return result_path
