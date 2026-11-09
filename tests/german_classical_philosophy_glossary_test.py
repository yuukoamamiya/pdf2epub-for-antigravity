from pathlib import Path
import yaml
import pytest

from pdf2epub.glossary import (
    load_glossary,
    project_glossary,
    validate_glossary_languages,
    validate_reference_glossary_languages,
)

ROOT = Path(__file__).resolve().parents[1]
GLOSSARY_PATH = ROOT / "glossaries" / "german-classical-philosophy.yaml"


def test_german_classical_philosophy_glossary_supports_english_and_german():
    data = load_glossary(GLOSSARY_PATH)
    assert data["schema_version"] == 2
    entries = data["entries"]
    assert len(entries) == 561

    # All entries have en, de, zh-Hans
    for e in entries:
        terms = e["terms"]
        assert "de" in terms
        assert "en" in terms
        assert "zh-Hans" in terms

    # Project English -> Chinese
    proj_en = project_glossary(data, "English", "Chinese")
    assert len(proj_en["entries"]) == 561

    # Project German -> Chinese
    proj_de = project_glossary(data, "German", "Chinese")
    assert len(proj_de["entries"]) == 561

    # Validate languages
    assert not validate_glossary_languages(data, "English", "Chinese")
    assert not validate_glossary_languages(data, "German", "Chinese")
    assert not validate_reference_glossary_languages(data, "Chinese", "English")
    assert not validate_reference_glossary_languages(data, "Chinese", "German")


def test_core_hegelian_and_kantian_english_alignments():
    data = load_glossary(GLOSSARY_PATH)
    by_id = {e["id"]: e for e in data["entries"]}

    aufheben = by_id["german-classical-philosophy:aufheben"]
    assert aufheben["terms"]["en"]["preferred"] == "sublate"
    assert "sublation" in aufheben["terms"]["en"]["variants"]
    assert aufheben["terms"]["zh-Hans"]["preferred"] == "扬弃"

    geist = by_id["german-classical-philosophy:geist"]
    assert geist["terms"]["en"]["preferred"] == "spirit"
    assert geist["terms"]["en"]["aliases"] == ["mind"]
    assert geist["terms"]["zh-Hans"]["preferred"] == "精神"

    begriff = by_id["german-classical-philosophy:begriff"]
    assert begriff["terms"]["en"]["preferred"] == "concept"
    assert begriff["terms"]["en"]["aliases"] == ["Notion"]
    assert begriff["terms"]["zh-Hans"]["preferred"] == "概念"

    ding_an_sich = by_id["german-classical-philosophy:ding-an-sich"]
    assert ding_an_sich["terms"]["en"]["preferred"] == "thing in itself"
    assert ding_an_sich["terms"]["zh-Hans"]["preferred"] == "自在之物"

    sittlichkeit = by_id["german-classical-philosophy:sittlichkeit"]
    assert sittlichkeit["terms"]["en"]["preferred"] == "ethical life"
    assert sittlichkeit["terms"]["zh-Hans"]["preferred"] == "伦理"
