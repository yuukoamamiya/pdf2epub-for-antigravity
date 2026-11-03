from pathlib import Path
import yaml


ROOT = Path(__file__).resolve().parents[1]
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
        counts[path.name] = len(data["entries"])
        for entry in data["entries"]:
            assert entry["id"] not in seen_ids
            seen_ids.add(entry["id"])
            provenance = entry["provenance"]
            original = provenance["original_record"]
            source_table = provenance["source_table"]
            assert source_table in SOURCE_LANGUAGES
            assert provenance["source_id"] == original["id"]
            assert entry["id"] == f"{Path(source_table).stem}:{original['id']}"
            assert entry["terms"][SOURCE_LANGUAGES[source_table]]["preferred"] == original["source"]
            assert entry["terms"]["zh-Hans"]["preferred"] == original["target"]
            assert entry["terms"]["zh-Hans"].get("policy", "preferred") == original.get("policy", "preferred")
            assert entry.get("note") == original.get("note")
            assert entry.get("category") == original.get("category")
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
