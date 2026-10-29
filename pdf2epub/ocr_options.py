"""Dependency-light OCR configuration accessors."""

from __future__ import annotations

from typing import Any, Mapping


def _ocr_config(config: Mapping[str, Any]) -> Mapping[str, Any]:
    value = config.get("ocr", {}) if isinstance(config, Mapping) else {}
    return value if isinstance(value, Mapping) else {}


def _config_bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"true", "yes", "on", "1"}:
            return True
        if normalized in {"false", "no", "off", "0"}:
            return False
    return default


def secondary_ocr_enabled(config: Mapping[str, Any]) -> bool:
    """Return whether the optional secondary OCR pass is enabled."""
    ocr = _ocr_config(config)
    secondary = ocr.get("secondary")
    if isinstance(secondary, Mapping):
        backend = str(secondary.get("backend") or "").strip()
        if "enabled" in secondary:
            return _config_bool(secondary.get("enabled"), False)
        return bool(backend)
    return bool(str(ocr.get("secondary_backend") or "").strip())
