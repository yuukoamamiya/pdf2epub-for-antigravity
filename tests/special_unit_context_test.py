import hashlib
import json
from pathlib import Path

import pytest

from pdf2epub.special_unit_context import (
    SpecialUnitContextError,
    build_special_unit_context,
    term_candidates,
)


def _prepared(tmp_path: Path):
    source_dir = tmp_path / "polished_markdown"
    source_dir.mkdir()
    source = source_dir / "index.md"
    source.write_text("Aufhebung; sublation", encoding="utf-8")
    unit_dir = tmp_path / "translation_glossaries" / "unit_contexts"
    unit_dir.mkdir(parents=True)
    context_path = unit_dir / "index.json"
    context_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "source_file": "index.md",
                "entries": [
                    {"kind": "domain", "source": "Aufhebung", "target": "扬弃", "policy": "fixed"},
                    {"kind": "domain", "source": "Aufhebung", "target": "扬弃（偏好）", "policy": "preferred"},
                    {"kind": "book_entity", "original": "sub", "suggested_translation": "子"},
                    {"kind": "reference", "source": "sublation", "target": "扬弃概念"},
                ],
            },
            ensure_ascii=False,
            separators=(",", ":"),
        ),
        encoding="utf-8",
    )
    context_hash = hashlib.sha256(context_path.read_bytes()).hexdigest()
    manifest = {
        "task": "translate",
        "workflow": "antigravity-subagent",
        "files": ["index.md"],
        "source_dir": "polished_markdown",
        "source_sha256": {"index.md": hashlib.sha256(source.read_bytes()).hexdigest()},
        "unit_context_files": {"index.md": "translation_glossaries/unit_contexts/index.json"},
        "unit_context_sha256": {"index.md": context_hash},
        "toc_heading_contexts": {
            "index.md": {"toc_title": "Index", "binding_mode": "visible", "children": []}
        },
    }
    (tmp_path / "translate_subagent_manifest.json").write_text(
        json.dumps(manifest), encoding="utf-8"
    )
    return source, context_path


def test_build_context_requires_prepare_manifest(tmp_path: Path):
    with pytest.raises(SpecialUnitContextError, match="translate prepare"):
        build_special_unit_context(
            tmp_path, "index.md", "index", source_language="German", target_language="Chinese"
        )


def test_build_context_uses_scoped_context_and_freshness_hash(tmp_path: Path):
    _prepared(tmp_path)
    context = build_special_unit_context(
        tmp_path, "index.md", "index", source_language="German", target_language="Chinese"
    )
    assert context["heading_binding"]["toc_title"] == "Index"
    assert context["language"] == {"source": "German", "target": "Chinese"}
    assert [entry.get("policy") for entry in context["entries"][:2]] == ["fixed", "preferred"]
    assert context["entries"][-1]["kind"] == "reference"
    assert context["contract_sha256"]
    assert context["unit_context_sha256"]


def test_contract_stays_unit_scoped_across_unrelated_parent_and_toc_reprepare(tmp_path: Path):
    _prepared(tmp_path)
    entity_path = tmp_path / "translation_entities.json"
    entity_path.write_text('{"revision":1}', encoding="utf-8")
    manifest_path = tmp_path / "translate_subagent_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["context_files"] = {"translation_entities": "translation_entities.json"}
    manifest["context_sha256"] = {
        "translation_entities": hashlib.sha256(entity_path.read_bytes()).hexdigest()
    }
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    toc_path = tmp_path / "toc_tree.json"
    toc_path.write_text('{"unrelated":1}', encoding="utf-8")
    first = build_special_unit_context(
        tmp_path, "index.md", "index", source_language="German", target_language="Chinese"
    )

    # A new translate prepare records updated full-book snapshots while this
    # unit's own projection and precise heading binding stay unchanged.
    entity_path.write_text('{"revision":2}', encoding="utf-8")
    manifest["context_sha256"]["translation_entities"] = hashlib.sha256(
        entity_path.read_bytes()
    ).hexdigest()
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    toc_path.write_text('{"unrelated":2}', encoding="utf-8")
    second = build_special_unit_context(
        tmp_path, "index.md", "index", source_language="German", target_language="Chinese"
    )

    assert first["parent_context_sha256"] != second["parent_context_sha256"]
    assert first["toc_sha256"] != second["toc_sha256"]
    assert first["contract_sha256"] == second["contract_sha256"]


def test_contract_changes_with_this_units_heading_or_projection(tmp_path: Path):
    _, context_path = _prepared(tmp_path)
    manifest_path = tmp_path / "translate_subagent_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    baseline = build_special_unit_context(
        tmp_path, "index.md", "index", source_language="German", target_language="Chinese"
    )

    manifest["toc_heading_contexts"]["index.md"]["toc_title"] = "Changed Index"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    changed_heading = build_special_unit_context(
        tmp_path, "index.md", "index", source_language="German", target_language="Chinese"
    )
    assert changed_heading["contract_sha256"] != baseline["contract_sha256"]

    manifest["toc_heading_contexts"]["index.md"]["toc_title"] = "Index"
    context_data = json.loads(context_path.read_text(encoding="utf-8"))
    context_data["entries"].append(
        {"kind": "domain", "source": "neue", "target": "新", "policy": "preferred"}
    )
    context_path.write_text(json.dumps(context_data, ensure_ascii=False), encoding="utf-8")
    manifest["unit_context_sha256"]["index.md"] = hashlib.sha256(context_path.read_bytes()).hexdigest()
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    changed_projection = build_special_unit_context(
        tmp_path, "index.md", "index", source_language="German", target_language="Chinese"
    )
    assert changed_projection["contract_sha256"] != baseline["contract_sha256"]


def test_empty_glossary_projection_survives_translate_prepare_and_special_prepare(tmp_path: Path):
    from pdf2epub.glossary import build_unit_glossary_contexts
    from pdf2epub.markdown_handoff import prepare_markdown_subagent
    from pdf2epub.special_unit_translation import prepare

    output = tmp_path / "output"
    source_dir = output / "polished_markdown" / "validated"
    source_dir.mkdir(parents=True)
    source = source_dir / "index.md"
    source.write_text("# Index\n\nAlpha, 12\n", encoding="utf-8")
    sparse = build_unit_glossary_contexts(output, source_dir, {}, None, source_language="English")
    assert sparse == {}
    handoff = prepare_markdown_subagent(
        output,
        "translate",
        source_dir,
        output / "translated",
        "English",
        "Chinese",
        config={},
        file_roles={"index.md": "index"},
        heading_contexts={
            "index.md": {"toc_title": "索引", "binding_mode": "visible", "children": []}
        },
        unit_context_files=sparse,
        controlled_files={"index.md": "index"},
    )
    context = build_special_unit_context(
        output, "index.md", "index", source_language="English", target_language="Chinese"
    )
    assert context["unit_context_state"] == "empty"
    assert context["entries"] == []
    assert context["unit_context_sha256"]
    manifest = prepare("index", source, output, output / "translated", context=context)
    assert manifest["context_contract_sha256"] == context["contract_sha256"]
    assert handoff["manifest"].is_file()


def test_term_candidates_are_suggestions_and_respect_authority(tmp_path: Path):
    _prepared(tmp_path)
    context = build_special_unit_context(
        tmp_path, "index.md", "index", source_language="German", target_language="Chinese"
    )
    candidates = term_candidates("Aufhebung", context)
    assert len(candidates) == 2
    assert all(candidate["match_type"] == "exact_source" for candidate in candidates)
    assert candidates[0]["kind"] == "domain"
    assert candidates[0]["policy"] == "fixed"
    assert candidates[0]["ambiguous"] is False
    assert candidates[1]["ambiguous"] is False
    partial = term_candidates("subl", context)
    assert partial
    assert all(candidate["match_type"] == "partial" for candidate in partial)
    conflict = term_candidates(
        "Aufhebung",
        {
            "entries": [
                {"kind": "domain", "source": "Aufhebung", "target": "甲", "policy": "fixed"},
                {"kind": "domain", "source": "Aufhebung", "target": "乙", "policy": "fixed"},
                {"kind": "domain", "source": "Aufhebung", "target": "丙", "policy": "preferred"},
                {"kind": "reference", "source": "Aufhebung", "target": "丁"},
            ]
        },
    )
    assert [item["ambiguous"] for item in conflict] == [True, True, False, False]


def test_term_candidates_use_word_boundaries_and_label_exact_aliases():
    context = {
        "language": {"source": "English", "target": "Chinese"},
        "entries": [
            {"kind": "domain", "source": "art", "target": "艺术", "policy": "fixed"},
            {"kind": "domain", "source": "term", "target": "术语", "policy": "preferred", "aliases": ["concept"]},
            {"kind": "reference", "source": "Begriff", "target": "概念", "aliases_by_language": {"English": ["concept"]}},
        ],
    }
    assert term_candidates("art", context)[0]["match_type"] == "exact_source"
    assert term_candidates("cart", context) == []
    alias = term_candidates("concept", context)
    assert {item["match_type"] for item in alias} == {"exact_alias", "exact_cross_language_alias"}
    assert not any(item["ambiguous"] for item in alias)

    list_source = term_candidates(
        "sublatio",
        {"entries": [{"kind": "domain", "source": ["Aufhebung", "sublatio"], "target": "扬弃", "policy": "fixed"}]},
    )
    assert list_source[0]["match_type"] == "exact_source"


def test_term_candidates_apply_authority_before_canonical_form():
    alias_wins = term_candidates(
        "Alpha",
        {"entries": [
            {"kind": "book_entity", "source": "Alpha", "target": "书译"},
            {"kind": "domain", "source": "Other", "aliases": ["Alpha"], "target": "固定译", "policy": "fixed"},
        ]},
    )
    assert [item["ambiguous"] for item in alias_wins] == [False, False]
    assert alias_wins[0]["target"] == "书译"
    assert alias_wins[1]["target"] == "固定译"

    fixed_conflict = term_candidates(
        "Alpha",
        {"entries": [
            {"kind": "book_entity", "source": "Alpha", "target": "书译"},
            {"kind": "domain", "source": "One", "aliases": ["Alpha"], "target": "固定译一", "policy": "fixed"},
            {"kind": "domain", "source": "Two", "aliases": ["Alpha"], "target": "固定译二", "policy": "fixed"},
        ]},
    )
    assert [item["ambiguous"] for item in fixed_conflict] == [False, True, True]


def test_build_context_rejects_malformed_entry_and_manifest_kind(tmp_path: Path):
    _prepared(tmp_path)
    context_path = tmp_path / "translation_glossaries" / "unit_contexts" / "index.json"
    context_path.write_text(json.dumps({"entries": [{"source": "", "target": "扬弃"}]}), encoding="utf-8")
    manifest = json.loads((tmp_path / "translate_subagent_manifest.json").read_text(encoding="utf-8"))
    manifest["unit_context_sha256"]["index.md"] = hashlib.sha256(context_path.read_bytes()).hexdigest()
    (tmp_path / "translate_subagent_manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(SpecialUnitContextError, match=r"entries\[0\].source"):
        build_special_unit_context(
            tmp_path, "index.md", "index", source_language="German", target_language="Chinese"
        )
    manifest["task"] = "polish"
    (tmp_path / "translate_subagent_manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(SpecialUnitContextError, match="not a translate handoff"):
        build_special_unit_context(
            tmp_path, "index.md", "index", source_language="German", target_language="Chinese"
        )


def test_build_context_rejects_parent_snapshot_and_unsafe_paths(tmp_path: Path):
    source, _ = _prepared(tmp_path)
    manifest_path = tmp_path / "translate_subagent_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["context_files"] = {"translation_entities": "translation_entities.json"}
    manifest["context_sha256"] = {"translation_entities": "stale"}
    (tmp_path / "translation_entities.json").write_text("{}", encoding="utf-8")
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(SpecialUnitContextError, match="parent context changed"):
        build_special_unit_context(
            tmp_path, "index.md", "index", source_language="German", target_language="Chinese"
        )

    manifest["context_files"] = {}
    manifest["context_sha256"] = {}
    manifest["source_dir"] = "../outside"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(SpecialUnitContextError, match="escapes output"):
        build_special_unit_context(
            tmp_path, "index.md", "index", source_language="German", target_language="Chinese"
        )


def test_build_context_rejects_changed_current_heading_binding(tmp_path: Path, monkeypatch):
    _prepared(tmp_path)
    (tmp_path / "toc_tree_translated.json").write_text("{}", encoding="utf-8")
    tree_dir = tmp_path / "ocr_markdown"
    tree_dir.mkdir()
    (tree_dir / "tree_progress.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(
        "pdf2epub.special_unit_context.build_toc_heading_contexts",
        lambda *_args, **_kwargs: {
            "index.md": {"toc_title": "Changed Index", "binding_mode": "visible", "children": []}
        },
    )
    with pytest.raises(SpecialUnitContextError, match="TOC heading context changed"):
        build_special_unit_context(
            tmp_path, "index.md", "index", source_language="German", target_language="Chinese"
        )


def test_build_context_rejects_missing_current_heading_binding(tmp_path: Path, monkeypatch):
    _prepared(tmp_path)
    (tmp_path / "toc_tree_translated.json").write_text("{}", encoding="utf-8")
    tree_dir = tmp_path / "ocr_markdown"
    tree_dir.mkdir()
    (tree_dir / "tree_progress.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(
        "pdf2epub.special_unit_context.build_toc_heading_contexts",
        lambda *_args, **_kwargs: {},
    )
    with pytest.raises(SpecialUnitContextError, match="TOC heading context changed"):
        build_special_unit_context(
            tmp_path, "index.md", "index", source_language="German", target_language="Chinese"
        )


def test_build_context_rejects_half_present_empty_context_mapping(tmp_path: Path):
    _prepared(tmp_path)
    manifest_path = tmp_path / "translate_subagent_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["unit_context_sha256"] = {}
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(SpecialUnitContextError, match="incomplete sparse context mapping"):
        build_special_unit_context(
            tmp_path, "index.md", "index", source_language="German", target_language="Chinese"
        )


@pytest.mark.parametrize("filename", ["../index.md", "sub/index.md", "index.txt"])
def test_build_context_rejects_non_basename_filename(tmp_path: Path, filename: str):
    with pytest.raises(SpecialUnitContextError, match="basename"):
        build_special_unit_context(
            tmp_path, filename, "index", source_language="German", target_language="Chinese"
        )


@pytest.mark.parametrize("stale", ["source", "context"])
def test_build_context_rejects_stale_source_or_projection(tmp_path: Path, stale: str):
    source, context_path = _prepared(tmp_path)
    target = source if stale == "source" else context_path
    target.write_text(target.read_text(encoding="utf-8") + " changed", encoding="utf-8")
    with pytest.raises(SpecialUnitContextError, match="changed after translate prepare"):
        build_special_unit_context(
            tmp_path, "index.md", "index", source_language="German", target_language="Chinese"
        )
