"""Apply verified enrichment fields to an EPUB OPF without replacing its identity."""
from __future__ import annotations

from pathlib import Path
import html
import hashlib
import os
import tempfile
import posixpath
from typing import Any, Mapping
from lxml import etree as LET

from .html_translation.package_paths import has_local_tag, safe_lxml_parser

DC = "http://purl.org/dc/elements/1.1/"
OPF = "http://www.idpf.org/2007/opf"


def enrichment_outcome(plan: Mapping[str, Any]) -> dict[str, Any]:
    """Describe a successful application without certifying skipped work."""
    status = plan.get("status")
    fields = list(plan.get("verified_fields") or [])
    if status in {"disabled", "not_applicable"}:
        return {"status": status, "requested_fields": []}
    if status == "verified":
        return {"status": "applied", "requested_fields": fields}
    if fields:
        return {"status": "partial", "requested_fields": fields,
                "reason": "Verified fields used; incomplete fields retain existing values"}
    return {"status": "skipped", "requested_fields": [],
            "reason": "Metadata enrichment incomplete; existing metadata preserved"}


def apply_package_metadata(opf_path: Path, plan: Mapping[str, Any], *, cover_policy: str = "preserve") -> None:
    """Apply as one staging transaction, removing any newly-created members on error."""
    opf_path = Path(opf_path)
    before = {path.resolve() for path in opf_path.parent.rglob("*") if path.is_file()}
    nav_backups: dict[Path, bytes] = {}
    existing_member_backups: dict[Path, bytes] = {}
    try:
        root = LET.parse(str(opf_path), parser=safe_lxml_parser()).getroot()
        manifest = next((el for el in root.iter() if has_local_tag(el, "manifest")), None)
        if manifest is not None:
            for item in manifest:
                if has_local_tag(item, "item") and "nav" in item.get("properties", "").split():
                    nav_path = (opf_path.parent / item.get("href", "")).resolve()
                    if nav_path.is_relative_to(opf_path.parent.resolve()) and nav_path.is_file():
                        nav_backups[nav_path] = nav_path.read_bytes()
            cover_path = Path(str((plan.get("metadata") or {}).get("cover_path") or ""))
            if cover_path.is_file():
                source_hash = hashlib.sha256(cover_path.read_bytes()).hexdigest()[:12]
                page_item = _generated_cover_item(manifest, f"metadata-cover-page-{source_hash}", opf_path, source_hash)
                if page_item is not None:
                    page_path = (opf_path.parent / page_item.get("href", "")).resolve()
                    if page_path.is_relative_to(opf_path.parent.resolve()) and page_path.is_file():
                        existing_member_backups[page_path] = page_path.read_bytes()
    except (OSError, LET.XMLSyntaxError):
        pass
    try:
        _apply_package_metadata_impl(opf_path, plan, cover_policy=cover_policy)
    except Exception:
        for path, content in nav_backups.items():
            _atomic_write_bytes(path, content)
        for path, content in existing_member_backups.items():
            _atomic_write_bytes(path, content)
        for path in opf_path.parent.rglob("*"):
            if path.is_file() and path.resolve() not in before:
                path.unlink(missing_ok=True)
        raise


def _atomic_write_text(path: Path, content: str) -> None:
    fd, name = tempfile.mkstemp(prefix=".metadata-member-", suffix=".tmp", dir=path.parent)
    os.close(fd)
    try:
        Path(name).write_text(content, encoding="utf-8")
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def _atomic_write_bytes(path: Path, content: bytes) -> None:
    fd, name = tempfile.mkstemp(prefix=".metadata-member-", suffix=".tmp", dir=path.parent)
    os.close(fd)
    try:
        Path(name).write_bytes(content)
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def _unused_filename(directory: Path, filename: str) -> str:
    base = Path(filename)
    candidate = base.name
    suffix = 2
    while (directory / candidate).exists():
        candidate = f"{base.stem}-{suffix}{base.suffix}"
        suffix += 1
    return candidate


def _unique_id(base: str, used_ids: set, manifest, expected_image_name: str | None) -> str:
    existing = next((el for el in manifest if has_local_tag(el, "item") and el.get("id") == base), None)
    if base not in used_ids or (expected_image_name and existing is not None and existing.get("href", "").endswith(expected_image_name)):
        return base
    suffix = 2
    while f"{base}-{suffix}" in used_ids:
        candidate = next((el for el in manifest if has_local_tag(el, "item") and el.get("id") == f"{base}-{suffix}"), None)
        if expected_image_name and candidate is not None and candidate.get("href", "").endswith(expected_image_name):
            return f"{base}-{suffix}"
        suffix += 1
    return f"{base}-{suffix}"


def _generated_cover_item(manifest, page_id: str, opf_path: Path, source_hash: str):
    for item in manifest:
        if not has_local_tag(item, "item") or not item.get("id", "").startswith(page_id):
            continue
        page = (opf_path.parent / item.get("href", "")).resolve()
        if not page.is_relative_to(opf_path.parent.resolve()) or not page.is_file():
            continue
        try:
            parsed = LET.parse(str(page), parser=safe_lxml_parser())
            if any(has_local_tag(el, "meta") and el.get("name") == "pdf2epub-generated-cover" and
                   el.get("content") == source_hash for el in parsed.iter()):
                return item
        except LET.XMLSyntaxError:
            continue
    return None


def _explicit_cover_item(root, manifest, opf_path: Path):
    """Find a cover page only through EPUB guide or a verified image-only cover page."""
    href = None
    for element in root.iter():
        if has_local_tag(element, "reference") and element.get("type", "").casefold() == "cover":
            href = element.get("href", "").split("#", 1)[0]
            break
    if href is None:
        nav_item = next((el for el in manifest if has_local_tag(el, "item") and
                         "nav" in el.get("properties", "").split()), None)
        if nav_item is not None:
            nav_path = (opf_path.parent / nav_item.get("href", "")).resolve()
            if nav_path.is_relative_to(opf_path.parent.resolve()) and nav_path.is_file():
                try:
                    nav_tree = LET.parse(str(nav_path), parser=safe_lxml_parser())
                    for link in nav_tree.iter():
                        if not has_local_tag(link, "a"):
                            continue
                        link_type = next((value for key, value in link.attrib.items() if key.endswith("}type")), "")
                        if "cover" in link_type.split() and link.get("href"):
                            href = posixpath.normpath(posixpath.join(
                                posixpath.dirname(nav_item.get("href", "")),
                                link.get("href", "").split("#", 1)[0],
                            ))
                            break
                except LET.XMLSyntaxError:
                    pass
    if href is None:
        cover_image_id = None
        for element in root.iter():
            if has_local_tag(element, "meta") and element.get("name") == "cover":
                cover_image_id = element.get("content")
                break
        image_item = next((el for el in manifest if has_local_tag(el, "item") and
                           (el.get("id") == cover_image_id or "cover-image" in el.get("properties", "").split())), None)
        if image_item is not None:
            image_href = image_item.get("href", "")
            for page_item in manifest:
                if not has_local_tag(page_item, "item") or page_item.get("media-type") != "application/xhtml+xml":
                    continue
                page_path = (opf_path.parent / page_item.get("href", "")).resolve()
                if not page_path.is_relative_to(opf_path.parent.resolve()) or not page_path.is_file():
                    continue
                try:
                    page = LET.parse(str(page_path), parser=safe_lxml_parser())
                except LET.XMLSyntaxError:
                    continue
                body = next((el for el in page.iter() if has_local_tag(el, "body")), None)
                if body is None:
                    continue
                elements = [el for el in body.iter() if isinstance(el.tag, str) and not has_local_tag(el, "body")]
                images = [el for el in elements if has_local_tag(el, "img")]
                other = [el for el in elements if not has_local_tag(el, "img") and ((el.text or "").strip() or list(el))]
                if len(images) == 1 and not other and images[0].get("src", "").endswith(Path(image_href).name):
                    return page_item
    if not href:
        return None
    normalized = (opf_path.parent / href).resolve()
    if not normalized.is_relative_to(opf_path.parent.resolve()):
        return None
    relative = normalized.relative_to(opf_path.parent.resolve()).as_posix()
    return next((el for el in manifest if has_local_tag(el, "item") and
                 Path(el.get("href", "").split("#", 1)[0]).as_posix() == relative), None)


def _update_cover_navigation(root, manifest, opf_path: Path, page_href: str, old_href: str | None) -> None:
    nav_item = next((el for el in manifest if has_local_tag(el, "item") and
                     "nav" in el.get("properties", "").split()), None)
    if nav_item is not None:
        nav_path = (opf_path.parent / nav_item.get("href", "")).resolve()
        if nav_path.is_relative_to(opf_path.parent.resolve()) and nav_path.is_file():
            tree = LET.parse(str(nav_path), parser=safe_lxml_parser())
            changed = False
            for link in tree.iter():
                if not has_local_tag(link, "a"):
                    continue
                link_type = next((value for key, value in link.attrib.items() if key.endswith("}type")), "")
                if "cover" not in link_type.split():
                    continue
                old_link = posixpath.normpath(posixpath.join(
                    posixpath.dirname(nav_item.get("href", "")),
                    link.get("href", "").split("#", 1)[0],
                ))
                if old_href is None or old_link == old_href:
                    link.set("href", posixpath.relpath(page_href, start=posixpath.dirname(nav_item.get("href", "")) or "."))
                    changed = True
            if changed:
                fd, name = tempfile.mkstemp(prefix=".metadata-nav-", suffix=".tmp", dir=nav_path.parent)
                os.close(fd)
                try:
                    tree.write(name, encoding="UTF-8", xml_declaration=True, doctype=None)
                    os.replace(name, nav_path)
                finally:
                    Path(name).unlink(missing_ok=True)
    elif old_href:
        for element in root.iter():
            if has_local_tag(element, "reference") and element.get("type", "").casefold() == "cover":
                element.set("href", page_href)


def _apply_package_metadata_impl(opf_path: Path, plan: Mapping[str, Any], *, cover_policy: str = "preserve") -> None:
    """Fill absent descriptive fields; preserve package ID, date and source metadata."""
    metadata = plan.get("metadata")
    verified = set(plan.get("verified_fields") or [])
    if not isinstance(metadata, Mapping) or not verified:
        return
    tree = LET.parse(str(opf_path), parser=safe_lxml_parser())
    root = tree.getroot()
    md = next((element for element in root.iter() if has_local_tag(element, "metadata")), None)
    if md is None:
        raise ValueError("EPUB package has no metadata element")
    ns = {"dc": DC}
    is_epub3 = str(root.get("version", "2.0")).startswith("3")
    if is_epub3:
        prefix = root.get("prefix", "")
        for declaration in ("marc: http://id.loc.gov/vocabulary/relators/", "onix: http://www.editeur.org/ONIX/book/codelists/current.html#"):
            name = declaration.split(":", 1)[0] + ":"
            if name not in prefix:
                prefix = (prefix + " " + declaration).strip()
        root.set("prefix", prefix)

    def values(tag: str):
        return [el for el in md.iter() if has_local_tag(el, tag)]

    def fill(tag: str, value: Any) -> None:
        matches = values(tag)
        if tag not in verified or not value or any((el.text or "").strip() for el in matches):
            return
        el = matches[0] if matches else LET.SubElement(md, f"{{{DC}}}{tag}")
        el.text = str(value)

    fill("title", metadata.get("title"))
    fill("description", metadata.get("description"))
    if "publishing" in verified:
        verified.update({"publisher", "date"})
    fill("publisher", metadata.get("publisher"))
    fill("date", metadata.get("date"))
    if "authors" in verified:
        authors = metadata.get("authors", []) or []
        if isinstance(authors, str):
            authors = [authors]
        creators = values("creator")
        # Treat the builder's placeholder as absent; preserve all real source
        # contributors and add only roles not already represented.
        for old in list(creators):
            if (old.text or "").strip().casefold() in {"unknown", "unknown author", "n/a"}:
                md.remove(old)
        role_attr = f"{{{OPF}}}role"
        role_map = {el.get("refines", "").lstrip("#"): (el.text or "") for el in values("meta") if el.get("property") == "role"}
        existing_people = {
            ((el.text or "").strip().casefold(), el.get(role_attr, role_map.get(el.get("id", ""), "aut")))
            for el in values("creator")
        }
        existing_people.update(
            ((el.text or "").strip().casefold(), role_map.get(el.get("id", "")))
            for el in values("contributor") if role_map.get(el.get("id", ""))
        )
        used_ids = {el.get("id") for el in root.iter() if el.get("id")}
        for index, author in enumerate(authors, 1):
            if isinstance(author, Mapping):
                author_name = author.get("name")
                role = str(author.get("role") or "aut").casefold()
            else:
                author_name, role = author, "aut"
            if not author_name:
                continue
            role = {"author": "aut", "editor": "edt", "translator": "trl"}.get(role, role)
            if (str(author_name).strip().casefold(), role) in existing_people:
                continue
            contributor_tag = "contributor" if is_epub3 and role == "trl" else "creator"
            el = LET.SubElement(md, f"{{{DC}}}{contributor_tag}")
            el.text = str(author_name)
            if is_epub3:
                creator_id = f"enriched-creator-{index}"
                suffix = 2
                while creator_id in used_ids:
                    creator_id = f"enriched-creator-{index}-{suffix}"
                    suffix += 1
                el.set("id", creator_id)
                used_ids.add(creator_id)
                LET.SubElement(md, f"{{{OPF}}}meta", refines=f"#{creator_id}", property="role", scheme="marc:relators").text = role
            else:
                el.set(f"{{{OPF}}}role", role)
            existing_people.add((str(author_name).strip().casefold(), role))
    if "subjects" in verified and not any((el.text or "").strip() for el in values("subject")):
        subjects = metadata.get("subjects", []) or []
        for subject in subjects:
            if subject:
                LET.SubElement(md, f"{{{DC}}}subject").text = str(subject)
    existing_ids = {(el.text or "").strip() for el in values("identifier")}
    ids = metadata.get("identifiers", {}) or {}
    if is_epub3 and ("identifiers" in verified or any(name in verified for name in ("doi", "isbn", "issn"))):
        prefix = root.get("prefix", "")
        if "onix:" not in prefix:
            root.set("prefix", (prefix + " onix: http://www.editeur.org/ONIX/book/codelists/current.html#").strip())
    identifier_codes = {"doi": "06", "isbn": "15", "issn": "08"}
    source_isbn_roles = {"translation_source", "source_print", "corresponding_print", "source_edition"}
    for scheme in ("doi", "isbn", "issn"):
        value = ids.get(scheme)
        if scheme == "isbn" and metadata.get("isbn_role") in source_isbn_roles:
            continue
        values_to_add = value if isinstance(value, list) else [value]
        scheme_exists = any(el.get(f"{{{OPF}}}scheme", "").casefold() == scheme for el in values("identifier"))
        scheme_exists = scheme_exists or any(
            el.get("property") == "identifier-type" and el.text == identifier_codes[scheme]
            and el.get("scheme", "") == "onix:codelist5" for el in values("meta")
        )
        if scheme_exists:
            continue
        if "identifiers" in verified or scheme in verified:
            for candidate in values_to_add:
                candidate = str(candidate).strip() if candidate else ""
                if candidate and candidate not in existing_ids:
                    el = LET.SubElement(md, f"{{{DC}}}identifier")
                    el.text = candidate
                    existing_ids.add(candidate)
                    if is_epub3:
                        ident_id = f"enriched-identifier-{scheme}"
                        used_ids = {item.get("id") for item in root.iter() if item.get("id")}
                        suffix = 2
                        while ident_id in used_ids:
                            ident_id = f"enriched-identifier-{scheme}-{suffix}"
                            suffix += 1
                        el.set("id", ident_id)
                        LET.SubElement(md, f"{{{OPF}}}meta", refines=f"#{ident_id}", property="identifier-type", scheme="onix:codelist5").text = identifier_codes[scheme]
                    else:
                        el.set(f"{{{OPF}}}scheme", scheme.upper())
    source_isbn = metadata.get("source_isbn") or (ids.get("isbn") if metadata.get("isbn_role") in source_isbn_roles else None)
    source_exists = any((el.text or "").strip() == f"urn:isbn:{source_isbn}" for el in values("source")) if source_isbn else False
    if source_isbn and not source_exists and "identifiers" in verified:
        LET.SubElement(md, f"{{{DC}}}source").text = f"urn:isbn:{source_isbn}"
        if not is_epub3:
            LET.SubElement(md, f"{{{OPF}}}meta", name="source-isbn-role", content=str(metadata.get("isbn_role") or "source-edition"))
    source_context = plan.get("source_context") if isinstance(plan.get("source_context"), Mapping) else {}
    relationship_note = (metadata.get("relationship_note") or metadata.get("artifact_relation") or
                         source_context.get("relationship_note") or source_context.get("artifact_relation"))
    if not relationship_note and source_isbn:
        relationship_note = f"ISBN {source_isbn} identifies the source-language edition; this package is a derivative digital edition."
    source_language = metadata.get("source_language") or source_context.get("source_language")
    source_metadata_applied = bool({"publishing", "identifiers"} & verified)
    if relationship_note and (source_metadata_applied or ("authors" in verified and source_context)):
        note = "The listed edition details are associated with the source edition. " + str(relationship_note)
        if source_language:
            note += f" Source language: {source_language}."
        if is_epub3:
            if not any(has_local_tag(el, "meta") and el.get("property") == "dcterms:relation" and (el.text or "") == note for el in values("meta")):
                LET.SubElement(md, f"{{{OPF}}}meta", property="dcterms:relation").text = note
        elif not any(has_local_tag(el, "meta") and el.get("name") == "source-edition-note" for el in values("meta")):
            LET.SubElement(md, f"{{{OPF}}}meta", name="source-edition-note", content=note)
        manifest = next((el for el in root.iter() if has_local_tag(el, "manifest")), None)
        spine = next((el for el in root.iter() if has_local_tag(el, "spine")), None)
        if manifest is not None and spine is not None and not any(
            has_local_tag(el, "item") and el.get("id", "").startswith("source-edition-note") and
            el.get("media-type") == "application/xhtml+xml" for el in manifest
        ):
            note_name = _unused_filename(opf_path.parent / "text", "source-edition-note.xhtml")
            note_id = _unique_id("source-edition-note", {el.get("id") for el in root.iter() if el.get("id")}, manifest, None)
            note_file = opf_path.parent / "text" / note_name
            note_file.parent.mkdir(parents=True, exist_ok=True)
            language = html.escape(str(metadata.get("language") or "en"), quote=True)
            note_text = html.escape(str(relationship_note or note), quote=False)
            _atomic_write_text(note_file,
                '<?xml version="1.0" encoding="UTF-8"?>\n'
                f'<html xmlns="http://www.w3.org/1999/xhtml" xml:lang="{language}" lang="{language}">'
                f'<head><title>Edition information</title><meta name="pdf2epub-source-edition-note" content="1"/></head><body><p>{note_text}</p></body></html>',
            )
            LET.SubElement(manifest, f"{{{OPF}}}item", id=note_id, href=f"text/{note_name}", **{"media-type": "application/xhtml+xml"})
            spine.append(LET.Element(f"{{{OPF}}}itemref", idref=note_id, linear="no"))
    cover_path = Path(str(metadata.get("cover_path") or ""))
    if "cover" in verified and cover_path.is_file() and cover_policy in {"fill_missing", "prefer_verified"}:
        manifest = next((el for el in root.iter() if has_local_tag(el, "manifest")), None)
        spine = next((el for el in root.iter() if has_local_tag(el, "spine")), None)
        if manifest is None or spine is None:
            raise ValueError("EPUB package has no manifest or spine for cover integration")
        existing_cover = any(
            has_local_tag(el, "item") and (el.get("properties", "").split().__contains__("cover-image")
                or el.get("id") == "cover-image")
            for el in manifest
        ) or any(has_local_tag(el, "meta") and el.get("name") == "cover" for el in md)
        if not existing_cover or cover_policy == "prefer_verified":
            source_hash = hashlib.sha256(cover_path.read_bytes()).hexdigest()[:12]
            image_name = f"verified-cover-{source_hash}" + (cover_path.suffix.lower() if cover_path.suffix else ".jpg")
            image_file = opf_path.parent / "images" / image_name
            image_file.parent.mkdir(parents=True, exist_ok=True)
            if not image_file.exists():
                image_file.write_bytes(cover_path.read_bytes())
            media = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".webp": "image/webp"}.get(image_file.suffix, "image/jpeg")
            image_id, page_id = f"metadata-cover-image-{source_hash}", f"metadata-cover-page-{source_hash}"
            used_ids = {el.get("id") for el in root.iter() if el.get("id")}
            image_id = _unique_id(image_id, used_ids, manifest, image_name)
            existing_page = _generated_cover_item(manifest, page_id, opf_path, source_hash)
            if existing_page is not None:
                page_id = existing_page.get("id")
            else:
                page_id = _unique_id(page_id, used_ids, manifest, None)
            explicit_cover = _explicit_cover_item(root, manifest, opf_path)
            old_cover_href = explicit_cover.get("href") if explicit_cover is not None else None
            image_attrs = {"id": image_id, "href": f"images/{image_name}", "media-type": media}
            if str(root.get("version", "2.0")).startswith("3"):
                for old in manifest:
                    if has_local_tag(old, "item") and "cover-image" in old.get("properties", "").split():
                        old.set("properties", " ".join(x for x in old.get("properties", "").split() if x != "cover-image"))
                image_attrs["properties"] = "cover-image"
            LET.SubElement(manifest, f"{{{OPF}}}item", **image_attrs)
            page_href = existing_page.get("href") if existing_page is not None else f"text/{page_id}.xhtml"
            page = opf_path.parent / page_href
            if not page.resolve().is_relative_to(opf_path.parent.resolve()):
                raise ValueError("metadata cover page path escapes the EPUB package")
            if existing_page is None and page.exists():
                raise ValueError("metadata cover page path collides with an existing package resource")
            page.parent.mkdir(parents=True, exist_ok=True)
            image_href = posixpath.relpath(f"images/{image_name}", start=posixpath.dirname(page_href) or ".")
            _atomic_write_text(page,
                '<?xml version="1.0" encoding="UTF-8"?>\n'
                '<html xmlns="http://www.w3.org/1999/xhtml"><head><title>Cover</title>'
                '<meta name="viewport" content="width=device-width,height=device-height"/>'
                '<style type="text/css">'
                'html,body{margin:0;padding:0;width:100%;height:100%;}'
                'body{display:flex;align-items:center;justify-content:center;overflow:hidden;'
                'break-inside:avoid;page-break-inside:avoid;}'
                'img{display:block;width:auto;height:auto;max-width:100%;max-height:100%;'
                'object-fit:contain;break-inside:avoid;page-break-inside:avoid;}'
                '</style>'
                f'<meta name="pdf2epub-generated-cover" content="{source_hash}"/></head>'
                f'<body><img src="{html.escape(image_href, quote=True)}" alt="Book cover"/></body></html>')
            if existing_page is None:
                LET.SubElement(manifest, f"{{{OPF}}}item", id=page_id, href=page_href, **{"media-type": "application/xhtml+xml"})
            if not str(root.get("version", "2.0")).startswith("3"):
                for old in list(md):
                    if has_local_tag(old, "meta") and old.get("name") == "cover":
                        md.remove(old)
                LET.SubElement(md, f"{{{OPF}}}meta", name="cover", content=image_id)
            _update_cover_navigation(root, manifest, opf_path, page_href, old_cover_href)
            for ref in list(spine):
                if has_local_tag(ref, "itemref") and ref.get("idref") == page_id:
                    spine.remove(ref)
                elif explicit_cover is not None and has_local_tag(ref, "itemref") and ref.get("idref") == explicit_cover.get("id"):
                    spine.remove(ref)
            spine.insert(0, LET.Element(f"{{{OPF}}}itemref", idref=page_id, linear="no"))
    fd, temp_name = tempfile.mkstemp(prefix=".metadata-opf-", suffix=".tmp", dir=opf_path.parent)
    os.close(fd)
    try:
        tree.write(temp_name, encoding="UTF-8", xml_declaration=True, doctype=None)
        os.replace(temp_name, opf_path)
    finally:
        Path(temp_name).unlink(missing_ok=True)
