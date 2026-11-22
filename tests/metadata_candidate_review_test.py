"""Offline end-to-end contracts for metadata candidate review and validation."""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from PIL import Image

from pdf2epub.metadata_enrichment import network
from pdf2epub.metadata_enrichment.service import (
    fetch_enrichment,
    prepare_enrichment,
    resolve_enrichment,
    validate_enrichment,
)


def _decision(output: Path, config: dict) -> tuple[Path, dict, dict]:
    prepare_enrichment(output, config)
    root = output / "metadata_enrichment"
    evidence = json.loads((root / "evidence.json").read_text(encoding="utf-8"))
    ref = {
        "location": evidence["evidence"][0]["location"],
        "sha256": evidence["evidence"][0]["sha256"],
    }
    decision = {
        "schema_version": 1,
        "evidence_sha256": evidence["evidence_sha256"],
        "applicability": {
            "status": "publication",
            "rationale": "Synthetic title page identifies a published work.",
            "evidence_refs": [ref],
        },
        "identity": {
            "status": "verified",
            "document_type": "book",
            "work_title": "The Fixed Work",
            "authors": [{"name": "Ada Author", "role": "author"}],
            "source_edition": "English edition",
            "artifact_relation": "The input is a scan of the source edition.",
            "evidence_refs": [ref],
        },
        "fields": {},
    }
    (root / "decision.json").write_text(
        json.dumps(decision, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return root, evidence, decision


def _mock_catalog_and_cover(monkeypatch, *, cover_png: bytes | None = None) -> list[str]:
    calls: list[str] = []
    google = {
        "items": [
            {
                "selfLink": "https://www.googleapis.com/books/v1/volumes/fixed-volume",
                "volumeInfo": {
                    "title": "The Fixed Work",
                    "authors": ["Ada Author"],
                    "publisher": "Fixture Press",
                    "publishedDate": "2021",
                    "industryIdentifiers": [
                        {"type": "OTHER", "identifier": "10.1234/fixed-work"}
                    ],
                },
            }
        ]
    }
    open_library = {
        "docs": [
            {
                "key": "/works/OLfixedW",
                "title": "The Fixed Work",
                "author_name": ["Ada Author"],
                "first_publish_year": 2021,
                "doi": ["10.1234/fixed-work"],
            }
        ]
    }

    def fake_request(url: str, *, timeout_seconds: int, limit: int):
        del timeout_seconds, limit
        calls.append(url)
        if "googleapis.com" in url:
            body = json.dumps(google).encode("utf-8")
        elif "openlibrary.org/search.json" in url:
            body = json.dumps(open_library).encode("utf-8")
        elif cover_png is not None and url == "https://covers.example/fixed.png":
            body = cover_png
        else:
            raise AssertionError(f"unexpected request in offline test: {url}")
        return body, url

    monkeypatch.setattr(network, "_request", fake_request)
    return calls


def _local_evidence_ref(evidence: dict) -> dict:
    item = evidence["evidence"][0]
    return {"location": item["location"], "sha256": item["sha256"]}


def test_candidate_reference_is_reviewed_and_candidate_tamper_does_not_drop_local_fields(
    tmp_path: Path, monkeypatch
):
    output = tmp_path / "book"
    (output / "pages").mkdir(parents=True)
    (output / "pages" / "page_0001.md").write_text(
        "Title page: The Fixed Work\nAuthor: Ada Author", encoding="utf-8"
    )
    config = {
        "metadata_enrichment": {
            "mode": "auto",
            "online": True,
            "fields": ["identifiers", "publishing"],
            "max_candidates": 4,
        }
    }
    root, evidence, decision = _decision(output, config)
    local_ref = _local_evidence_ref(evidence)
    _mock_catalog_and_cover(monkeypatch)

    fetched = fetch_enrichment(output, config)
    assert fetched["state"] == "candidates"
    assert fetched["candidates"]
    candidate = next(item for item in fetched["candidates"] if item["source"] == "open_library")
    assert candidate["matched"] is False

    # The reviewer explicitly accepts the DOI from this exact candidate and
    # independently records publisher/date from local title-page evidence.
    decision["fields"] = {
        "identifiers": {
            "status": "verified",
            "value": {"doi": "10.1234/fixed-work"},
            "evidence_refs": [
                {"location": candidate["source_url"], "sha256": candidate["candidate_sha256"]}
            ],
        },
        "publishing": {
            "status": "verified",
            "value": {"publisher": "Fixture Press", "date": "2021"},
            "evidence_refs": [local_ref],
        },
    }
    (root / "decision.json").write_text(
        json.dumps(decision, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    report = validate_enrichment(output, config)
    assert report["state"] == "valid"
    assert report["current"] is True
    accepted = resolve_enrichment(output, config)
    assert accepted["metadata"]["identifiers"]["doi"] == "10.1234/fixed-work"
    assert accepted["metadata"]["publisher"] == "Fixture Press"

    original_fetch = json.loads((root / "fetch.json").read_text(encoding="utf-8"))

    # Tampering with the cited Open Library candidate invalidates the DOI, but
    # independently cited local publisher/date values remain usable.
    tampered = json.loads(json.dumps(original_fetch))
    cited = next(item for item in tampered["candidates"] if item["source"] == "open_library")
    cited["title"] = "Tampered cited candidate title"
    (root / "fetch.json").write_text(
        json.dumps(tampered, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    rejected = validate_enrichment(output, config)
    assert rejected["fields"]["identifiers"]["verified"] is False
    assert rejected["fields"]["publishing"]["verified"] is True

    # Restore and revalidate the exact reviewed candidate snapshot.
    (root / "fetch.json").write_text(
        json.dumps(original_fetch, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    restored = validate_enrichment(output, config)
    assert restored["fields"]["identifiers"]["verified"] is True
    accepted_receipt = (root / "validation.json").read_bytes()

    # Editing a different, uncited provider result must not invalidate the
    # already-reviewed Open Library DOI. Resolve without another validation;
    # the prior receipt and independently sourced local fields remain usable.
    tampered = json.loads(json.dumps(original_fetch))
    uncited = next(item for item in tampered["candidates"] if item["source"] == "google_books")
    uncited["title"] = "Tampered unreferenced candidate title"
    (root / "fetch.json").write_text(
        json.dumps(tampered, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    assert (root / "validation.json").read_bytes() == accepted_receipt
    remaining = resolve_enrichment(output, config)
    assert remaining["metadata"]["identifiers"]["doi"] == "10.1234/fixed-work"
    assert remaining["metadata"]["publisher"] == "Fixture Press"
    assert remaining["metadata"]["date"] == "2021"


def test_cover_download_hash_and_review_are_required_before_resolution(
    tmp_path: Path, monkeypatch
):
    output = tmp_path / "book"
    (output / "pages").mkdir(parents=True)
    (output / "pages" / "page_0001.md").write_text(
        "Title page: The Fixed Work\nAuthor: Ada Author", encoding="utf-8"
    )
    config = {
        "metadata_enrichment": {
            "mode": "auto",
            "online": True,
            "fields": ["cover"],
            "cover_policy": "fill_missing",
            "max_candidates": 4,
        }
    }
    root, evidence, decision = _decision(output, config)
    local_ref = _local_evidence_ref(evidence)
    from io import BytesIO

    buffer = BytesIO()
    Image.new("RGB", (650, 1000), color=(90, 120, 160)).save(buffer, format="PNG")
    raw_cover = buffer.getvalue()
    calls = _mock_catalog_and_cover(monkeypatch, cover_png=raw_cover)

    fetched = fetch_enrichment(
        output, config, cover_url="https://covers.example/fixed.png"
    )
    assert fetched["state"] == "candidates"
    cover_result = fetched["cover_candidate"]
    assert cover_result["status"] == "candidate_downloaded"
    assert cover_result["verified"] is False
    assert Path(cover_result["raw_path"]).read_bytes() == raw_cover
    assert len(calls) == 3

    # A downloaded image is still only a candidate. A separate review record
    # must bind its JPEG hash and describe its relation to the reviewed edition.
    decision["fields"] = {
        "cover": {
            "status": "verified",
            "value": {
                "jpeg_sha256": cover_result["jpeg_sha256"],
                "kind": "journal_cover",
                "visual_review": "The image title and author match the source edition.",
                "version_relation": "Cover for the reviewed English source edition.",
            },
            "evidence_refs": [
                {
                    "location": f"cover_candidate.jpg#{cover_result['jpeg_sha256']}",
                    "sha256": cover_result["jpeg_sha256"],
                },
                {
                    "location": f"cover_candidate.json#{cover_result['candidate_evidence_sha256']}",
                    "sha256": cover_result["candidate_evidence_sha256"],
                },
                local_ref,
            ],
        }
    }
    (root / "decision.json").write_text(
        json.dumps(decision, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    report = validate_enrichment(output, config)
    assert report["state"] == "valid"
    assert report["fields"]["cover"]["verified"] is True
    plan = resolve_enrichment(output, config)
    assert Path(plan["metadata"]["cover_path"]).read_bytes() == Path(
        cover_result["path"]
    ).read_bytes()

    # Visual classification and edition relation require actual review text.
    cover_value = decision["fields"]["cover"]["value"]
    for key, invalid_value in (("kind", "title_page"), ("visual_review", True), ("version_relation", "")):
        original = cover_value[key]
        cover_value[key] = invalid_value
        (root / "decision.json").write_text(json.dumps(decision), encoding="utf-8")
        assert validate_enrichment(output, config)["fields"]["cover"]["verified"] is False
        cover_value[key] = original
    (root / "decision.json").write_text(json.dumps(decision), encoding="utf-8")

    metadata = json.loads((root / "cover_candidate.json").read_text(encoding="utf-8"))
    metadata["width"] += 1
    (root / "cover_candidate.json").write_text(json.dumps(metadata), encoding="utf-8")
    invalid = validate_enrichment(output, config)
    assert invalid["fields"]["cover"]["verified"] is False


@pytest.mark.parametrize("reference_form", ["url", "fragment"])
def test_conflicting_catalog_candidates_require_review_for_all_reference_forms(tmp_path, monkeypatch, reference_form):
    output = tmp_path / "book"
    (output / "pages").mkdir(parents=True)
    (output / "pages" / "page_0001.md").write_text("Synthetic source edition", encoding="utf-8")
    config = {"metadata_enrichment": {"fields": ["publishing"], "online": True}}
    root, evidence, decision = _decision(output, config)
    decision["identity"]["work_title"] = "An Entirely Unrelated Treatise"
    decision["identity"]["authors"] = [{"name": "Someone Different", "role": "author"}]
    (root / "decision.json").write_text(json.dumps(decision), encoding="utf-8")
    _mock_catalog_and_cover(monkeypatch)
    result = fetch_enrichment(output, config)
    candidate = result["candidates"][0]
    assert candidate["identity_match"]["hard_conflicts"]
    location = candidate["source_url"] if reference_form == "url" else f"fetch.json#{candidate['candidate_sha256']}"
    record = {"status": "verified", "value": {"publisher": "Fixture Press", "date": "2021"},
              "evidence_refs": [{"location": location, "sha256": candidate["candidate_sha256"]}]}
    decision["fields"] = {"publishing": record}
    (root / "decision.json").write_text(json.dumps(decision), encoding="utf-8")
    assert validate_enrichment(output, config)["fields"]["publishing"]["verified"] is False
    record["match_review"] = {
        "summary": "Synthetic reviewer confirms the catalog uses an alternate title and contributor form.",
        "edition_relation": "Alternate catalog entry for the reviewed 2021 English source edition.",
    }
    (root / "decision.json").write_text(json.dumps(decision), encoding="utf-8")
    assert validate_enrichment(output, config)["fields"]["publishing"]["verified"] is True

def test_small_cover_requires_explicit_review_acceptance(tmp_path: Path, monkeypatch):
    from io import BytesIO

    output = tmp_path / "small-cover-book"
    (output / "pages").mkdir(parents=True)
    (output / "pages" / "page_0001.md").write_text(
        "Title page: The Fixed Work\nAuthor: Ada Author", encoding="utf-8"
    )
    config = {
        "metadata_enrichment": {
            "mode": "auto",
            "online": True,
            "fields": ["cover"],
            "cover_policy": "fill_missing",
            "max_candidates": 4,
        }
    }
    root, evidence, decision = _decision(output, config)
    local_ref = _local_evidence_ref(evidence)
    buffer = BytesIO()
    Image.new("RGB", (24, 32), color=(90, 120, 160)).save(buffer, format="PNG")
    raw_cover = buffer.getvalue()
    _mock_catalog_and_cover(monkeypatch, cover_png=raw_cover)
    fetched = fetch_enrichment(output, config, cover_url="https://covers.example/fixed.png")
    cover_result = fetched["cover_candidate"]

    value = {
        "jpeg_sha256": cover_result["jpeg_sha256"],
        "kind": "article_page",
        "visual_review": "This is a source image with legible publication title and authors.",
        "version_relation": "Interior title page belonging to the reviewed English edition.",
    }
    refs = [
        {"location": f"cover_candidate.jpg#{cover_result['jpeg_sha256']}", "sha256": cover_result["jpeg_sha256"]},
        {"location": f"cover_candidate.json#{cover_result['candidate_evidence_sha256']}", "sha256": cover_result["candidate_evidence_sha256"]},
        local_ref,
    ]
    decision["fields"] = {"cover": {"status": "verified", "value": value, "evidence_refs": refs}}
    (root / "decision.json").write_text(json.dumps(decision, ensure_ascii=False, indent=2), encoding="utf-8")
    rejected = validate_enrichment(output, config)
    assert rejected["fields"]["cover"]["verified"] is False

    # Accepting a below-target candidate requires an explicit reviewer flag.
    value["accept_below_target"] = True
    (root / "decision.json").write_text(json.dumps(decision, ensure_ascii=False, indent=2), encoding="utf-8")
    accepted = validate_enrichment(output, config)
    assert accepted["fields"]["cover"]["verified"] is True
    assert accepted["fields"]["cover"]["status"] == "improved_below_target"
    assert resolve_enrichment(output, config)["metadata"]["cover_path"]
