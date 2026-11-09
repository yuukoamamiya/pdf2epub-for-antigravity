"""Normalize language names/codes to ISO 639-1 two-letter codes."""

import re

_LANGUAGE_CODES = {
    "english": "en",
    "german": "de",
    "deutsch": "de",
    "french": "fr",
    "spanish": "es",
    "italian": "it",
    "russian": "ru",
    "japanese": "ja",
    "chinese": "zh",
    "korean": "ko",
    "latin": "la",
    "greek": "el",
    "portuguese": "pt",
    "dutch": "nl",
    "polish": "pl",
    "czech": "cs",
    "swedish": "sv",
    "danish": "da",
    "norwegian": "no",
    "finnish": "fi",
    "turkish": "tr",
    "arabic": "ar",
    "hebrew": "he",
    "original": "en",
}

_TWO_LETTER_CODE = re.compile(r"^[a-z]{2}([-_].*)?$")

__all__ = ["language_code"]


def language_code(name: str) -> str:
    """Return the ISO 639-1 two-letter code for a language name or code.

    Unknown values fall back to a short sanitized token, or ``"en"`` when no
    usable letters remain.
    """
    value = (name or "").strip()
    if not value:
        return "en"

    normalized = value.lower()
    mapped = _LANGUAGE_CODES.get(normalized)
    if mapped:
        return mapped

    if _TWO_LETTER_CODE.match(normalized):
        return normalized[:2]

    sanitized = re.sub(r"[^a-z]+", "", normalized)[:8]
    return sanitized or "en"
