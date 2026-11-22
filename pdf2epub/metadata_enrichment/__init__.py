"""Evidence-bound metadata enrichment for EPUB packaging."""

from .service import (
    MetadataEnrichmentRequiredError,
    enrichment_status,
    fetch_enrichment,
    prepare_enrichment,
    resolve_enrichment,
    validate_enrichment,
)

__all__ = [
    "MetadataEnrichmentRequiredError", "enrichment_status", "fetch_enrichment",
    "prepare_enrichment", "resolve_enrichment", "validate_enrichment",
]
