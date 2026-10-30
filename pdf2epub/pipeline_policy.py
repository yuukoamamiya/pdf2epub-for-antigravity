"""Pipeline-level contracts shared by PDF workflow commands.

The PDF conversion and translation workflows intentionally share the same
source preparation and EPUB building machinery.  This module keeps the
workflow policy in one place so command handlers do not each grow their own
set of ``pipeline`` special cases.
"""

from dataclasses import dataclass, field
from typing import Any, Mapping, Optional

from pdf2epub.ocr_options import secondary_ocr_enabled


CONVERSION_PIPELINE_NAMES = frozenset({"epub_conversion", "ocr_to_epub"})
SUPPORTED_PIPELINE_NAMES = frozenset({"translation", *CONVERSION_PIPELINE_NAMES})


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
    requires_ocr_correction: bool = True
    special_unit_modes: Mapping[str, str] = field(default_factory=dict)

    @property
    def is_conversion(self) -> bool:
        """Whether this is the language-neutral PDF-to-EPUB workflow."""
        return not self.requires_translation

    def requires_visual_ocr_review(
        self, output_dir: Any = None, ocr_evidence=None
    ) -> bool:
        """Whether workflow policy or current raw pages require visual review."""
        if output_dir is not None:
            from pdf2epub.ocr_source import is_legacy_native_source

            if is_legacy_native_source(output_dir):
                return False
        if self.requires_ocr_correction:
            return True
        if output_dir is None:
            return False
        from pdf2epub.ocr_anomalies import evidence_report_for_output, scan_ocr_pages

        report = evidence_report_for_output(ocr_evidence, output_dir)
        if report is None:
            report = scan_ocr_pages(output_dir, persist=False)
        return bool(report["review_required_files"] or report.get("scan_errors"))

    @classmethod
    def from_config(cls, config: Optional[Mapping[str, Any]]) -> "PipelinePolicy":
        """Build a policy from the nested YAML configuration.

        Missing or legacy pipeline settings retain the historical translation
        workflow.  ``mode: ocr_to_epub`` remains a compatibility alias but is
        normalized to the canonical ``epub_conversion`` kind in reports.
        """
        config = config if isinstance(config, Mapping) else {}
        configured_kind = str(
            config.get("pipeline") or config.get("mode") or "translation"
        ).strip().lower()
        if configured_kind not in SUPPORTED_PIPELINE_NAMES:
            raise ValueError(f"unsupported pipeline: {configured_kind}; expected one of {sorted(SUPPORTED_PIPELINE_NAMES)}")
        kind = "epub_conversion" if configured_kind in CONVERSION_PIPELINE_NAMES else configured_kind

        translation = config.get("translation")
        translation = translation if isinstance(translation, Mapping) else {}
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
        if special and kind != "translation":
            raise ValueError("translation.special_units is supported only by the PDF translation pipeline")
        requires_translation = kind != "epub_conversion"
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
            requires_translated_toc=requires_translation,
            # The optional second OCR pass and its visual correction gate are
            # one feature: enabling the secondary backend requires correction
            # for scanned PDFs. With one OCR backend, raw pages remain the
            # selected source for backwards-compatible single-pass runs.
            requires_ocr_correction=secondary_ocr_enabled(config),
            special_unit_modes=dict(special or {}),
            # Every PDF workflow must pass the polish gate, including pure
            # conversion.  Translation-specific gates are separate flags.
            requires_polish=True,
            source_language=str(source_language) if source_language else None,
            target_language=str(target_language) if target_language else None,
        )


def pipeline_policy(config: Optional[Mapping[str, Any]]) -> PipelinePolicy:
    """Short functional form used by command modules."""
    return PipelinePolicy.from_config(config)


__all__ = [
    "CONVERSION_PIPELINE_NAMES",
    "SUPPORTED_PIPELINE_NAMES",
    "PipelinePolicy",
    "pipeline_policy",
]
