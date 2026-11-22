"""Bounded public-catalog lookup and safe cover candidate download."""
from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import tempfile
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

MAX_JSON_BYTES = 1_000_000
MAX_COVER_BYTES = 10 * 1024 * 1024
MAX_REDIRECTS = 3
USER_AGENT = "pdf2epub-metadata/1.0"


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _bounded_int(value: int, low: int, high: int) -> int:
    return max(low, min(int(value), high))


def _public_url(url: str) -> str:
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("URL must use http or https and include a host")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("URL credentials are not allowed")
    host = parsed.hostname.rstrip(".").lower()
    if host == "localhost" or host.endswith(".localhost") or host.endswith(".local"):
        raise ValueError("local hostnames are not allowed")
    try:
        addresses = [ipaddress.ip_address(host)]
    except ValueError:
        try:
            addresses = [ipaddress.ip_address(item[4][0].split("%", 1)[0]) for item in socket.getaddrinfo(host, parsed.port or (443 if parsed.scheme == "https" else 80), type=socket.SOCK_STREAM)]
        except OSError as exc:
            raise ValueError(f"host lookup failed: {type(exc).__name__}") from exc
    if not addresses or any(not addr.is_global for addr in addresses):
        raise ValueError("URL resolves to a non-public address")
    return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, parsed.path or "/", parsed.query, ""))


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: N802
        return None


_OPENER = urllib.request.build_opener(_NoRedirect())


def _request(url: str, *, timeout_seconds: int, limit: int) -> tuple[bytes, str]:
    current = url
    for _ in range(MAX_REDIRECTS + 1):
        current = _public_url(current)
        req = urllib.request.Request(current, headers={"User-Agent": USER_AGENT, "Accept": "application/json,image/*;q=0.8,*/*;q=0.1"})
        try:
            response = _OPENER.open(req, timeout=timeout_seconds)
        except urllib.error.HTTPError as exc:
            if exc.code in {301, 302, 303, 307, 308}:
                location = exc.headers.get("Location")
                if not location:
                    raise ValueError(f"redirect {exc.code} missing Location")
                current = urllib.parse.urljoin(current, location)
                continue
            raise
        with response:
            data = response.read(limit + 1)
            if len(data) > limit:
                raise ValueError("response exceeds size limit")
            return data, current
    raise ValueError("too many redirects")


def _candidate(source: str, item: dict[str, Any]) -> dict[str, Any]:
    if source == "google_books":
        info = item.get("volumeInfo") or {}
        if not isinstance(info, dict):
            raise ValueError("Google Books volumeInfo must be an object")
        identifiers = info.get("industryIdentifiers", [])
        link = item.get("selfLink")
        result = {
            "title": info.get("title"), "authors": info.get("authors", []),
            "publisher": info.get("publisher"), "published_date": info.get("publishedDate"),
            "description": info.get("description"), "subjects": info.get("categories", []),
            "identifiers": identifiers, "cover_url": (info.get("imageLinks") or {}).get("thumbnail"),
            "source_url": link, "edition": info.get("edition"),
        }
    else:
        key = item.get("key")
        result = {
            "title": item.get("title"), "authors": item.get("author_name", []),
            "publisher": (item.get("publisher") or [])[:5], "published_date": item.get("first_publish_year"),
            "description": None, "subjects": (item.get("subject") or [])[:20],
            "identifiers": {"isbn": (item.get("isbn") or [])[:12], "issn": (item.get("issn") or [])[:12], "doi": (item.get("doi") or [])[:12]},
            "cover_url": f"https://covers.openlibrary.org/b/id/{item['cover_i']}-L.jpg" if item.get("cover_i") else None,
            "source_url": "https://openlibrary.org" + key if isinstance(key, str) and key.startswith("/") else None,
            "edition": item.get("edition_key", [None])[0] if item.get("edition_key") else None,
        }
    result["source"] = source
    result["matched"] = False
    result["warnings"] = ["catalog result is a candidate only; edition and artifact identity require human review"]
    # Stable identity for later evidence references, excluding mutable fetch metadata.
    result["candidate_sha256"] = _sha(json.dumps(result, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode())
    return result


def fetch_candidates(query: str, *, timeout_seconds: int, max_candidates: int, retry: bool = False, cache_path: str | Path | None = None) -> dict:
    """Query Google Books and Open Library with bounded response and hash-bound cache."""
    timeout = _bounded_int(timeout_seconds, 1, 20)
    maximum = _bounded_int(max_candidates, 1, 12)
    q = " ".join(str(query).split())[:500]
    query_sha = _sha(json.dumps({"q": q, "max_candidates": maximum}, sort_keys=True).encode())
    cache = Path(cache_path) if cache_path is not None else None
    cached: dict = {}
    if cache and cache.is_file():
        try:
            cached = json.loads(cache.read_text(encoding="utf-8"))
            if not isinstance(cached, dict):
                cached = {}
        except (OSError, ValueError):
            cached = {}
    if cached.get("query_sha256") == query_sha and cached.get("state") == "candidates" and (not retry or not cached.get("failures")):
        return cached
    if cached.get("query_sha256") == query_sha and cached.get("state") in {"request_failed", "not_found"} and not retry:
        return cached

    endpoints = [
        ("google_books", "https://www.googleapis.com/books/v1/volumes?q=" + urllib.parse.quote(q)),
        ("open_library", "https://openlibrary.org/search.json?q=" + urllib.parse.quote(q)),
    ]
    sources, failures, all_candidates = [], [], []
    attempts = 0
    for name, url in endpoints:
        old_source = next((s for s in cached.get("sources", []) if s.get("source") == name), {}) if cached.get("query_sha256") == query_sha else {}
        if old_source.get("state") == "ok":
            sources.append(old_source)
            all_candidates.extend(old_source.get("candidates", []))
            continue
        record: dict[str, Any] = {"source": name, "state": "request_failed", "candidates": []}
        for attempt in range(2):
            attempts += 1
            try:
                raw, final_url = _request(url, timeout_seconds=timeout, limit=MAX_JSON_BYTES)
                payload = json.loads(raw)
                if not isinstance(payload, dict):
                    raise ValueError("catalog response must be a JSON object")
                items = payload.get("items", []) if name == "google_books" else payload.get("docs", [])
                if not isinstance(items, list):
                    raise ValueError("catalog response has no result list")
                candidates = [_candidate(name, item) for item in items[:maximum] if isinstance(item, dict)]
                record = {"source": name, "state": "ok", "request_url": url, "response_url": final_url, "response_sha256": _sha(raw), "candidates": candidates}
                all_candidates.extend(candidates)
                break
            except urllib.error.HTTPError as exc:
                record["error"] = f"HTTP {exc.code}"
                if exc.code == 404:
                    record["state"] = "not_found"
                    break
                # 404/429 and other client errors are not retried.
                if 500 <= exc.code < 600 and attempt == 0:
                    time.sleep(0.15)
                    continue
                break
            except (OSError, TimeoutError, json.JSONDecodeError, ValueError) as exc:
                record["error"] = f"{type(exc).__name__}: {str(exc)[:300]}"
                if attempt == 0:
                    time.sleep(0.15)
                    continue
                break
        if record["state"] == "not_found":
            record["error"] = "HTTP 404"
        elif record["state"] != "ok":
            failures.append({"source": name, "error": record.get("error", "request failed")})
        sources.append(record)

    state = "candidates" if all_candidates else "request_failed" if failures else "not_found"
    result = {"schema_version": 1, "query": q, "query_sha256": query_sha, "state": state, "attempts": attempts, "sources": sources, "candidates": all_candidates, "failures": failures}
    result["candidate_evidence_sha256"] = _sha(json.dumps(all_candidates, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode())
    if cache:
        cache.parent.mkdir(parents=True, exist_ok=True)
        tmp = cache.with_suffix(cache.suffix + ".tmp")
        tmp.write_text(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        tmp.replace(cache)
    return result


def download_cover(url: str, destination: str | Path, *, timeout_seconds: int = 8, retry: bool = False) -> dict:
    """Download a public cover as an unverified candidate; preserve bytes and normalized JPEG."""
    try:
        from PIL import Image, ImageOps
    except ImportError as exc:
        return {"status": "request_failed", "verified": False, "error": f"Pillow unavailable: {exc}"}
    timeout = _bounded_int(timeout_seconds, 1, 20)
    dest = Path(destination)
    raw_dest = dest.with_name(dest.stem + ".original" + (dest.suffix or ".bin"))
    attempts = 0
    last_error = "download failed"
    for attempt in range(2 if retry else 1):
        attempts += 1
        try:
            import io
            raw, final_url = _request(url, timeout_seconds=timeout, limit=MAX_COVER_BYTES)
            if not raw:
                raise ValueError("empty image response")
            import warnings
            with warnings.catch_warnings():
                warnings.simplefilter("error", Image.DecompressionBombWarning)
                with Image.open(io.BytesIO(raw)) as probe:
                    if probe.width * probe.height > 50_000_000:
                        raise ValueError("image dimensions exceed safety limit")
                    probe.verify()
                with Image.open(io.BytesIO(raw)) as image:
                    if image.width * image.height > 50_000_000:
                        raise ValueError("image dimensions exceed safety limit")
                    image.load()
                    image = ImageOps.exif_transpose(image).convert("RGB")
                    width, height = image.size
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    fd_raw, raw_stage_name = tempfile.mkstemp(prefix=".cover-raw-", dir=dest.parent)
                    fd_jpeg, jpeg_stage_name = tempfile.mkstemp(prefix=".cover-jpeg-", dir=dest.parent)
                    raw_stage, jpeg_stage = Path(raw_stage_name), Path(jpeg_stage_name)
                    old_raw = raw_dest.read_bytes() if raw_dest.is_file() else None
                    old_jpeg = dest.read_bytes() if dest.is_file() else None
                    try:
                        with os.fdopen(fd_raw, "wb") as stream:
                            stream.write(raw)
                        with os.fdopen(fd_jpeg, "wb") as stream:
                            image.save(stream, format="JPEG", quality=92, optimize=True)
                        try:
                            os.replace(raw_stage, raw_dest)
                            os.replace(jpeg_stage, dest)
                        except OSError:
                            if old_raw is None:
                                raw_dest.unlink(missing_ok=True)
                            else:
                                raw_dest.write_bytes(old_raw)
                            if old_jpeg is None:
                                dest.unlink(missing_ok=True)
                            else:
                                dest.write_bytes(old_jpeg)
                            raise
                    finally:
                        raw_stage.unlink(missing_ok=True)
                        jpeg_stage.unlink(missing_ok=True)
            jpeg = dest.read_bytes()
            return {"status": "candidate_downloaded", "verified": False, "source_url": final_url, "raw_path": str(raw_dest), "path": str(dest), "raw_sha256": _sha(raw), "jpeg_sha256": _sha(jpeg), "width": width, "height": height, "attempts": attempts, "warning": "candidate image requires visual review before use"}
        except Exception as exc:
            last_error = f"{type(exc).__name__}: {str(exc)[:300]}"
            if attempt + 1 < (2 if retry else 1):
                time.sleep(0.15)
    return {"status": "request_failed", "verified": False, "source_url": url, "attempts": attempts, "error": last_error}
