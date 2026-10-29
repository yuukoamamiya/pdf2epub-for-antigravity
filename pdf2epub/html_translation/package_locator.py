"""Safe EPUB package path discovery."""
from __future__ import annotations

from pathlib import Path
from typing import Dict, Optional
from defusedxml import ElementTree as ET
from loguru import logger

from .package_paths import (
    UnsafePackagePath,
    has_local_tag,
    safe_existing_package_file,
    safe_package_path,
)

def find_opf_path(extract_dir: Path) -> Optional[Path]:
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
        opf_candidates = [
            safe_path
            for candidate in extract_dir.rglob("*.opf")
            if (safe_path := safe_existing_package_file(extract_dir, candidate))
        ]
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
                if has_local_tag(elem, 'rootfile'):
                    rootfile = elem
                    break

        if rootfile is not None:
            full_path = rootfile.get('full-path')
            if full_path:
                opf_path = safe_package_path(
                    extract_dir,
                    full_path,
                    label="container.xml full-path",
                )
                if opf_path.is_file():
                    return opf_path

        # Fallback to glob
        opf_candidates = [
            safe_path
            for candidate in extract_dir.rglob("*.opf")
            if (safe_path := safe_existing_package_file(extract_dir, candidate))
        ]
        return opf_candidates[0] if opf_candidates else None

    except UnsafePackagePath as exc:
        logger.warning(f"Rejected unsafe EPUB package path: {exc}")
        return None
    except Exception as e:
        logger.debug(f"Error parsing container.xml: {e}, falling back to glob")
        opf_candidates = [
            safe_path
            for candidate in extract_dir.rglob("*.opf")
            if (safe_path := safe_existing_package_file(extract_dir, candidate))
        ]
        return opf_candidates[0] if opf_candidates else None

def find_toc_files(extract_dir: Path, opf_path: Path) -> Dict[str, Optional[Path]]:
    """
    Find NCX and Nav files by parsing the OPF manifest.

    Returns dict with keys:
    - 'ncx': Path to NCX file (EPUB 2 TOC, media-type="application/x-dtbncx+xml")
    - 'nav': Path to Nav document (EPUB 3 TOC, properties="nav")
    """
    result = {'ncx': None, 'nav': None}
    opf_path = safe_existing_package_file(extract_dir, opf_path)
    if opf_path is None:
        logger.warning("Rejected OPF path outside extracted EPUB")
        return result
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
                if has_local_tag(elem, 'manifest'):
                    manifest = elem
                    break

        if manifest is None:
            logger.debug("No manifest found in OPF")
            return result

        # Search all items in manifest
        for item in manifest:
            if not has_local_tag(item, 'item'):
                continue

            media_type = item.get('media-type', '')
            properties = item.get('properties', '')
            href = item.get('href', '')

            if not href:
                continue

            # NCX: identified by media-type
            if media_type == 'application/x-dtbncx+xml':
                ncx_path = safe_package_path(
                    extract_dir,
                    href,
                    base=opf_dir,
                    label="OPF NCX href",
                )
                if ncx_path.is_file():
                    result['ncx'] = ncx_path
                    logger.debug(f"Found NCX via manifest: {ncx_path}")

            # Nav: identified by properties="nav"
            if 'nav' in properties.split():
                nav_path = safe_package_path(
                    extract_dir,
                    href,
                    base=opf_dir,
                    label="OPF navigation href",
                )
                if nav_path.is_file():
                    result['nav'] = nav_path
                    logger.debug(f"Found Nav via manifest: {nav_path}")

        # Also check spine for toc attribute (alternative NCX reference)
        if result['ncx'] is None:
            spine = root.find('.//opf:spine', ns)
            if spine is None:
                for elem in root.iter():
                    if has_local_tag(elem, 'spine'):
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
                                ncx_path = safe_package_path(
                                    extract_dir,
                                    href,
                                    base=opf_dir,
                                    label="OPF spine NCX href",
                                )
                                if ncx_path.is_file():
                                    result['ncx'] = ncx_path
                                    logger.debug(f"Found NCX via spine toc attr: {ncx_path}")
                            break

    except UnsafePackagePath as exc:
        logger.warning(f"Rejected unsafe EPUB navigation path: {exc}")
    except Exception as e:
        logger.debug(f"Error parsing OPF for TOC files: {e}")

    return result
