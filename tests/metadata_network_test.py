import json
from pathlib import Path

from pdf2epub.metadata_enrichment import network


def test_candidate_is_explicitly_unverified_and_fingerprinted():
    candidate = network._candidate(
        "open_library",
        {"title": "A Work", "author_name": ["A. Author"], "first_publish_year": 1950, "key": "/works/OL1W"},
    )
    assert candidate["matched"] is False
    assert "human review" in candidate["warnings"][0]
    assert len(candidate["candidate_sha256"]) == 64
    assert candidate["source_url"] == "https://openlibrary.org/works/OL1W"


def test_private_urls_and_credentials_are_rejected():
    for url in ("http://127.0.0.1/", "http://localhost/", "https://user:pass@example.com/"):
        try:
            network._public_url(url)
        except ValueError:
            pass
        else:
            raise AssertionError(f"unsafe URL was accepted: {url}")


def test_fetch_candidates_caches_result_by_query_and_records_hashes(tmp_path, monkeypatch):
    cache = tmp_path / "fetch.json"
    raw = json.dumps({"docs": [{"title": "A Work", "author_name": ["A. Author"], "key": "/works/OL1W"}]}).encode()
    calls = []

    def fake_request(url, *, timeout_seconds, limit):
        calls.append(url)
        return raw, url

    monkeypatch.setattr(network, "_request", fake_request)
    first = network.fetch_candidates("A Work", timeout_seconds=3, max_candidates=4, cache_path=cache)
    second = network.fetch_candidates("A Work", timeout_seconds=3, max_candidates=4, cache_path=cache)
    assert first["state"] == "candidates"
    assert len(first["sources"]) == 2
    assert first["sources"][1]["response_sha256"] == network._sha(raw)
    assert first["candidate_evidence_sha256"]
    assert second == first
    assert len(calls) == 2


def test_failure_cache_requires_explicit_retry(tmp_path, monkeypatch):
    cache = tmp_path / "fetch.json"
    calls = []

    def fail_request(url, *, timeout_seconds, limit):
        calls.append(url)
        raise OSError("offline")

    monkeypatch.setattr(network, "_request", fail_request)
    first = network.fetch_candidates("A Work", timeout_seconds=2, max_candidates=1, cache_path=cache)
    second = network.fetch_candidates("A Work", timeout_seconds=2, max_candidates=1, cache_path=cache)
    assert first["state"] == "request_failed"
    assert second == first
    assert len(calls) == 4  # one bounded transient retry for each provider
    network.fetch_candidates("A Work", timeout_seconds=2, max_candidates=1, retry=True, cache_path=cache)
    assert len(calls) == 8


def test_cover_download_is_not_verified_and_keeps_original(tmp_path, monkeypatch):
    from PIL import Image
    import io

    buff = io.BytesIO()
    Image.new("RGB", (12, 8), "red").save(buff, format="PNG")
    raw = buff.getvalue()
    monkeypatch.setattr(network, "_request", lambda *a, **kw: (raw, "https://covers.example/1.png"))
    out = tmp_path / "cover.jpg"
    result = network.download_cover("https://covers.example/1.png", out)
    assert result["status"] == "candidate_downloaded"
    assert result["verified"] is False
    assert result["width"] == 12 and result["height"] == 8
    assert Path(result["raw_path"]).read_bytes() == raw
    assert Image.open(out).format == "JPEG"


def test_retry_reuses_successful_provider_and_retries_only_failure(tmp_path, monkeypatch):
    cache = tmp_path / "fetch.json"
    calls = []
    fail_open_library = {"enabled": True}

    def request(url, *, timeout_seconds, limit):
        calls.append(url)
        if "openlibrary.org" in url and fail_open_library["enabled"]:
            raise OSError("temporary outage")
        if "googleapis.com" in url:
            return json.dumps({"items": [{"volumeInfo": {"title": "A Work"}}]}).encode(), url
        return json.dumps({"docs": [{"title": "A Work", "key": "/works/OL1W"}]}).encode(), url

    monkeypatch.setattr(network, "_request", request)
    first = network.fetch_candidates("A Work", timeout_seconds=2, max_candidates=2, cache_path=cache)
    assert first["state"] == "candidates"
    assert first["failures"]
    google_calls = sum("googleapis.com" in url for url in calls)
    fail_open_library["enabled"] = False
    second = network.fetch_candidates("A Work", timeout_seconds=2, max_candidates=2, retry=True, cache_path=cache)
    assert second["state"] == "candidates"
    assert not second["failures"]
    assert sum("googleapis.com" in url for url in calls) == google_calls
    assert sum("openlibrary.org" in url for url in calls) > 2


def test_http_404_is_not_found_and_cached_separately(tmp_path, monkeypatch):
    import io
    import urllib.error

    cache = tmp_path / "fetch.json"
    calls = []

    def not_found(url, *, timeout_seconds, limit):
        calls.append(url)
        raise urllib.error.HTTPError(url, 404, "missing", {}, io.BytesIO())

    monkeypatch.setattr(network, "_request", not_found)
    result = network.fetch_candidates("No such book", timeout_seconds=2, max_candidates=1, cache_path=cache)
    again = network.fetch_candidates("No such book", timeout_seconds=2, max_candidates=1, cache_path=cache)
    assert result["state"] == "not_found"
    assert result["failures"] == []
    assert all(source["state"] == "not_found" for source in result["sources"])
    assert again == result and len(calls) == 2


def test_redirect_to_private_address_is_rejected_without_network(monkeypatch):
    import io
    import urllib.error

    class Redirector:
        def open(self, request, timeout):
            raise urllib.error.HTTPError(request.full_url, 302, "redirect", {"Location": "http://127.0.0.1/admin"}, io.BytesIO())

    monkeypatch.setattr(network, "_public_url", lambda url: url if "example.com" in url else (_ for _ in ()).throw(ValueError("private target")))
    monkeypatch.setattr(network, "_OPENER", Redirector())
    try:
        network._request("https://example.com/image", timeout_seconds=1, limit=10)
    except ValueError as exc:
        assert "private" in str(exc)
    else:
        raise AssertionError("private redirect target was accepted")


def test_oversized_response_is_rejected(tmp_path, monkeypatch):
    class Oversized:
        def __enter__(self):
            return self
        def __exit__(self, *args):
            return False
        def read(self, size):
            return b"x" * (size + 1)

    class Opener:
        def open(self, request, timeout):
            return Oversized()

    monkeypatch.setattr(network, "_public_url", lambda url: url)
    monkeypatch.setattr(network, "_OPENER", Opener())
    try:
        network._request("https://example.com/data", timeout_seconds=1, limit=10)
    except ValueError as exc:
        assert "size limit" in str(exc)
    else:
        raise AssertionError("oversized response was accepted")


def test_failed_cover_attempt_preserves_previous_success(tmp_path, monkeypatch):
    from PIL import Image
    import io

    raw = b"new image bytes"
    out = tmp_path / "cover.jpg"
    raw_out = tmp_path / "cover.original.jpg"
    out.write_bytes(b"previous normalized cover")
    raw_out.write_bytes(b"previous source image")
    monkeypatch.setattr(network, "_request", lambda *a, **kw: (raw, "https://covers.example/cover"))
    # Invalid bytes fail decoding before replacing either existing output.
    result = network.download_cover("https://covers.example/cover", out)
    assert result["status"] == "request_failed"
    assert out.read_bytes() == b"previous normalized cover"
    assert raw_out.read_bytes() == b"previous source image"
