"""Pipeline-level contracts shared by PDF workflow commands.

The PDF conversion and translation workflows intentionally share the same
source preparation and EPUB building machinery.  This module keeps the
workflow policy in one place so command handlers do not each grow their own
set of ``pipeline`` special cases.
"""

from dataclasses import dataclass
from typing import Any, Mapping, Optional


CONVERSION_PIPELINE_NAMES = frozenset({"epub_conversion", "ocr_to_epub"})


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

    @property
    def is_conversion(self) -> bool:
        """Whether this is the language-neutral PDF-to-EPUB workflow."""
        return not self.requires_translation

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
        kind = "epub_conversion" if configured_kind in CONVERSION_PIPELINE_NAMES else configured_kind
        if not kind:
            kind = "translation"

        translation = config.get("translation")
        translation = translation if isinstance(translation, Mapping) else {}
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
    "PipelinePolicy",
    "pipeline_policy",
]
