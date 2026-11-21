"""Pipeline-level contracts shared by workflow commands.

The PDF conversion and translation workflows intentionally share the same
source preparation and EPUB building machinery, while the HTML, light-novel
and TeX workflows reuse the same translation hand-off/runtime.  This module
keeps every pipeline's capability flags in one place so command handlers do
not each grow their own set of ``pipeline`` special cases.
"""

from dataclasses import dataclass, field
from typing import Any, Mapping, Optional

from pdf2epub.ocr_options import secondary_ocr_enabled


PDF_TRANSLATION_PIPELINE = "pdf_translation"
PDF_CONVERSION_PIPELINE = "pdf_conversion"
HTML_PIPELINE = "html"
NOVEL_PIPELINE = "novel"
TEX_PIPELINE = "tex"

PDF_PIPELINE_KINDS = frozenset({PDF_TRANSLATION_PIPELINE, PDF_CONVERSION_PIPELINE})
TRANSLATION_PIPELINE_KINDS = frozenset(
    {PDF_TRANSLATION_PIPELINE, HTML_PIPELINE, NOVEL_PIPELINE, TEX_PIPELINE}
)

# Historical config values remain accepted and are normalized to the canonical
# kind.  ``translation``/``epub_conversion`` appear in many existing book
# configurations; ``ocr_to_epub`` is the oldest conversion spelling.
PIPELINE_KIND_ALIASES = {
    "translation": PDF_TRANSLATION_PIPELINE,
    "epub_conversion": PDF_CONVERSION_PIPELINE,
    "ocr_to_epub": PDF_CONVERSION_PIPELINE,
}
CONVERSION_PIPELINE_NAMES = frozenset(
    {PDF_CONVERSION_PIPELINE, "epub_conversion", "ocr_to_epub"}
)
SUPPORTED_PIPELINE_NAMES = frozenset(
    {PDF_TRANSLATION_PIPELINE, PDF_CONVERSION_PIPELINE, HTML_PIPELINE, NOVEL_PIPELINE, TEX_PIPELINE}
)


@dataclass(frozen=True)
class PipelinePolicy:
    """Capabilities and requirements for one configured PDF pipeline."""

    kind: str
    requires_translation: bool
    requires_entities: bool
    requires_translated_toc: bool
    requires_polish: bool
    source_language: Optional[str]
    target_language: Optional[str]
    dynamic_terminology_enabled: bool = False
    requires_ocr_correction: bool = False
    requires_compressed_cache: bool = False
    requires_compiled_pdf: bool = False
    special_unit_modes: Mapping[str, str] = field(default_factory=dict)

    @property
    def is_conversion(self) -> bool:
        """Whether this is the language-neutral PDF-to-EPUB workflow."""
        return not self.requires_translation

    @property
    def is_pdf(self) -> bool:
        """Whether this pipeline consumes a PDF and runs the OCR stages."""
        return self.kind in PDF_PIPELINE_KINDS

    def requires_visual_ocr_review(
        self, output_dir: Any = None, ocr_evidence=None
    ) -> bool:
        """Whether workflow policy or current raw pages require visual review."""
        from pdf2epub.ocr_review_selection import has_manual_review_selection

        manual_selection = (
            has_manual_review_selection(output_dir) if output_dir is not None else False
        )
        if output_dir is not None:
            from pdf2epub.refine.pdf_evidence import is_native_text_source

            if is_native_text_source(output_dir):
                return manual_selection
        if self.requires_ocr_correction:
            return True
        if output_dir is None:
            return False
        if manual_selection:
            return True
        from pdf2epub.ocr_anomalies import evidence_report_for_output, scan_ocr_pages

        report = evidence_report_for_output(ocr_evidence, output_dir)
        if report is None:
            report = scan_ocr_pages(output_dir, persist=False)
        return bool(report["review_required_files"] or report.get("scan_errors"))

    @classmethod
    def from_config(
        cls,
        config: Optional[Mapping[str, Any]],
        *,
        default_kind: Optional[str] = None,
    ) -> "PipelinePolicy":
        """Build a policy from the nested YAML configuration.

        Missing or legacy pipeline settings retain the historical PDF
        translation workflow.  ``translation``/``epub_conversion``/
        ``ocr_to_epub`` remain compatibility aliases and are normalized to the
        canonical ``pdf_translation``/``pdf_conversion`` kinds in reports.
        """
        config = config if isinstance(config, Mapping) else {}
        configured_kind = str(
            config.get("pipeline")
            or config.get("mode")
            or default_kind
            or PDF_TRANSLATION_PIPELINE
        ).strip().lower()
        raw_kind = PIPELINE_KIND_ALIASES.get(configured_kind, configured_kind)
        if raw_kind not in SUPPORTED_PIPELINE_NAMES:
            raise ValueError(
                f"unsupported pipeline: {configured_kind}; expected one of "
                f"{sorted(SUPPORTED_PIPELINE_NAMES)} "
                f"(aliases: {sorted(PIPELINE_KIND_ALIASES)})"
            )
        kind = raw_kind

        translation = config.get("translation")
        translation = translation if isinstance(translation, Mapping) else {}
        dynamic = translation.get("dynamic_terminology", {}) or {}
        if not isinstance(dynamic, Mapping):
            raise ValueError("translation.dynamic_terminology must be a mapping")
        unknown_dynamic = set(dynamic) - {"enabled"}
        if unknown_dynamic:
            raise ValueError(
                "unknown translation.dynamic_terminology key(s): "
                + ", ".join(sorted(str(key) for key in unknown_dynamic))
            )
        dynamic_enabled = dynamic.get("enabled", False)
        if not isinstance(dynamic_enabled, bool):
            raise ValueError("translation.dynamic_terminology.enabled must be a boolean")
        if dynamic_enabled and kind not in TRANSLATION_PIPELINE_KINDS:
            raise ValueError(
                "translation.dynamic_terminology is supported only by translation pipelines"
            )
        special = translation.get("special_units", {})
        if special is not None and not isinstance(special, Mapping):
            raise ValueError("translation.special_units must be a mapping")
        for name, value in (special or {}).items():
            if name not in {"bibliography", "index"}:
                raise ValueError(f"unknown translation.special_units key: {name}")
            if not isinstance(value, str) or value != "controlled":
                raise ValueError(
                    f"translation.special_units.{name} must be 'controlled' when specified"
                )
        if special and kind != PDF_TRANSLATION_PIPELINE:
            raise ValueError("translation.special_units is supported only by the PDF translation pipeline")
        requires_translation = kind in TRANSLATION_PIPELINE_KINDS
        requires_entities = requires_translation and bool(
            translation.get("require_entities", True)
        )
        source_language = (
            translation.get("source_language", "English")
            if requires_translation
            else None
        )
        target_language = (
            translation.get("target_language", "Chinese")
            if requires_translation
            else None
        )
        return cls(
            kind=kind,
            requires_translation=requires_translation,
            requires_entities=requires_entities,
            requires_translated_toc=kind == PDF_TRANSLATION_PIPELINE,
            # The optional second OCR pass and its visual correction gate are
            # one feature: enabling the secondary backend requires correction
            # for scanned PDFs. With one OCR backend, raw pages remain the
            # selected source for backwards-compatible single-pass runs.
            requires_ocr_correction=(
                kind in PDF_PIPELINE_KINDS and secondary_ocr_enabled(config)
            ),
            special_unit_modes=dict(special or {}),
            # Every PDF workflow must pass the polish gate, including pure
            # conversion.  Translation-specific gates are separate flags.
            requires_polish=kind in PDF_PIPELINE_KINDS,
            # EPUB translation keeps a compressed-unit cache; TeX validation
            # produces (and checks) a compiled PDF.
            requires_compressed_cache=kind == HTML_PIPELINE,
            requires_compiled_pdf=kind == TEX_PIPELINE,
            source_language=str(source_language) if source_language else None,
            target_language=str(target_language) if target_language else None,
            dynamic_terminology_enabled=dynamic_enabled,
        )


def pipeline_policy(
    config: Optional[Mapping[str, Any]],
    *,
    default_kind: Optional[str] = None,
) -> PipelinePolicy:
    """Short functional form used by command modules."""
    return PipelinePolicy.from_config(config, default_kind=default_kind)


__all__ = [
    "CONVERSION_PIPELINE_NAMES",
    "HTML_PIPELINE",
    "NOVEL_PIPELINE",
    "PDF_CONVERSION_PIPELINE",
    "PDF_PIPELINE_KINDS",
    "PDF_TRANSLATION_PIPELINE",
    "PIPELINE_KIND_ALIASES",
    "SUPPORTED_PIPELINE_NAMES",
    "TEX_PIPELINE",
    "TRANSLATION_PIPELINE_KINDS",
    "PipelinePolicy",
    "pipeline_policy",
]
