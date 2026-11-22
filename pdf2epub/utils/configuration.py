"""Configuration loading and workflow policy validation."""
from pathlib import Path
from typing import Any, Dict, Mapping, Set
import yaml
from pdf2epub.pipeline_policy import PipelinePolicy

BASE_KEY = "base"


def _read_yaml_mapping(path: Path) -> Dict[str, Any]:
    with path.open("r", encoding="utf-8") as file:
        data = yaml.safe_load(file) or {}
    if not isinstance(data, Mapping):
        raise ValueError(f"configuration root must be a mapping: {path}")
    return dict(data)


def _resolve_base_path(config_path: Path, base_value: Any) -> Path:
    raw = str(base_value or "").strip()
    if not raw:
        raise ValueError(
            f"invalid `base` value in {config_path}: expected a relative or absolute file path"
        )
    candidate = Path(raw).expanduser()
    if not candidate.is_absolute():
        candidate = config_path.parent / candidate
    return candidate


def _load_with_base(config_path: Path, seen: Set[Path]) -> Dict[str, Any]:
    resolved = config_path.expanduser().resolve()
    if resolved in seen:
        chain = " -> ".join(str(path) for path in [*seen, resolved])
        raise ValueError(f"configuration base cycle detected: {chain}")
    if not resolved.is_file():
        raise FileNotFoundError(f"configuration file not found: {config_path}")

    data = _read_yaml_mapping(resolved)
    base_value = data.pop(BASE_KEY, None)
    if base_value is None:
        return data

    base_path = _resolve_base_path(resolved, base_value)
    base_data = _load_with_base(base_path, seen | {resolved})
    # Shallow merge: a section declared by the book config replaces the same
    # section from the base instead of merging key by key. This keeps override
    # semantics predictable; restate a whole section to change one nested key.
    merged = dict(base_data)
    merged.update(data)
    return merged


def load_config(config_path: str = "config.yaml") -> Dict:
    """Load the nested YAML configuration, resolving an optional ``base`` file.

    A config may declare ``base: <path>``. The path is resolved relative to the
    declaring file, and the base document is shallow-merged underneath the
    current document. Bases may themselves declare a base; cycles fail loudly.
    """
    config = _load_with_base(Path(config_path), set())
    from pdf2epub.ocr_consensus import validate_ocr_config
    from pdf2epub.metadata_enrichment.options import validate_config as validate_metadata_config

    validate_ocr_config(config)
    validate_metadata_config(config)
    PipelinePolicy.from_config(config)
    return config
