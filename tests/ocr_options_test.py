"""Coverage for dependency-light OCR configuration accessors."""

from pdf2epub.ocr_options import _config_bool, _ocr_config, secondary_ocr_enabled


def test_ocr_config_normalizes_missing_or_invalid_values():
    assert _ocr_config({"ocr": {"backend": "chandra"}}) == {"backend": "chandra"}
    assert _ocr_config({}) == {}
    assert _ocr_config({"ocr": "not-a-mapping"}) == {}
    assert _ocr_config(None) == {}


def test_config_bool_accepts_booleans_strings_and_defaults():
    assert _config_bool(True, False) is True
    assert _config_bool(" YES ", False) is True
    assert _config_bool("off", True) is False
    assert _config_bool(None, True) is True
    assert _config_bool("maybe", False) is False


def test_secondary_ocr_enabled_prefers_explicit_enabled_flag():
    config = {"ocr": {"secondary": {"enabled": False, "backend": "paddle"}}}
    assert secondary_ocr_enabled(config) is False

    config = {"ocr": {"secondary": {"enabled": "true", "backend": "paddle"}}}
    assert secondary_ocr_enabled(config) is True


def test_secondary_ocr_enabled_falls_back_to_backend_presence():
    assert secondary_ocr_enabled({"ocr": {"secondary": {"backend": "paddle"}}}) is True
    assert secondary_ocr_enabled({"ocr": {"secondary": {"backend": "  "}}}) is False
    assert secondary_ocr_enabled({"ocr": {"secondary_backend": "paddle"}}) is True
    assert secondary_ocr_enabled({"ocr": {}}) is False
    assert secondary_ocr_enabled({}) is False
