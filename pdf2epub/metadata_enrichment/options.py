"""Metadata enrichment configuration normalization."""

from __future__ import annotations

DEFAULTS = {
    "mode": "auto", "online": True,
    "fields": ["description", "subjects", "identifiers", "publishing"],
    "cover_policy": "preserve", "required_fields": [],
    "max_candidates": 8, "timeout_seconds": 8, "evidence_pages": 5,
}


def metadata_options(config: dict) -> dict:
    raw = config.get("metadata_enrichment", {})
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise ValueError("metadata_enrichment must be a mapping")
    unknown = set(raw) - (set(DEFAULTS) | {"source_note", "source_url"})
    if unknown:
        raise ValueError("unknown metadata_enrichment option(s): " + ", ".join(sorted(unknown)))
    result = {**DEFAULTS, **raw}
    if result["mode"] not in {"auto", "required", "disabled"}:
        raise ValueError("metadata_enrichment.mode must be auto, required, or disabled")
    if result["cover_policy"] not in {"preserve", "fill_missing", "prefer_verified"}:
        raise ValueError("metadata_enrichment.cover_policy is invalid")
    fields = result["fields"]
    allowed = {"description", "subjects", "identifiers", "publishing", "title", "authors", "cover"}
    if not isinstance(fields, list) or any(not isinstance(x, str) or x not in allowed for x in fields):
        raise ValueError("metadata_enrichment.fields must be a list of supported field names")
    required = result.get("required_fields")
    if not isinstance(required, list) or any(not isinstance(x, str) or x not in allowed for x in required):
        raise ValueError("metadata_enrichment.required_fields must be a list of supported field names")
    if not isinstance(result["online"], bool):
        raise ValueError("metadata_enrichment.online must be a boolean")
    for key in ("source_note", "source_url"):
        if key in raw and raw[key] is not None and not isinstance(raw[key], str):
            raise ValueError(f"metadata_enrichment.{key} must be text")
    if raw.get("source_url"):
        from urllib.parse import urlsplit
        parsed = urlsplit(raw["source_url"])
        if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.username or parsed.password:
            raise ValueError("metadata_enrichment.source_url must be a public HTTP(S) URL without credentials")
    result["fields"] = sorted(set(fields))
    if "required_fields" not in raw and result["mode"] == "required":
        required = result["fields"]
    elif result["mode"] == "required" and not required:
        raise ValueError("metadata_enrichment.mode required needs non-empty required_fields")
    if not set(required).issubset(result["fields"]):
        raise ValueError("metadata_enrichment.required_fields must be a subset of fields")
    result["required_fields"] = sorted(set(required))
    for key in ("max_candidates", "timeout_seconds", "evidence_pages"):
        if isinstance(result[key], bool) or not isinstance(result[key], int):
            raise ValueError(f"metadata_enrichment.{key} must be an integer")
    result["max_candidates"] = max(1, min(result["max_candidates"], 12))
    result["timeout_seconds"] = max(1, min(result["timeout_seconds"], 20))
    result["evidence_pages"] = max(1, min(result["evidence_pages"], 20))
    return result


def validate_config(config: dict) -> None:
    """Validate metadata options during normal configuration loading."""
    metadata_options(config)
