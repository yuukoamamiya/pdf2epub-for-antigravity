from pathlib import Path
import json
import yaml


ROOT = Path(__file__).resolve().parents[1]
REVIEW = json.loads(
    (ROOT / "glossaries/reviews/marxism-de-zh-supplement-2026-11-03.json").read_text(encoding="utf-8")
)
REVIEWED_UPDATES = {item["concept_id"]: item for item in REVIEW["existing_updates"]}
COUNTS = {
    "marxism.yaml": 286,
    "german-classical-philosophy.yaml": 561,
    "psychoanalysis.yaml": 175,
    "communication.yaml": 51,
    "games.yaml": 166,
}
SOURCE_LANGUAGES = {
    "marxist-political-economy-en-zh.yaml": "en",
    "marxist-theory-de-zh.yaml": "de",
    "german-classical-philosophy.yaml": "de",
    "lacanian-psychoanalysis-en-zh.yaml": "en",
    "political-economy-of-communication.yaml": "en",
    "crpg-common-zh-cn.yaml": "en",
    "nintendo-common-zh-cn.yaml": "en",
}


def test_consolidated_tables_preserve_all_original_records():
    tables = sorted((ROOT / "glossaries").glob("*.yaml"))
    assert {path.name for path in tables} == set(COUNTS)
    seen_ids = set()
    counts = {}
    for path in tables:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        assert data["schema_version"] == 2
        assert set(data["metadata"]) == {"id", "name", "domain", "version"}
        assert data["metadata"]["id"] == path.stem
        historical_entries = [entry for entry in data["entries"] if "original_record" in entry.get("provenance", {})]
        counts[path.name] = len(historical_entries)
        for entry in data["entries"]:
            assert entry["id"] not in seen_ids
            seen_ids.add(entry["id"])
            provenance = entry["provenance"]
            if "original_record" not in provenance:
                # Explicitly reviewed additions have their own provenance contract.
                assert entry["id"].startswith("marxism:")
                continue
            original = provenance["original_record"]
            source_table = provenance["source_table"]
            assert source_table in SOURCE_LANGUAGES
            assert provenance["source_id"] == original["id"]
            assert entry["id"] == f"{Path(source_table).stem}:{original['id']}"
            if entry["id"] in REVIEWED_UPDATES:
                update = REVIEWED_UPDATES[entry["id"]]
                before = update["before"]
                after = update["after"]
                assert original == update["original_record"]
                assert before["terms"]["zh-Hans"]["preferred"] == original["target"]
                assert before.get("note") == original.get("note")
                assert entry["terms"] == after["terms"]
                assert entry.get("note") == after.get("note")
                assert update["historical_original_record_unchanged"] is True
            else:
                source_language = SOURCE_LANGUAGES[source_table]
                ambiguous_source_forms = {
                    item["form"]
                    for item in provenance.get("ambiguous_forms", [])
                    if item.get("language") == source_language
                }
                quarantined_forms = set(provenance.get("legacy_unclassified_forms", []))
                source_variants = [
                    form for form in original.get("variants", [])
                    if form not in ambiguous_source_forms and form not in quarantined_forms
                ]
                source_terms = {"preferred": original["source"]}
                if source_variants:
                    source_terms["variants"] = source_variants
                for key in ("aliases", "allow_short"):
                    if key in original:
                        source_terms[key] = original[key]
                expected_terms = {
                    source_language: source_terms,
                    "zh-Hans": {
                        "preferred": original["target"],
                        "policy": original.get("policy", "preferred"),
                    },
                }
                language_codes = {"French": "fr", "German": "de", "English": "en"}
                for language, forms in original.get("aliases_by_language", {}).items():
                    code = language_codes[language]
                    expected_terms[code] = {"preferred": forms[0]}
                assert entry["terms"] == expected_terms, entry["id"]
                assert entry.get("note") == original.get("note")
            assert entry.get("category") == original.get("category")
    assert set(REVIEWED_UPDATES) == {
        "marxist-political-economy-en-zh:new-marx-reading",
        "marxist-political-economy-en-zh:labour-theory-of-value",
        "marxist-political-economy-en-zh:commodity-fetishism",
        "marxist-political-economy-en-zh:real-abstraction",
        "marxist-political-economy-en-zh:traditional-marxism",
        "marxist-political-economy-en-zh:class-struggle",
        "marxist-political-economy-en-zh:classical-political-economy",
    }
    assert counts == COUNTS


def test_psychoanalysis_untyped_variants_remain_unclassified():
    data = yaml.safe_load((ROOT / "glossaries/psychoanalysis.yaml").read_text(encoding="utf-8"))
    entries = {entry["provenance"]["source_id"]: entry for entry in data["entries"]}
    anxiety = entries["anxiety"]
    assert "fr" not in anxiety["terms"] or anxiety["terms"]["fr"]["preferred"] == "angoisse"
    assert "variants" not in anxiety["terms"]["en"]
    assert set(anxiety["provenance"]["legacy_unclassified_forms"]) == {"angoisse", "Angst"}


def test_zelda_series_short_alias_is_quarantined_from_product_title():
    data = yaml.safe_load((ROOT / "glossaries/games.yaml").read_text(encoding="utf-8"))
    entries = {entry["id"]: entry for entry in data["entries"]}
    series = entries["crpg-common-zh-cn:legend-of-zelda-series"]
    title = entries["nintendo-common-zh-cn:the-legend-of-zelda"]

    assert series["terms"]["en"]["preferred"] == "The Legend of Zelda series"
    assert "variants" not in series["terms"]["en"]
    assert title["terms"]["en"]["preferred"] == "The Legend of Zelda"
    assert series["provenance"]["original_record"]["variants"] == ["The Legend of Zelda"]
    assert series["provenance"]["ambiguous_forms"] == [{
        "language": "en",
        "form": "The Legend of Zelda",
        "reason": "collision_with_canonical_source",
        "other_concept_id": "nintendo-common-zh-cn:the-legend-of-zelda",
    }]
