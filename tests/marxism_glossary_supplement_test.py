from pathlib import Path
import json

import yaml

from pdf2epub.glossary import build_unit_glossary_contexts, load_glossary, project_glossary

ROOT = Path(__file__).resolve().parents[1]


def _glossary():
    return yaml.safe_load((ROOT / "glossaries/marxism.yaml").read_text(encoding="utf-8"))


def _entries():
    return {entry["id"]: entry for entry in _glossary()["entries"]}


def test_reviewed_supplement_has_twenty_new_concepts_and_seven_german_extensions():
    data = _glossary()
    entries = {entry["id"]: entry for entry in data["entries"]}
    additions = {key: value for key, value in entries.items() if key.startswith("marxism:")}
    assert len(data["entries"]) == 306
    assert len(additions) == 20
    assert all(value["terms"]["zh-Hans"]["policy"] == "preferred" for value in additions.values())
    assert all("de" in value["terms"] and "en" not in value["terms"] for value in additions.values())

    extended = {
        "new-marx-reading", "labour-theory-of-value", "commodity-fetishism",
        "real-abstraction", "traditional-marxism", "class-struggle", "classical-political-economy",
    }
    for suffix in extended:
        entry = entries[f"marxist-political-economy-en-zh:{suffix}"]
        assert "de" in entry["terms"]
        assert entry["provenance"]["original_record"]["source"] == entry["terms"]["en"]["preferred"]


def test_real_abstraction_has_reviewed_chinese_preference_and_german_source():
    entry = _entries()["marxist-political-economy-en-zh:real-abstraction"]
    assert entry["terms"]["en"]["preferred"] == "real abstraction"
    assert entry["terms"]["en"].get("variants", []) == []
    assert entry["terms"]["de"]["preferred"] == "Realabstraktion"
    assert entry["terms"]["zh-Hans"]["preferred"] == "真实抽象"
    assert "现实抽象" in entry["terms"]["zh-Hans"]["variants"]
    assert entry["terms"]["zh-Hans"]["policy"] == "preferred"
    assert entry["provenance"]["original_record"]["target"] == "现实抽象"


def test_new_terms_keep_scope_distinctions_and_do_not_restore_deferred_or_removed_terms():
    entries = _entries()
    assert entries["marxism:state-derivation"]["terms"]["zh-Hans"]["preferred"] == "国家衍生"
    assert entries["marxism:state-derivation-debate"]["terms"]["zh-Hans"]["preferred"] == "国家衍生争论"
    assert entries["marxist-political-economy-en-zh:commodity-fetishism"]["terms"]["de"]["preferred"] == "Warenfetischismus"
    assert "Warenfetisch" not in entries["marxist-political-economy-en-zh:commodity-fetishism"]["terms"]["de"].get("variants", [])
    assert "Arbeiterbewegungsmarxismus" not in {e["terms"].get("de", {}).get("preferred") for e in entries.values()}
    historical_removed = {"subjektive Arbeitswertlehre", "Produktionskostentheorie", "Geld als Selbstzweck", "Scheinausgleich", "theoretisches Feld", "dialektische Darstellung"}
    assert historical_removed.isdisjoint({e["terms"].get("de", {}).get("preferred") for e in entries.values()})


def test_new_concept_provenance_preserves_research_queries_and_scope_notes():
    entries = _entries()
    state = entries["marxism:state-derivation"]
    evidence = state["provenance"]["web_review"]
    assert evidence["queries"]
    assert all(source["url"].startswith("https://") for source in evidence["sources"])
    assert "Formbestimmtheit" in entries["marxism:form-determination"]["note"]
    assert "年轻时期的黑格尔" in entries["marxism:young-hegelians"]["note"]
    assert "群体" in entries["marxism:ricardian-socialists"]["note"]
    assert "经济学学派" in entries["marxism:neoclassical-economics"]["note"]
    assert entries["marxism:form-determination"]["terms"]["zh-Hans"].get("variants", []) == []
    assert entries["marxism:marginalism"]["terms"]["zh-Hans"].get("variants", []) == []
    assert entries["marxism:neo-ricardianism"]["terms"]["zh-Hans"].get("variants", []) == []
    for slug in ("form-determination", "marginalism", "neo-ricardianism", "class-consciousness", "legal-form", "state-form", "class-antagonism"):
        assert "variants" not in entries[f"marxism:{slug}"]["terms"]["zh-Hans"]
    assert entries["marxism:structural-marxism"]["terms"]["zh-Hans"]["variants"] == ["结构主义的马克思主义", "结构马克思主义"]
    assert entries["marxism:analytical-marxism"]["terms"]["zh-Hans"]["variants"] == ["分析的马克思主义"]
    assert entries["marxism:neoclassical-economics"]["terms"]["zh-Hans"]["variants"] == ["新古典主义经济学"]
    school_evidence = {
        "neoclassical-economics": ("chapter_5.1.part1.md", "Neoklassik"),
        "marginalism": ("chapter_4.1.md", "Marginalismus"),
        "young-hegelians": ("chapter_6.1.part1.md", "Junghegelianer"),
        "vulgar-economics": ("chapter_4.1.md", "Vulgärökonomie"),
        "frankfurt-school": ("chapter_4.1.md", "Frankfurter Schule"),
        "neo-ricardianism": ("chapter_7.3.2.3.md", "Neoricardianismus"),
        "physiocracy": ("chapter_5.1.part1.md", "Physiokratie"),
        "ricardian-socialists": ("chapter_5.1.part3.md", "ricardianische Sozialisten"),
    }
    for slug, (source_file, surface_form) in school_evidence.items():
        proof = entries[f"marxism:{slug}"]["provenance"]["source_book"]
        assert proof["book"] == "Die Wissenschaft vom Wert"
        assert proof["file"] == source_file
        assert proof["role"] == "body"
        assert proof["surface_form"] == surface_form


def test_review_report_records_deferred_candidate_and_all_web_research():
    report = json.loads((ROOT / "glossaries/reviews/marxism-de-zh-supplement-2026-11-03.json").read_text(encoding="utf-8"))
    assert len(report["adopted"]) == 20
    assert report["deferred"]["source"] == "Arbeiterbewegungsmarxismus"
    assert len(report["deferred"]["queries"]) > 0
    real_update = next(item for item in report["existing_updates"] if item["concept_id"].endswith(":real-abstraction"))
    assert real_update["before"]["terms"]["zh-Hans"]["preferred"] == "现实抽象"
    assert real_update["after"]["terms"]["zh-Hans"]["preferred"] == "真实抽象"
    assert real_update["after"]["terms"]["zh-Hans"]["variants"] == ["现实抽象"]
    assert report["applied_status"] == {
        "new_concepts_added": 20,
        "existing_concepts_updated": 7,
        "total_concepts_after": 306,
        "all_original_records_preserved": True,
    }
    assert all(item["before"]["terms"] != item["after"]["terms"] for item in report["existing_updates"])
    assert "book_review" not in report and "web_research" not in report
    serialized = json.dumps(report, ensure_ascii=False)
    assert ".work/" not in serialized and "web-a.json" not in serialized


def test_runtime_projection_and_sparse_contexts_use_real_german_chinese_forms(tmp_path):
    glossary_path = ROOT / "glossaries/marxism.yaml"
    native = load_glossary(glossary_path)
    english = project_glossary(native, "English", "Chinese")
    german = project_glossary(native, "German", "Chinese")
    assert len(english["entries"]) == 166
    assert len(german["entries"]) == 147

    expected = {
        "Staatsableitung": "国家衍生",
        "Staatsableitungsdebatte": "国家衍生争论",
        "Rechtsform": "法律形式",
        "Staatsform": "国家形式",
        "Formanalyse": "形式分析",
        "Formbestimmung": "形式规定",
        "prämonetäre Werttheorie": "前货币价值论",
        "Klassenbewusstsein": "阶级意识",
        "Klassenantagonismus": "阶级对抗",
        "Ideologiekritik": "意识形态批判",
        "strukturaler Marxismus": "结构主义马克思主义",
        "analytischer Marxismus": "分析马克思主义",
        "Neoklassik": "新古典经济学",
        "Marginalismus": "边际主义",
        "Junghegelianer": "青年黑格尔派",
        "Vulgärökonomie": "庸俗经济学",
        "Frankfurter Schule": "法兰克福学派",
        "Neoricardianismus": "新李嘉图主义",
        "Physiokratie": "重农主义",
        "Ricardianische Sozialisten": "李嘉图派社会主义者",
    }
    actual = {entry["source"]: entry["target"] for entry in german["entries"] if entry["id"].startswith("marxism:")}
    assert actual == expected

    source_dir = tmp_path / "units"
    source_dir.mkdir()
    (source_dir / "chapter.md").write_text(
        "Arbeitswerttheorie; Arbeitswertlehre; strukturalen Marxismus; analytischen Marxismus; "
        "analytische Marxismus; reale Abstraktion; "
        "Formbestimmungen; Rechtsformen; Staatsformen",
        encoding="utf-8",
    )
    contexts = build_unit_glossary_contexts(
        tmp_path / "output",
        source_dir,
        {"domain_glossary_001": glossary_path},
        source_language="German",
        target_language="Chinese",
    )
    context = json.loads(contexts["chapter.md"].read_text(encoding="utf-8"))
    matches = {
        form: entry["target"]
        for entry in context["entries"]
        for form in [entry["source"], *entry.get("variants", [])]
    }
    assert {form: matches[form] for form in (
        "Arbeitswerttheorie", "Arbeitswertlehre", "strukturalen Marxismus", "analytischen Marxismus",
        "analytische Marxismus", "reale Abstraktion",
        "Formbestimmungen", "Rechtsformen", "Staatsformen",
    )} == {
        "Arbeitswerttheorie": "劳动价值论",
        "Arbeitswertlehre": "劳动价值论",
        "strukturalen Marxismus": "结构主义马克思主义",
        "analytischen Marxismus": "分析马克思主义",
        "analytische Marxismus": "分析马克思主义",
        "reale Abstraktion": "真实抽象",
        "Formbestimmungen": "形式规定",
        "Rechtsformen": "法律形式",
        "Staatsformen": "国家形式",
    }
