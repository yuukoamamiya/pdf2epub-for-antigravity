"""NCX and nav document updates for a prepared EPUB package."""
from __future__ import annotations

import re

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional
from loguru import logger
from lxml import etree as LET
from defusedxml import ElementTree as ET

from .package_paths import has_local_tag, package_relative_href, toc_entry_href


def record_navigation(navigation_report, kind, status, path=None, extract_dir=None, updated_entries=0, error=None):
    relative_path = None
    if path is not None:
        try:
            relative_path = path.resolve().relative_to(Path(extract_dir).resolve()).as_posix() if extract_dir else path.as_posix()
        except ValueError:
            relative_path = path.name
    entry = {"status": status, "path": relative_path, "updated_entries": updated_entries}
    if error:
        entry["error"] = error
    navigation_report[kind] = entry


@dataclass(frozen=True)
class NavigationUpdateContext:
    navigation_report: Dict[str, Any]
    find_opf_path: Callable[[Path], Optional[Path]]
    find_toc_files: Callable[[Path, Path], Dict[str, Optional[Path]]]


class EPUBNavigationUpdater:
    """Update NCX and nav documents using explicit package context."""
    def __init__(self, context: NavigationUpdateContext):
        self.context = context
        self.navigation_report = context.navigation_report

    def _find_opf_path(self, extract_dir):
        return self.context.find_opf_path(extract_dir)

    def _find_toc_files(self, extract_dir, opf_path):
        return self.context.find_toc_files(extract_dir, opf_path)

    def _record_navigation(self, kind, status, path=None, extract_dir=None, updated_entries=0, error=None):
        record_navigation(self.navigation_report, kind, status, path, extract_dir, updated_entries, error)


    def update_toc_ncx(self, extract_dir: Path, metadata: Dict):
        """Update toc.ncx with translated chapter titles."""
        # Find NCX file via OPF manifest (proper EPUB way)
        opf_path = self._find_opf_path(extract_dir)
        if not opf_path:
            logger.debug("No OPF found, cannot locate NCX")
            self._record_navigation("ncx", "not_found", extract_dir=extract_dir)
            return

        toc_files = self._find_toc_files(extract_dir, opf_path)
        ncx_path = toc_files.get('ncx')

        if not ncx_path:
            # Fallback to glob (for malformed EPUBs)
            ncx_candidates = list(extract_dir.rglob("*.ncx"))
            if not ncx_candidates:
                logger.debug("No NCX file found (EPUB 3 only?)")
                self._record_navigation("ncx", "not_found", extract_dir=extract_dir)
                return
            ncx_path = ncx_candidates[0]

        ncx_dir = ncx_path.parent  # For resolving relative paths
        toc_entries = metadata.get('toc', [])
        if not toc_entries:
            self._record_navigation("ncx", "skipped", ncx_path, extract_dir=extract_dir)
            return

        # Build multiple mappings for robust matching
        # 1. Full href -> title (exact match)
        # 2. Basename + fragment -> title (for when paths differ)
        # 3. Normalized href -> title
        href_to_title = {}
        basename_to_title = {}
        for entry in toc_entries:
            href = toc_entry_href(entry)
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
                resolved_str = package_relative_href(extract_dir, ncx_dir, src)
                if resolved_str in href_to_title:
                    return href_to_title[resolved_str]

            # 3. Match by basename + fragment
            basename = Path(src.split('#', 1)[0]).name
            if '#' in src:
                basename += '#' + src.split('#', 1)[1]
            if basename in basename_to_title:
                return basename_to_title[basename]

            return None

        try:
            # ``defusedxml.ElementTree`` intentionally exposes the safe
            # parsing API but does not provide stdlib's ``register_namespace``
            # helper.  Namespace registration is not needed to update or
            # serialize an already namespaced NCX tree; calling it here made
            # every EPUB 2 TOC update fail before any labels were written.
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
                        if has_local_tag(elem, 'text'):
                            parent = elem.getparent() if hasattr(elem, 'getparent') else None
                            if (
                                parent is not None
                                and has_local_tag(parent, 'docTitle')
                            ):
                                doc_title = elem
                                break
                if doc_title is not None:
                    doc_title.text = translated_title

            # Update navPoints
            updated_count = 0
            for nav_point in root.iter():
                if has_local_tag(nav_point, 'navPoint'):
                    # Find content src
                    content = None
                    for child in nav_point:
                        if has_local_tag(child, 'content'):
                            content = child
                            break

                    if content is not None:
                        src = content.get('src', '')
                        translated = find_translation(src)

                        if translated:
                            # Find navLabel/text and update
                            for child in nav_point:
                                if has_local_tag(child, 'navLabel'):
                                    for text_elem in child:
                                        if has_local_tag(text_elem, 'text'):
                                            text_elem.text = translated
                                            updated_count += 1
                                            break
                                    break

            tree.write(ncx_path, encoding='utf-8', xml_declaration=True)
            logger.debug(f"Updated {updated_count} navPoints in toc.ncx")
            self._record_navigation(
                "ncx", "updated", ncx_path, extract_dir, updated_count
            )

        except Exception as e:
            logger.warning("Failed to update toc.ncx: {}", e)
            self._record_navigation(
                "ncx", "warning", ncx_path, extract_dir, error=str(e)
            )

    def update_nav_xhtml(self, extract_dir: Path, metadata: Dict):
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
                self._record_navigation("nav", "not_found", extract_dir=extract_dir)
                return

            nav_path = nav_candidates[0]
        nav_dir = nav_path.parent
        toc_entries = metadata.get('toc', [])
        if not toc_entries:
            self._record_navigation("nav", "skipped", nav_path, extract_dir=extract_dir)
            return

        # Build multiple mappings for robust matching (like NCX)
        href_to_title = {}
        basename_to_title = {}
        original_to_title = {}
        for entry in toc_entries:
            href = toc_entry_href(entry)
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
                    resolved_str = package_relative_href(extract_dir, nav_dir, href)
                    if resolved_str in href_to_title:
                        translated = href_to_title[resolved_str]

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
            self._record_navigation(
                "nav", "updated", nav_path, extract_dir,
                updated_count + structural_updated_count,
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
            self._record_navigation("nav", "updated", nav_path, extract_dir, updated_count)

        except Exception as e:
            logger.warning("Failed to update nav.xhtml: {}", e)
            self._record_navigation(
                "nav", "warning", nav_path, extract_dir, error=str(e)
            )
