"""Resolve arXiv identifiers and local TeX sources into an immutable source tree."""

from __future__ import annotations

import gzip
import hashlib
import json
import re
import shutil
import ssl
import tarfile
import tempfile
import urllib.parse
import urllib.request
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from pdf2epub.workflow_contracts import atomic_write_json, sha256_file

_ARXIV_ID_RE = re.compile(
    r"^(?:arxiv:)?(?P<id>(?:\d{4}\.\d{4,5}|[a-z-]+(?:\.[A-Z]{2})?/\d{7})(?:v\d+)?)$",
    re.IGNORECASE,
)
_ARXIV_URL_RE = re.compile(r"arxiv\.org/(?:abs|pdf|e-print)/(?P<id>[^?#]+)", re.IGNORECASE)
_ARXIV_HOSTS = frozenset({"arxiv.org", "www.arxiv.org", "export.arxiv.org"})
_SOURCE_MARKER_SCHEMA = 1


def _validated_arxiv_url(value: str) -> str:
    """Return an arXiv URL only when its scheme, host, and path are trusted."""
    parsed = urllib.parse.urlsplit(value)
    host = (parsed.hostname or "").lower().rstrip(".")
    if parsed.scheme.lower() != "https" or host not in _ARXIV_HOSTS:
        raise ValueError("Only HTTPS URLs hosted by arxiv.org are allowed")
    match = re.fullmatch(
        r"/(?:abs|pdf|e-print)/(?P<id>[^/?#]+?)(?:\.pdf)?",
        parsed.path,
        flags=re.IGNORECASE,
    )
    if not match:
        raise ValueError("Not a supported arXiv URL")
    return urllib.parse.unquote(match.group("id"))


class _ArxivRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Keep redirects on the same small arXiv host allowlist."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        _validated_arxiv_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


@dataclass(frozen=True)
class ResolvedSource:
    source_dir: Path
    source_id: str
    suggested_main_tex: str | None = None


class ArxivSourceResolver:
    """Materialize local directories, archives, TeX files, or arXiv e-prints."""

    def __init__(self, *, max_download_bytes: int = 512 * 1024 * 1024):
        self.max_download_bytes = max_download_bytes

    def materialize(self, source: str | Path, destination: Path) -> ResolvedSource:
        destination = destination.resolve()
        source_path = Path(source).expanduser()
        is_local = source_path.exists()
        if is_local:
            resolved_source = source_path.resolve()
            if resolved_source.is_dir():
                source_digest = _tree_sha256(resolved_source)
                source_key = {
                    "kind": "local_directory",
                    "path": str(resolved_source),
                    "sha256": source_digest,
                }
            elif resolved_source.suffix.lower() == ".tex":
                source_key = {
                    "kind": "local_tex",
                    "path": str(resolved_source),
                    "relative_tex": resolved_source.relative_to(resolved_source.parent).as_posix(),
                    "sha256": _tree_sha256(resolved_source.parent),
                }
            else:
                source_key = {
                    "kind": "local_archive",
                    "path": str(resolved_source),
                    "sha256": sha256_file(resolved_source),
                }
        else:
            source_key = {"kind": "arxiv", "id": normalize_arxiv_id(str(source))}
        marker = destination.with_name(f".{destination.name}.source.json")
        if destination.exists():
            if not destination.is_dir():
                raise NotADirectoryError(
                    f"TeX source destination is not a directory: {destination}"
                )
            if any(destination.iterdir()):
                record = _read_source_marker(marker)
                if record is not None:
                    if record.get("source") != source_key:
                        raise ValueError(
                            "Existing TeX source snapshot belongs to a different input; "
                            f"preserving it at {destination}"
                        )
                    actual_digest = _tree_sha256(destination)
                    if record.get("tree_sha256") != actual_digest:
                        raise ValueError(
                            f"Existing TeX source snapshot failed integrity check: {destination}"
                        )
                    return ResolvedSource(
                        destination,
                        self.source_id(source),
                        record.get("suggested_main_tex"),
                    )
                # Legacy snapshots have no marker. Accept only when their bytes
                # exactly match a locally available source tree/archive.
                if is_local:
                    with _temporary_source_tree(destination) as staging:
                        legacy_result = self._materialize_new(source_path, staging)
                        expected_digest = _tree_sha256(staging)
                    if expected_digest == _tree_sha256(destination):
                        _write_source_marker(
                            marker,
                            source_key,
                            expected_digest,
                            legacy_result.suggested_main_tex,
                        )
                        return ResolvedSource(
                            destination,
                            self.source_id(source),
                            legacy_result.suggested_main_tex,
                        )
                raise ValueError(
                    "Existing TeX source snapshot has no verifiable source identity; "
                    f"preserving it at {destination}"
                )
            destination.rmdir()

        destination.parent.mkdir(parents=True, exist_ok=True)
        with _temporary_source_tree(destination) as staging:
            result = self._materialize_new(source_path, staging) if is_local else None
            if not is_local:
                arxiv_id = source_key["id"]
                self._download_arxiv(arxiv_id, staging)
                result = ResolvedSource(staging, arxiv_id)
            assert result is not None
            digest = _tree_sha256(staging)
            # Promote first; if marker writing fails, the unmarked destination
            # will fail closed on resume rather than being mistaken as complete.
            staging.replace(destination)
            _write_source_marker(marker, source_key, digest, result.suggested_main_tex)
            return ResolvedSource(destination, result.source_id, result.suggested_main_tex)

    def _materialize_new(self, local: Path, destination: Path) -> ResolvedSource:
        if local.is_dir():
            _reject_nested_destination(local.resolve(), destination)
            _copy_tree(local.resolve(), destination)
            return ResolvedSource(destination, local.name)
        if local.suffix.lower() == ".tex":
            _reject_nested_destination(local.resolve().parent, destination)
            _copy_tree(local.resolve().parent, destination)
            return ResolvedSource(
                destination,
                local.stem,
                local.resolve().relative_to(local.resolve().parent).as_posix(),
            )
        self._extract_archive(local.resolve(), destination)
        return ResolvedSource(destination, _archive_stem(local))

    def source_id(self, source: str | Path) -> str:
        local = Path(source).expanduser()
        if local.exists():
            return local.stem if local.is_file() else local.name
        return normalize_arxiv_id(str(source))

    def _download_arxiv(self, arxiv_id: str, destination: Path) -> None:
        safe_id = urllib.parse.quote(arxiv_id, safe="/")
        url = f"https://export.arxiv.org/e-print/{safe_id}"
        request = urllib.request.Request(
            url,
            headers={"User-Agent": "pdf2epub/0.2 (arXiv TeX translation)"},
        )
        archive_path = destination.parent / f".{slugify_source_id(arxiv_id)}.download"
        try:
            tls_context = _tls_context()
            opener = urllib.request.build_opener(
                _ArxivRedirectHandler(),
                urllib.request.HTTPSHandler(context=tls_context),
            )
            with opener.open(request, timeout=120) as response:
                total = 0
                with archive_path.open("wb") as output:
                    while chunk := response.read(1024 * 1024):
                        total += len(chunk)
                        if total > self.max_download_bytes:
                            raise ValueError(
                                f"arXiv source exceeds {self.max_download_bytes} bytes"
                            )
                        output.write(chunk)
            self._extract_archive(archive_path, destination)
        finally:
            archive_path.unlink(missing_ok=True)

    def _extract_archive(self, archive: Path, destination: Path) -> None:
        if tarfile.is_tarfile(archive):
            with tarfile.open(archive, "r:*") as tar:
                _safe_extract_tar(
                    tar,
                    destination,
                    max_bytes=self.max_download_bytes,
                )
            _flatten_single_directory(destination)
            return

        with archive.open("rb") as source:
            gzip_header = source.read(2)
        if gzip_header == b"\x1f\x8b":
            with gzip.open(archive, "rb") as source:
                decompressed = source.read(self.max_download_bytes + 1)
        else:
            if archive.stat().st_size > self.max_download_bytes:
                raise ValueError(
                    f"Source file exceeds {self.max_download_bytes} bytes"
                )
            decompressed = archive.read_bytes()
        if len(decompressed) > self.max_download_bytes:
            raise ValueError(
                f"Expanded source exceeds {self.max_download_bytes} bytes"
            )
        if not decompressed.strip():
            raise ValueError(f"Source archive is empty: {archive}")
        (destination / "main.tex").write_bytes(decompressed)


def normalize_arxiv_id(value: str) -> str:
    """Normalize ``arXiv:...`` and arxiv.org URLs to a source identifier."""
    cleaned = value.strip()
    parsed = urllib.parse.urlsplit(cleaned)
    if parsed.scheme or parsed.netloc:
        cleaned = _validated_arxiv_url(cleaned)
    else:
        url_match = _ARXIV_URL_RE.search(cleaned)
        if url_match:
            cleaned = urllib.parse.unquote(url_match.group("id"))
            if cleaned.lower().endswith(".pdf"):
                cleaned = cleaned[:-4]
    match = _ARXIV_ID_RE.fullmatch(cleaned)
    if not match:
        raise ValueError(f"Not a local source or valid arXiv identifier/URL: {value}")
    return match.group("id")


def slugify_source_id(source_id: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9._-]+", "_", source_id).strip("._")
    return slug or "arxiv-source"


def _safe_extract_tar(
    tar: tarfile.TarFile,
    destination: Path,
    *,
    max_bytes: int,
) -> None:
    destination = destination.resolve()
    members: list[tarfile.TarInfo] = []
    expanded_bytes = 0
    for member in tar.getmembers():
        member_path = PurePosixPath(member.name)
        if member_path.is_absolute() or ".." in member_path.parts:
            raise ValueError(f"Unsafe path in source archive: {member.name}")
        if member.issym() or member.islnk() or not (member.isdir() or member.isfile()):
            raise ValueError(f"Unsupported archive member: {member.name}")
        target = (destination / Path(*member_path.parts)).resolve()
        try:
            target.relative_to(destination)
        except ValueError as exc:
            raise ValueError(f"Unsafe path in source archive: {member.name}") from exc
        expanded_bytes += member.size
        if expanded_bytes > max_bytes:
            raise ValueError(f"Expanded source exceeds {max_bytes} bytes")
        members.append(member)
    tar.extractall(destination, members=members, filter="data")


def _flatten_single_directory(destination: Path) -> None:
    entries = list(destination.iterdir())
    if len(entries) != 1 or not entries[0].is_dir():
        return
    nested = entries[0]
    for child in list(nested.iterdir()):
        child.replace(destination / child.name)
    nested.rmdir()


def _copy_tree(source: Path, destination: Path) -> None:
    for child in source.iterdir():
        target = destination / child.name
        if child.is_symlink():
            raise ValueError(f"Symlinks are not supported in TeX source trees: {child}")
        if child.is_dir():
            shutil.copytree(child, target)
        else:
            shutil.copy2(child, target)


def _reject_nested_destination(source: Path, destination: Path) -> None:
    try:
        destination.relative_to(source)
    except ValueError:
        return
    raise ValueError(
        "The TeX run directory must not be nested inside the local source tree"
    )


def _archive_stem(path: Path) -> str:
    name = path.name
    for suffix in (".tar.gz", ".tar.bz2", ".tar.xz", ".tgz", ".gz", ".tar"):
        if name.lower().endswith(suffix):
            return name[: -len(suffix)]
    return path.stem


@contextmanager
def _temporary_source_tree(destination: Path):
    """Create a private sibling directory that can be atomically promoted."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    path = Path(tempfile.mkdtemp(prefix=f".{destination.name}.staging-", dir=destination.parent))
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


def _tree_sha256(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*"), key=lambda item: item.relative_to(root).as_posix()):
        relative = path.relative_to(root).as_posix()
        if path.is_symlink():
            raise ValueError(f"Symlinks are not supported in TeX source trees: {path}")
        if path.is_dir():
            digest.update(b"D\0" + relative.encode("utf-8") + b"\0")
            continue
        size = path.stat().st_size
        digest.update(b"F\0" + relative.encode("utf-8") + b"\0" + str(size).encode() + b"\0")
        with path.open("rb") as source:
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(chunk)
        digest.update(b"\0")
    return digest.hexdigest()


def _read_source_marker(marker: Path) -> dict | None:
    try:
        payload = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict) or payload.get("schema") != _SOURCE_MARKER_SCHEMA:
        return None
    if not isinstance(payload.get("source"), dict) or not isinstance(
        payload.get("tree_sha256"), str
    ):
        return None
    return payload


def _write_source_marker(
    marker: Path,
    source: dict,
    tree_sha256: str,
    suggested_main_tex: str | None,
) -> None:
    payload = {
        "schema": _SOURCE_MARKER_SCHEMA,
        "source": source,
        "tree_sha256": tree_sha256,
        "suggested_main_tex": suggested_main_tex,
    }
    atomic_write_json(marker, payload, sort_keys=True)


def _tls_context() -> ssl.SSLContext:
    """Use the package CA bundle on Python installs without system roots."""
    try:
        import certifi
    except ImportError:
        return ssl.create_default_context()
    return ssl.create_default_context(cafile=certifi.where())
