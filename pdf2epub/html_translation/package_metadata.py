"""Translated OPF metadata mutation, separate from navigation updates."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, Optional
from loguru import logger
from lxml import etree as LET

from .build_contracts import BuildConfig
from .package_paths import has_local_tag, safe_lxml_parser
from .metadata_values import sort_author_for_library, sort_title_for_library


@dataclass(frozen=True)
class PackageMetadataContext:
    config: BuildConfig
    find_opf_path: Callable[[Path], Optional[Path]]


class EPUBMetadataUpdater:
    """Apply validated title/language/author/description fields to content.opf."""
    def __init__(self, context: PackageMetadataContext):
        self.config = context.config
        self.find_opf_path = context.find_opf_path
        self._find_opf_path = context.find_opf_path
    def update_opf(self, extract_dir: Path, metadata: Dict):
        """Update content.opf with translated title and language."""
        opf_path = self._find_opf_path(extract_dir)
        if not opf_path:
            logger.warning("No OPF file found")
            return

        translated_title = metadata.get('translated_title')
        target_lang_code = metadata.get('target_language_code')
        # Sort values are bibliographic indexes, not prose to be translated.
        # Derive them locally so older hand-offs (and subagent omissions) still
        # produce correct Calibre metadata.
        # Sort the translated display title.  If a translation is not
        # available (for a partial preview), fall back to the source title.
        # This still handles an untranslated ``The Class Matrix`` as
        # ``Class Matrix, The``.
        title_for_sort = metadata.get('translated_title') or self.config.book_title
        title_sort = sort_title_for_library(title_for_sort)
        explicit_author_sort = metadata.get('translated_author_file_as') or metadata.get(
            'translated_author_sort'
        )

        if not translated_title and not target_lang_code and not title_sort:
            return

        try:
            # lxml preserves the source namespace map when serializing. The
            # stdlib ElementTree can emit OPF namespaced attributes such as
            # opf:role as invalid unqualified attributes when the OPF namespace
            # is also the document's default namespace.
            tree = LET.parse(str(opf_path), parser=safe_lxml_parser())
            root = tree.getroot()

            # Handle namespaces - OPF uses default namespace
            namespaces = {
                'opf': 'http://www.idpf.org/2007/opf',
                'dc': 'http://purl.org/dc/elements/1.1/'
            }
            is_epub3 = str(root.get('version', '')).startswith('3')
            metadata_elem = next(
                (element for element in root.iter() if has_local_tag(element, 'metadata')),
                None,
            )

            # Find and update dc:title
            if translated_title:
                # Try with namespace first
                title_elem = root.find('.//dc:title', namespaces)
                if title_elem is None:
                    # Try without namespace (some EPUBs don't use namespaces properly)
                    for elem in root.iter():
                        if has_local_tag(elem, 'title'):
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
                        if has_local_tag(elem, 'language'):
                            lang_elem = elem
                            break

                if lang_elem is not None:
                    old_lang = lang_elem.text
                    lang_elem.text = target_lang_code
                    logger.debug(f"Updated content.opf language: {old_lang} -> {target_lang_code}")
                else:
                    logger.warning("dc:language element not found in content.opf")

            # Keep creator text unchanged, but update its library sort value.
            # EPUB 2 stores this as opf:file-as; EPUB 3 stores it as a
            # refines/property meta element.
            creator_elements = [
                element
                for element in root.iter()
                if has_local_tag(element, 'creator')
                and has_local_tag(element.getparent(), 'metadata')
            ]
            if isinstance(explicit_author_sort, list):
                explicit_author_values = [str(value).strip() for value in explicit_author_sort]
            elif len(creator_elements) == 1 and isinstance(explicit_author_sort, str):
                explicit_author_values = [explicit_author_sort.strip()]
            else:
                explicit_author_values = []

            for index, creator in enumerate(creator_elements):
                sort_value = (
                    explicit_author_values[index]
                    if index < len(explicit_author_values) and explicit_author_values[index]
                    else sort_author_for_library(creator.text or '')
                )
                if not sort_value:
                    continue

                namespaced_file_as = f"{{{namespaces['opf']}}}file-as"
                if namespaced_file_as in creator.attrib:
                    creator.set(namespaced_file_as, sort_value)
                elif not is_epub3:
                    creator.set(namespaced_file_as, sort_value)

                creator_id = creator.get('id')
                refinement = None
                if creator_id:
                    refinement = next(
                        (
                            element
                            for element in root.iter()
                            if has_local_tag(element, 'meta')
                            and element.get('refines') == f'#{creator_id}'
                            and element.get('property') == 'file-as'
                        ),
                        None,
                    )
                if refinement is not None:
                    refinement.text = sort_value
                elif is_epub3 and creator_id and metadata_elem is not None:
                    LET.SubElement(
                        metadata_elem,
                        f"{{{namespaces['opf']}}}meta",
                        refines=f'#{creator_id}',
                        property='file-as',
                    ).text = sort_value

            # Some Calibre-generated packages also carry an explicit author
            # sort meta. Update it when present, without adding a nonstandard
            # duplicate to packages that use creator file-as/refinements.
            if creator_elements:
                first_author_sort = (
                    explicit_author_values[0]
                    if explicit_author_values and explicit_author_values[0]
                    else sort_author_for_library(creator_elements[0].text or '')
                )
                for elem in root.iter():
                    if has_local_tag(elem, 'meta') and elem.get('name') == 'calibre:author_sort':
                        elem.set('content', first_author_sort)
                        break

            # Update dc:description
            if metadata.get('translated_description'):
                desc_elem = root.find('.//dc:description', namespaces)
                if desc_elem is None:
                    for e in root.iter():
                        if has_local_tag(e, 'description'):
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
                        if has_local_tag(e, 'rights'):
                            rights_elem = e
                            break
                if rights_elem is not None:
                    rights_elem.text = metadata['translated_rights']
                    logger.debug("Updated rights")

            # Update (or create) calibre:title_sort.  The sort value follows
            # the translated display title, so the Chinese title is
            # sorted as Chinese; an untranslated ``The Class Matrix`` would be
            # stored as ``Class Matrix, The``.
            title_sort_elem = None
            for elem in root.iter():
                if has_local_tag(elem, 'meta') and elem.get('name') == 'calibre:title_sort':
                    title_sort_elem = elem
                    break
            if title_sort:
                if title_sort_elem is None and metadata_elem is not None:
                    title_sort_elem = LET.SubElement(
                        metadata_elem,
                        f"{{{namespaces['opf']}}}meta",
                        name='calibre:title_sort',
                    )
                if title_sort_elem is not None:
                    title_sort_elem.set('content', title_sort)
                    logger.debug(f"Updated title_sort: {title_sort}")

                # EPUB 3 uses a title refinement as the canonical sort value;
                # calibre:title_sort alone is not enough for readers that
                # display the EPUB 3 ``file-as`` metadata.  EPUB 2 packages
                # may carry the equivalent namespaced attribute directly on
                # dc:title, so keep that value in sync when it is present.
                title_elem = root.find('.//dc:title', namespaces)
                if title_elem is None:
                    title_elem = next(
                        (element for element in root.iter() if has_local_tag(element, 'title')),
                        None,
                    )
                if title_elem is not None:
                    namespaced_file_as = f"{{{namespaces['opf']}}}file-as"
                    if namespaced_file_as in title_elem.attrib:
                        title_elem.set(namespaced_file_as, title_sort)

                    title_id = title_elem.get('id')
                    title_refinement = None
                    if title_id:
                        title_refinement = next(
                            (
                                element
                                for element in root.iter()
                                if has_local_tag(element, 'meta')
                                and element.get('refines') == f'#{title_id}'
                                and element.get('property') == 'file-as'
                            ),
                            None,
                        )
                    if title_refinement is not None:
                        title_refinement.text = title_sort
                    elif is_epub3 and title_id and metadata_elem is not None:
                        LET.SubElement(
                            metadata_elem,
                            f"{{{namespaces['opf']}}}meta",
                            refines=f'#{title_id}',
                            property='file-as',
                        ).text = title_sort
                    logger.debug(f"Updated title file-as: {title_sort}")

            tree.write(str(opf_path), encoding='utf-8', xml_declaration=True)

        except Exception as e:
            logger.warning("Failed to update content.opf: {}", e)
