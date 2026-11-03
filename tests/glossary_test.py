import hashlib
import json
from pathlib import Path

import pytest

from pdf2epub.glossary import (
    GlossaryError,
    build_metadata_glossary_context,
    build_unit_glossary_contexts,
    discover_glossary_candidates,
    load_selected_glossaries,
    load_glossary,
    _canonical_language,
    normalize_glossary,
    project_glossary,
    validate_translation_context,
)
from pdf2epub.entity_extractor import validate_entities
from pdf2epub.markdown_handoff import prepare_markdown_subagent


def test_v2_concepts_project_by_language_pair_and_target_policy():
    glossary = normalize_glossary({
        "schema_version": 2,
        "metadata": {"id": "concepts", "name": "Concepts", "domain": "philosophy", "version": "2"},
        "entries": [{"id": "c1", "terms": {
            "en": {"preferred": "sublation", "variants": ["sublate"]},
            "de": {"preferred": "Aufhebung"},
            "fr": {"preferred": "dépassement"},
            "zh-Hans": {"preferred": "扬弃", "policy": "fixed"},
        }}],
    })
    projected = project_glossary(glossary, "French", "Chinese")
    assert projected["entries"] == [{"id": "c1", "source": "dépassement", "target": "扬弃", "policy": "fixed"}]
    assert project_glossary(glossary, "German", "French")["entries"][0]["target"] == "dépassement"


def test_v2_skips_missing_pair_and_rejects_duplicate_or_ambiguous_forms():
    base = {"schema_version": 2, "metadata": {"id": "terms", "name": "terms", "domain": "x", "version": "1"}, "entries": [
        {"id": "a", "terms": {"en": {"preferred": "term"}}},
    ]}
    assert project_glossary(normalize_glossary(base), "English", "French")["entries"] == []
    with pytest.raises(GlossaryError, match="duplicate canonical language"):
        normalize_glossary({"schema_version": 2, "metadata": {"id": "x", "name": "x", "domain": "x", "version": "1"}, "entries": [
            {"id": "x", "terms": {"en": {"preferred": "a"}, "English": {"preferred": "b"}}}
        ]})
    collision = normalize_glossary({"schema_version": 2, "metadata": {"id": "x", "name": "x", "domain": "x", "version": "1"}, "entries": [
        {"id": "x", "terms": {"en": {"preferred": "shared"}, "fr": {"preferred": "un"}}},
        {"id": "y", "terms": {"en": {"preferred": "other", "aliases": ["shared"]}, "fr": {"preferred": "deux"}}},
    ]})
    with pytest.raises(GlossaryError, match="ambiguous conflicting source form"):
        project_glossary(collision, "English", "French")


def test_v2_preserves_distinct_chinese_scripts_and_rejects_malformed_values():
    native = normalize_glossary({
        "schema_version": 2,
        "metadata": {"id": "scripts", "name": "scripts", "domain": "test", "version": "1"},
        "entries": [{"id": "concept", "terms": {
            "en-US": {"preferred": "term", "policy": "fixed"},
            "zh-Hans": {"preferred": "简体"},
            "zh-TW": {"preferred": "繁體"},
        }}],
    })
    assert set(native["entries"][0]["terms"]) == {"en", "zh-Hans", "zh-Hant"}
    assert project_glossary(native, "English", "Chinese") ["entries"][0]["target"] == "简体"
    assert project_glossary(native, "English", "zh-TW")["entries"][0]["target"] == "繁體"
    invalid = {"schema_version": 2, "metadata": {"id": "x", "name": "x", "domain": "x", "version": "1"},
               "entries": [{"id": "x", "terms": {"en": {"preferred": "a", "policy": []}}}]}
    with pytest.raises(GlossaryError, match="policy must be fixed or preferred"):
        normalize_glossary(invalid)
    invalid["entries"][0]["terms"]["en"]["policy"] = "preferred"
    invalid["entries"][0]["policy"] = "fixed"
    with pytest.raises(GlossaryError, match="fields outside terms"):
        normalize_glossary(invalid)


def test_language_display_aliases_and_unknown_spaced_keys():
    assert _canonical_language("Simplified Chinese") == "zh-Hans"
    with pytest.raises(GlossaryError, match="invalid language key"):
        _canonical_language("made up")


def test_unit_context_hash_records_language_pair_even_when_terms_are_identical(tmp_path: Path):
    glossary_path = tmp_path / "terms.yaml"
    import yaml
    glossary_path.write_text(yaml.safe_dump({
        "schema_version": 2,
        "metadata": {"id": "terms", "name": "terms", "domain": "test", "version": "1"},
        "entries": [{"id": "art", "terms": {
            "en": {"preferred": "art"}, "fr": {"preferred": "art"},
            "zh-Hans": {"preferred": "艺术"},
        }}],
    }, allow_unicode=True), encoding="utf-8")
    output = tmp_path / "output"
    bundle = load_selected_glossaries(
        {"translation": {"glossaries": [str(glossary_path)]}},
        output,
        "English",
        "Chinese",
    )
    source_dir = tmp_path / "source"
    source_dir.mkdir()
    (source_dir / "unit.md").write_text("art", encoding="utf-8")
    en_context = build_unit_glossary_contexts(
        output, source_dir, bundle.context_files,
        source_language="English", target_language="Chinese",
    )["unit.md"].read_bytes()
    fr_context = build_unit_glossary_contexts(
        output, source_dir, bundle.context_files,
        source_language="French", target_language="Chinese",
    )["unit.md"].read_bytes()
    assert en_context != fr_context
def _write_glossary(path: Path, entries=None, name="domain", source_code="de"):
    entries = entries or [{"source": "Aufhebung", "variants": ["aufheben"], "target": "扬弃", "policy": "fixed"}]
    native = {"schema_version": 2, "metadata": {"id": name, "name": name, "domain": "test domain", "version": "1"}, "entries": []}
    for index, entry in enumerate(entries):
        src = {"preferred": entry["source"], **({"variants": entry["variants"]} if entry.get("variants") else {})}
        native["entries"].append({"id": str(index + 1), "terms": {source_code: src, "zh-Hans": {"preferred": entry["target"], "policy": entry.get("policy", "preferred")}}})
    import yaml
    path.write_text(yaml.safe_dump(native, allow_unicode=True, sort_keys=False), encoding="utf-8")


def test_load_glossary_normalizes_yaml(tmp_path: Path):
    source = tmp_path / "terms.yaml"
    _write_glossary(source)

    glossary = load_glossary(source)

    assert glossary["metadata"]["name"] == "domain"
    projected = project_glossary(glossary, "German", "Chinese")
    assert projected["entries"][0]["target"] == "扬弃"
    assert projected["entries"][0]["variants"] == ["aufheben"]


def test_external_schema1_glossaries_are_rejected(tmp_path: Path):
    source = tmp_path / "old.yaml"
    source.write_text("schema_version: 1\nmetadata: {name: old}\nentries: []\n", encoding="utf-8")
    with pytest.raises(GlossaryError, match="schema_version must be 2"):
        load_glossary(source)


def test_selected_glossary_is_snapshotted_and_hashable(tmp_path: Path):
    source = tmp_path / "terms.yaml"
    output = tmp_path / "output"
    _write_glossary(source, name="german-classical-philosophy")

    bundle = load_selected_glossaries(
        {"translation": {"glossaries": [{"path": str(source)}]}},
        output,
        "German",
        "Chinese",
    )

    snapshot = bundle.context_files["domain_glossary_001"]
    assert snapshot.is_relative_to(output)
    assert snapshot.suffix == ".json"
    assert json.loads(snapshot.read_text(encoding="utf-8"))["schema_version"] == 2
    assert bundle.entries == 1
    assert bundle.context_sha256["domain_glossary_001"] == hashlib.sha256(
        snapshot.read_bytes()
    ).hexdigest()


def test_glossary_candidates_report_language_and_metadata_eligibility(tmp_path: Path):
    glossary_dir = tmp_path / "glossaries"
    glossary_dir.mkdir()
    valid = glossary_dir / "valid.yaml"
    _write_glossary(valid, name="valid")
    invalid = glossary_dir / "wrong-language.yaml"
    _write_glossary(invalid, name="wrong-language")
    invalid_data = load_glossary(invalid)
    invalid_data["entries"][0]["terms"].pop("de")
    import yaml
    invalid.write_text(yaml.safe_dump(invalid_data, allow_unicode=True), encoding="utf-8")

    candidates = discover_glossary_candidates(glossary_dir, "German", "Chinese")

    by_name = {item.get("name"): item for item in candidates}
    assert by_name["valid"]["eligible"] is True
    assert by_name["wrong-language"]["eligible"] is False
    assert any("no concepts supporting" in error for error in by_name["wrong-language"]["errors"])


def test_glossary_candidates_mark_cross_language_reference_candidate(tmp_path: Path):
    glossary_dir = tmp_path / "glossaries"
    glossary_dir.mkdir()
    source = glossary_dir / "german.yaml"
    _write_glossary(source, name="german-classical-philosophy")
    import yaml
    glossary = load_glossary(source)
    source.write_text(yaml.safe_dump(glossary, allow_unicode=True), encoding="utf-8")

    candidates = discover_glossary_candidates(glossary_dir, "English", "Chinese")

    candidate = candidates[0]
    assert candidate["eligible"] is False
    assert candidate["reference_eligible"] is False
    assert candidate["reference_errors"]


def test_selected_glossary_writes_provenance_record(tmp_path: Path):
    source = tmp_path / "terms.yaml"
    output = tmp_path / "output"
    _write_glossary(source, name="domain")

    load_selected_glossaries(
        {"translation": {"glossaries": [{"path": str(source), "id": "domain"}]}},
        output,
        "German",
        "Chinese",
    )

    record = json.loads((output / "glossary_selection.json").read_text(encoding="utf-8"))
    assert record["mode"] == "explicit"
    assert record["selected"][0]["source_sha256"] == hashlib.sha256(
        source.read_bytes()
    ).hexdigest()
    assert record["selected"][0]["snapshot_sha256"]


def test_reference_glossary_requires_a_supported_requested_pair(tmp_path: Path):
    source = tmp_path / "german-classical-philosophy.yaml"
    output = tmp_path / "output"
    _write_glossary(source, name="german-classical-philosophy")
    original_hash = hashlib.sha256(source.read_bytes()).hexdigest()

    bundle = load_selected_glossaries(
        {
            "translation": {
                "reference_glossaries": [
                    {"path": str(source), "id": "german-classical-philosophy"}
                ]
            }
        },
        output,
        "German",
        "Chinese",
    )

    reference_path = bundle.context_files["reference_glossary_001"]
    assert reference_path.name == "reference_german-classical-philosophy.json"
    assert hashlib.sha256(source.read_bytes()).hexdigest() == original_hash
    assert any("reference-only" in rule for rule in bundle.rules)
    record = json.loads((output / "glossary_selection.json").read_text(encoding="utf-8"))
    assert record["selected"][0]["kind"] == "reference"


def test_reference_glossary_context_validates_projected_pair(tmp_path: Path):
    source = tmp_path / "german.yaml"
    output = tmp_path / "output"
    _write_glossary(source, name="german")
    bundle = load_selected_glossaries(
        {
            "translation": {
                "reference_glossaries": [str(source)],
                "require_entities": False,
            }
        },
        output,
        "German",
        "Chinese",
    )
    manifest = {
        "context_files": {
            name: path.relative_to(output).as_posix()
            for name, path in bundle.context_files.items()
        },
        "context_sha256": bundle.context_sha256,
        "skipped_context_files": ["translation_entities"],
    }
    manifest_path = output / "translate_subagent_manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    report = validate_translation_context(
        output,
        manifest_path.name,
        {
            "title": "Book",
            "translation": {
                "source_language": "German",
                "target_language": "Chinese",
                "require_entities": False,
                "reference_glossaries": [str(source)],
            },
        },
    )

    assert report["valid"] is True
    assert report["reference_glossaries"] == ["german"]
    assert report["reference_glossary_entries"] == 1


def test_reference_glossary_cross_language_aliases_are_sparse_context(tmp_path: Path):
    source = tmp_path / "german.yaml"
    source.write_text(
        "schema_version: 2\n"
        "metadata:\n"
        "  id: german\n"
        "  name: german\n"
        "  domain: German classical philosophy\n"
        "  version: '1'\n"
        "entries:\n"
        "  - id: aufhebung\n"
        "    terms:\n"
        "      en: {preferred: sublation, variants: [sublate]}\n"
        "      de: {preferred: Aufhebung}\n"
        "      zh-Hans: {preferred: 扬弃}\n",
        encoding="utf-8",
    )
    output = tmp_path / "output"
    bundle = load_selected_glossaries(
        {"translation": {"reference_glossaries": [str(source)]}},
        output,
        "English",
        "Chinese",
    )
    source_dir = tmp_path / "source"
    source_dir.mkdir()
    (source_dir / "unit.md").write_text(
        "The sublation of the opposition is decisive.\n", encoding="utf-8"
    )

    contexts = build_unit_glossary_contexts(
        output,
        source_dir,
        bundle.context_files,
        source_language="English",
        target_language="Chinese",
    )
    context = json.loads(contexts["unit.md"].read_text(encoding="utf-8"))
    assert context["entries"][0]["kind"] == "reference"
    assert context["entries"][0]["source"] == "sublation"
    assert context["entries"][0]["kind"] == "reference"
    hits = json.loads(
        (output / "translation_glossaries" / "keyword_hits" / "unit.json").read_text(
            encoding="utf-8"
        )
    )["hits"]
    assert hits[0]["match_types"] == ["source"]


def test_unit_glossary_context_contains_only_matching_entries(tmp_path: Path):
    source = tmp_path / "terms.yaml"
    output = tmp_path / "output"
    _write_glossary(
        source,
        name="domain",
        entries=[
            {"source": "Aufhebung", "target": "扬弃", "policy": "fixed"},
            {"source": "Dasein", "target": "定在", "policy": "fixed"},
        ],
    )
    bundle = load_selected_glossaries(
        {"translation": {"glossaries": [str(source)]}},
        output,
        "German",
        "Chinese",
    )
    source_dir = tmp_path / "source"
    source_dir.mkdir()
    (source_dir / "unit.md").write_text("Das Dasein ist bestimmt.\n", encoding="utf-8")

    contexts = build_unit_glossary_contexts(
        output, source_dir, bundle.context_files, source_language="German"
    )
    context = json.loads(contexts["unit.md"].read_text(encoding="utf-8"))
    assert [entry["source"] for entry in context["entries"]] == ["Dasein"]


def test_unit_glossary_context_is_compact_and_omits_empty_entity_notes(tmp_path: Path):
    output = tmp_path / "output"
    source_dir = tmp_path / "source"
    source_dir.mkdir()
    (source_dir / "unit.md").write_text("Hegel appears here.\n", encoding="utf-8")
    entity_path = output / "translation_entities.json"
    entity_path.parent.mkdir(parents=True)
    entity_path.write_text(
        json.dumps(
            {
                "characters": [{"original": "Hegel", "suggested_translation": "黑格尔"}],
                "places": [],
                "organizations": [],
                "terms": [],
                "races": [],
                "items": [],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    contexts = build_unit_glossary_contexts(output, source_dir, {}, entity_path)

    raw = contexts["unit.md"].read_text(encoding="utf-8")
    assert raw.count("\n") == 1
    context = json.loads(raw)
    assert context["entries"] == [
        {
            "kind": "book_entity",
            "category": "characters",
            "original": "Hegel",
            "target": "黑格尔",
        }
    ]


def test_unit_glossary_matching_normalizes_unicode_allows_short_terms_and_writes_audit(
    tmp_path: Path,
):
    output = tmp_path / "output"
    source_dir = tmp_path / "source"
    source_dir.mkdir()
    (source_dir / "unit.md").write_text(
        "AI appears with Hegel’s name and machine\nlearning.\n"
        "`secret` https://example.test/secret\n",
        encoding="utf-8",
    )
    glossary = tmp_path / "terms.yaml"
    glossary.write_text(
        "schema_version: 2\n"
        "metadata:\n"
        "  id: test\n"
        "  name: test\n"
        "  domain: test\n"
        "  version: '1'\n"
        "entries:\n"
        "  - {id: ai, terms: {en: {preferred: AI, allow_short: true}, zh-Hans: {preferred: 人工智能, policy: fixed}}}\n"
        "  - {id: hegel-name, terms: {en: {preferred: Hegel's name}, zh-Hans: {preferred: 黑格尔之名}}}\n"
        "  - {id: machine-learning, terms: {en: {preferred: machine learning}, zh-Hans: {preferred: 机器学习}}}\n"
        "  - {id: learning, terms: {en: {preferred: learning}, zh-Hans: {preferred: 学习}}}\n"
        "  - {id: secret, terms: {en: {preferred: secret}, zh-Hans: {preferred: 秘密}}}\n",
        encoding="utf-8",
    )
    bundle = load_selected_glossaries(
        {"translation": {"glossaries": [str(glossary)]}},
        output,
        "English",
        "Chinese",
    )

    contexts = build_unit_glossary_contexts(output, source_dir, bundle.context_files)
    context = json.loads(contexts["unit.md"].read_text(encoding="utf-8"))
    assert [entry["source"] for entry in context["entries"]] == [
        "AI",
        "machine learning",
        "Hegel's name",
    ]
    report = json.loads(
        (output / "translation_glossaries" / "keyword_hits" / "unit.json").read_text(
            encoding="utf-8"
        )
    )
    assert {hit["source"] for hit in report["hits"]} == {
        "AI",
        "machine learning",
        "Hegel's name",
    }
    assert "secret" not in {hit["source"] for hit in report["hits"]}
    suppressed = {item["source"]: item for item in report["suppressed_hits"]}
    assert suppressed["learning"]["winner"] == "machine learning"


def test_entity_aliases_and_variants_are_used_for_unit_matching(tmp_path: Path):
    output = tmp_path / "output"
    source_dir = tmp_path / "source"
    source_dir.mkdir()
    (source_dir / "unit.md").write_text("Georg Hegel and Hegelian ideas.\n", encoding="utf-8")
    entity_path = output / "translation_entities.json"
    entity_path.parent.mkdir(parents=True)
    entity_path.write_text(
        json.dumps(
            {
                "metadata": {"book_title": "Book", "extraction_complete": True},
                "characters": [
                    {
                        "original": "Hegel",
                        "aliases": ["Georg Hegel"],
                        "variants": ["Hegelian"],
                        "suggested_translation": "黑格尔",
                    }
                ],
                "places": [],
                "organizations": [],
                "terms": [],
                "races": [],
                "items": [],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    contexts = build_unit_glossary_contexts(output, source_dir, {}, entity_path)
    context = json.loads(contexts["unit.md"].read_text(encoding="utf-8"))
    assert context["entries"] == [
        {
            "kind": "book_entity",
            "category": "characters",
            "original": "Hegel",
            "target": "黑格尔",
            "variants": ["Hegelian"],
            "aliases": ["Georg Hegel"],
        }
    ]


def test_metadata_glossary_context_selects_only_matching_entries(tmp_path: Path):
    output = tmp_path / "output"
    glossary = tmp_path / "terms.yaml"
    _write_glossary(
        glossary,
        name="domain",
        entries=[
            {"source": "Aufhebung", "target": "扬弃", "policy": "fixed"},
            {"source": "Dasein", "target": "定在", "policy": "fixed"},
        ],
    )
    bundle = load_selected_glossaries(
        {"translation": {"glossaries": [str(glossary)]}},
        output,
        "German",
        "Chinese",
    )

    context_path = build_metadata_glossary_context(
        output,
        bundle.context_files,
        "Aufhebung in the title",
        source_language="German",
        target_language="Chinese",
    )

    assert context_path is not None
    context = json.loads(context_path.read_text(encoding="utf-8"))
    assert [entry["source"] for entry in context["entries"]] == ["Aufhebung"]


def test_entity_validation_rejects_conflicting_duplicate_originals():
    data = {
        "schema_version": 1,
        "metadata": {
            "book_title": "Book",
            "source_language": "German",
            "target_language": "Chinese",
            "extraction_complete": True,
        },
        "characters": [{"original": "Hegel", "suggested_translation": "黑格尔"}],
        "places": [{"original": "Hegel", "suggested_translation": "黑格尔（另一译法）"}],
        "organizations": [],
        "terms": [],
        "races": [],
        "items": [],
    }

    errors = validate_entities(data, "Book", "German", "Chinese")

    assert any("conflicts with characters[0]" in error for error in errors)


def test_selected_glossaries_reject_conflicting_source_forms(tmp_path: Path):
    first = tmp_path / "first.yaml"
    second = tmp_path / "second.yaml"
    _write_glossary(first, name="first")
    _write_glossary(
        second,
        name="second",
        entries=[{"source": "Aufhebung", "target": "保留", "policy": "fixed"}],
    )

    with pytest.raises(GlossaryError, match="conflict"):
        load_selected_glossaries(
            {"translation": {"glossaries": [str(first), str(second)]}},
            tmp_path / "output",
            "German",
            "Chinese",
        )


def test_selected_reference_glossaries_reject_pair_conflicts(tmp_path: Path):
    first, second = tmp_path / "first.yaml", tmp_path / "second.yaml"
    _write_glossary(first, name="first")
    _write_glossary(second, name="second", entries=[
        {"source": "Aufhebung", "target": "保留", "policy": "fixed"}
    ])
    with pytest.raises(GlossaryError, match="selected reference glossaries conflict"):
        load_selected_glossaries(
            {"translation": {"reference_glossaries": [str(first), str(second)]}},
            tmp_path / "output",
            "German",
            "Chinese",
        )


def test_translation_context_validates_snapshot_hash(tmp_path: Path):
    source = tmp_path / "terms.yaml"
    output = tmp_path / "output"
    _write_glossary(source)
    bundle = load_selected_glossaries(
        {"translation": {"glossaries": [str(source),], "require_entities": False}},
        output,
        "German",
        "Chinese",
    )
    manifest = {
        "context_files": {
            name: str(path.relative_to(output)).replace("\\", "/")
            for name, path in bundle.context_files.items()
        },
        "context_sha256": bundle.context_sha256,
        "skipped_context_files": ["translation_entities"],
    }
    (output / "translate-html_subagent_manifest.json").write_text(
        json.dumps(manifest), encoding="utf-8"
    )

    report = validate_translation_context(
        output,
        "translate-html_subagent_manifest.json",
        {"title": "Book", "translation": {"source_language": "German", "target_language": "Chinese", "require_entities": False, "glossaries": [str(source)]}},
    )

    assert report["valid"] is True
    assert report["glossary_entries"] == 1


def test_translation_context_uses_manifest_pair_over_config(tmp_path: Path):
    source = tmp_path / "terms.yaml"
    import yaml
    source.write_text(yaml.safe_dump({
        "schema_version": 2,
        "metadata": {"id": "pair", "name": "pair", "domain": "test", "version": "1"},
        "entries": [{"id": "art", "terms": {
            "fr": {"preferred": "art"}, "de": {"preferred": "Kunst"},
        }}],
    }, allow_unicode=True), encoding="utf-8")
    output = tmp_path / "output"
    bundle = load_selected_glossaries(
        {"translation": {"glossaries": [str(source)]}}, output, "French", "German"
    )
    manifest = {
        "source_language": "French",
        "target_language": "German",
        "context_files": {name: path.relative_to(output).as_posix() for name, path in bundle.context_files.items()},
        "context_sha256": bundle.context_sha256,
        "skipped_context_files": ["translation_entities"],
    }
    path = output / "translate_subagent_manifest.json"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    report = validate_translation_context(output, path.name, {
        "title": "Book",
        "translation": {
            "source_language": "English",
            "target_language": "Chinese",
            "require_entities": False,
            "glossaries": [str(source)],
        },
    })
    assert report["valid"] is True


def test_context_change_invalidates_resume_checkpoint(tmp_path: Path):
    source_dir = tmp_path / "source"
    target_dir = tmp_path / "target"
    source_dir.mkdir()
    target_dir.mkdir()
    (source_dir / "unit.md").write_text("source", encoding="utf-8")
    (target_dir / "unit.md").write_text("complete", encoding="utf-8")
    old_context = tmp_path / "context.json"
    old_context.write_text('{"version": 1}', encoding="utf-8")
    (tmp_path / "translate_subagent_manifest.json").write_text(
        json.dumps(
            {
                "context_sha256": {
                    "domain_glossary_001": hashlib.sha256(old_context.read_bytes()).hexdigest()
                }
            }
        ),
        encoding="utf-8",
    )
    (tmp_path / "translate_validation.json").write_text(
        json.dumps(
            {
                "valid_files": ["unit.md"],
                "source_sha256": {
                    "unit.md": hashlib.sha256(b"source").hexdigest()
                },
            }
        ),
        encoding="utf-8",
    )
    old_context.write_text('{"version": 2}', encoding="utf-8")

    paths = prepare_markdown_subagent(
        tmp_path,
        "translate",
        source_dir,
        target_dir,
        "German",
        "Chinese",
        resume=True,
        context_files={"domain_glossary_001": old_context},
    )
    manifest = json.loads(paths["manifest"].read_text(encoding="utf-8"))

    assert manifest["completed_files"] == []
    assert manifest["pending_files"] == ["unit.md"]
