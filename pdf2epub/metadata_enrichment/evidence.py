"""Bounded local metadata evidence extraction; never performs OCR or semantic decisions."""

from __future__ import annotations

import hashlib
import json
import re
import zipfile
from pathlib import Path
from defusedxml import ElementTree
from defusedxml.common import DefusedXmlException

MAX_FILE = 2_000_000
MAX_TEXT = 40_000


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _safe_text(path: Path, limit: int = MAX_TEXT, *, root: Path | None = None) -> str:
    try:
        if root is not None and not path.resolve().is_relative_to(root.resolve()):
            return ""
        if path.is_file() and path.stat().st_size <= MAX_FILE:
            return path.read_text(encoding="utf-8", errors="replace")[:limit]
    except OSError:
        pass
    return ""


def collect_evidence(output_dir: Path, config: dict, *, source_path: str | Path | None = None, epub_path: str | Path | None = None, max_pages: int = 5) -> dict:
    evidence: list[dict] = []
    # OCR first pages already present: finite count and per-file size cap.
    for path in sorted((output_dir / "pages").glob("*.md"))[:max_pages]:
        text = _safe_text(path, 8000, root=output_dir)
        if text:
            evidence.append({"kind": "existing_ocr", "location": path.relative_to(output_dir).as_posix(), "sha256": digest(text.encode()), "text": text})
    for path in (output_dir / "toc.md", output_dir / "translated_toc.md"):
        text = _safe_text(path, 5000, root=output_dir)
        if text:
            evidence.append({"kind": "local_frontmatter", "location": path.relative_to(output_dir).as_posix(), "sha256": digest(text.encode()), "text": text})
    configured_epub = config.get("input_epub") or config.get("epub")
    if not configured_epub and source_path and Path(source_path).suffix.lower() == ".epub":
        configured_epub = source_path
    epub_path = Path(epub_path or configured_epub).expanduser() if (epub_path or configured_epub) else None
    if epub_path and epub_path.is_file() and epub_path.suffix.lower() == ".epub":
        evidence.extend(_epub_evidence(epub_path))
    source = source_path or config.get("input_pdf") or config.get("input_epub") or config.get("input")
    source_hash = None
    if source:
        p = Path(str(source)).expanduser()
        try:
            if p.is_file():
                h = hashlib.sha256()
                with p.open("rb") as f:
                    for block in iter(lambda: f.read(1024 * 1024), b""):
                        h.update(block)
                source_hash = h.hexdigest()
        except OSError:
            pass
    meta_config = config.get("metadata_enrichment") or {}
    payload = {
        "schema_version": 1,
        "configured_source": config.get("input_pdf") or config.get("input_epub") or config.get("input"),
        "configured_epub": config.get("input_epub") or config.get("epub"),
        "source": {"path": str(source) if source else None, "sha256": source_hash},
        "epub": {"path": str(epub_path) if epub_path else None, "sha256": _file_hash(epub_path) if epub_path and epub_path.is_file() else None},
        "evidence": evidence,
        "source_note": meta_config.get("source_note"),
        "source_url": meta_config.get("source_url"),
    }
    payload["evidence_sha256"] = digest(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode())
    return payload


def _file_hash(path: Path) -> str | None:
    try:
        h = hashlib.sha256()
        with path.open("rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                h.update(block)
        return h.hexdigest()
    except OSError:
        return None


def _epub_evidence(path: Path) -> list[dict]:
    results = []
    try:
        if path.stat().st_size > 100_000_000:
            return results
        with zipfile.ZipFile(path) as archive:
            infos = archive.infolist()
            names = [info.filename for info in infos]
            if len(names) > 20_000 or any(name.startswith(("/", "\\")) or ".." in Path(name.replace("\\", "/")).parts or re.match(r"^[A-Za-z]:", name) for name in names):
                return results
            sizes = {info.filename: info.file_size for info in infos}
            if sizes.get("META-INF/container.xml", 2_000_000) > 32_000:
                return results
            container = ElementTree.fromstring(archive.read("META-INF/container.xml"))
            rootfile = next((node.attrib.get("full-path") for node in container.iter() if node.tag.endswith("rootfile")), None)
            if not rootfile or rootfile not in names:
                return results
            if sizes.get(rootfile, 2_000_000) > 256_000:
                return results
            opf = ElementTree.fromstring(archive.read(rootfile))
            ns = {"dc": "http://purl.org/dc/elements/1.1/", "opf": "http://www.idpf.org/2007/opf"}
            metadata = opf.find("{http://www.idpf.org/2007/opf}metadata")
            if metadata is not None:
                vals = []
                for child in list(metadata):
                    tag = child.tag.rsplit("}", 1)[-1]
                    if tag in {"title", "creator", "publisher", "date", "language", "description", "identifier", "subject"}:
                        value = (child.text or "").strip()
                        if value:
                            vals.append(f"{tag}: {value[:1000]}")
                if vals:
                    text = "\n".join(vals)
                    results.append({"kind": "epub_opf", "location": f"{path.name}!/{rootfile}", "sha256": digest(text.encode()), "text": text})
            manifest_items = [n for n in opf.iter() if n.tag.endswith("item")]
            manifest = {n.attrib.get("id"): n.attrib.get("href") for n in manifest_items}
            spine_ids = [n.attrib.get("idref") for n in opf.iter() if n.tag.endswith("itemref")][:500]
            opf_parent = Path(rootfile).parent
            pieces = []
            for idref in spine_ids[:4]:
                href = manifest.get(idref)
                member = (opf_parent / href).as_posix() if href else ""
                if href and member in names and not member.startswith("../") and sizes.get(member, 2_000_000) <= 200_000:
                    raw = archive.read(member)
                    if len(raw) <= 200_000:
                        text = re.sub(r"<[^>]+>", " ", raw.decode("utf-8", "replace"))
                        text = re.sub(r"\s+", " ", text)[:3000]
                        pieces.append((member, text))
            for member, text in pieces:
                if text:
                    results.append({"kind": "epub_spine_excerpt", "location": f"{path.name}!/{member}", "sha256": digest(text.encode()), "text": text})
            for item in manifest_items:
                properties = set(str(item.attrib.get("properties", "")).split())
                media_type = item.attrib.get("media-type", "")
                href = item.attrib.get("href")
                member = (opf_parent / href).as_posix() if href else ""
                if ("nav" in properties or media_type.endswith("ncx+xml")) and member in names and sizes.get(member, 2_000_000) <= 200_000:
                    raw = archive.read(member)
                    text = re.sub(r"<[^>]+>", " ", raw.decode("utf-8", "replace"))
                    text = re.sub(r"\s+", " ", text)[:5000]
                    if text:
                        results.append({"kind": "epub_navigation_excerpt", "location": f"{path.name}!/{member}", "sha256": digest(text.encode()), "text": text})
    except (OSError, ValueError, KeyError, zipfile.BadZipFile, ElementTree.ParseError, DefusedXmlException):
        return results
    return results
