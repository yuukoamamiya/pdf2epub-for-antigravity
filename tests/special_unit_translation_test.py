import json

import pytest
from types import SimpleNamespace

from pdf2epub.special_unit_translation import apply, parse_unit, prepare


def context_for(kind, source):
    import hashlib
    return {"kind": kind, "source_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
            "contract_sha256": "context-contract", "entries": []}


def test_bibliography_prepare_apply_preserves_entries_exactly(tmp_path):
    source_dir = tmp_path / "polished_markdown" / "validated"
    source_dir.mkdir(parents=True)
    source = source_dir / "refs.md"
    body = "# References\n\n- Smith. 2020. DOI: 10.1/example\n\n- Doe. 2021. Title\n"
    source.write_text(body, encoding="utf-8")
    output = tmp_path
    target_dir = tmp_path / "translated"
    context = context_for("bibliography", source)
    prepare("bibliography", source, output, target_dir, context=context)
    result_path = output / json.loads((output / "special_unit_bibliography_refs_manifest.json").read_text(encoding="utf-8"))["result_file"]
    result = json.loads(result_path.read_text(encoding="utf-8"))
    result["heading_translation"] = "参考文献"
    result_path.write_text(json.dumps(result), encoding="utf-8")
    target = apply("bibliography", source, output, target_dir, context=context)
    assert target.read_text(encoding="utf-8") == body.replace("# References", "# 参考文献")


def test_index_maps_terms_and_preserves_page_numbers_and_hierarchy(tmp_path):
    source = tmp_path / "index.md"
    source.write_text("# Index\n\nAlpha, 12\n  Beta, 14–16\n", encoding="utf-8")
    output = tmp_path
    target_dir = tmp_path / "translated"
    context = context_for("index", source)
    prepare("index", source, output, target_dir, context=context)
    result_path = output / json.loads((output / "special_unit_index_index_manifest.json").read_text(encoding="utf-8"))["result_file"]
    result = json.loads(result_path.read_text(encoding="utf-8"))
    result["heading_translation"] = "索引"
    result["translations"][0]["term"] = "阿尔法"
    result["translations"][1]["term"] = "贝塔"
    result_path.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
    target = apply("index", source, output, target_dir, context=context)
    assert target.read_text(encoding="utf-8") == "# 索引\n\n阿尔法, 12\n  贝塔, 14–16\n"


def test_strict_parsers_fallback_on_mixed_or_ambiguous_shapes():
    assert parse_unit("bibliography", "# References\nAn explanatory sentence.\n- Smith 2020\n") is None
    assert parse_unit("index", "A see B 12\n") is None
    assert parse_unit("index", "A, 12 see B\n") is None


def test_apply_rejects_stale_source_and_incomplete_ids(tmp_path):
    source = tmp_path / "refs.md"
    source.write_text("# References\n- Smith. 2020\n", encoding="utf-8")
    out, target = tmp_path, tmp_path / "translated"
    context = context_for("bibliography", source)
    prepare("bibliography", source, out, target, context=context)
    result_path = out / json.loads((out / "special_unit_bibliography_refs_manifest.json").read_text(encoding="utf-8"))["result_file"]
    result = json.loads(result_path.read_text(encoding="utf-8"))
    result["heading_translation"] = "参考文献"
    result["entry_confirmations"] = []
    result_path.write_text(json.dumps(result), encoding="utf-8")
    with pytest.raises(ValueError, match="IDs/order"):
        apply("bibliography", source, out, target, context=context)


@pytest.mark.parametrize("kind,source_text,result_update,expected", [
    ("bibliography", "## References\r\n\r\n- Smith 2020\r\n", {"heading_translation": "参考文献"}, "## 参考文献\r\n\r\n- Smith 2020\r\n"),
    ("index", "# Index\r\n\r\nAlpha, 12\r\n  Beta, 14-16", {"heading_translation": "索引", "terms": ["阿尔法", "贝塔"]}, "# 索引\r\n\r\n阿尔法, 12\r\n  贝塔, 14-16"),
])
def test_apply_preserves_crlf_and_missing_final_newline(tmp_path, kind, source_text, result_update, expected):
    source = tmp_path / f"{kind}.md"
    source.write_bytes(source_text.encode("utf-8"))
    out, target = tmp_path, tmp_path / "translated"
    context = context_for(kind, source)
    prepare(kind, source, out, target, context=context)
    result_path = out / json.loads((out / f"special_unit_{kind}_{kind}_manifest.json").read_text(encoding="utf-8"))["result_file"]
    result = json.loads(result_path.read_text(encoding="utf-8"))
    result["heading_translation"] = result_update["heading_translation"]
    if kind == "index":
        for row, term in zip(result["translations"], result_update["terms"]):
            row["term"] = term
    result_path.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
    produced = apply(kind, source, out, target, context=context)
    assert produced.read_bytes() == expected.encode("utf-8")


def test_prepare_apply_commands_then_normal_validator(tmp_path, monkeypatch):
    import pdf2epub.commands.markdown as commands
    import pdf2epub.toc_translation_workflow as toc_workflow
    from pdf2epub.commands.runtime import BookCommandContext
    from pdf2epub.markdown_subagent_validation import validate_markdown_subagent

    output = tmp_path / "out"
    source_dir = output / "polished_markdown" / "validated"
    source_dir.mkdir(parents=True)
    (source_dir / "refs.md").write_text("# References\n\n- Smith 2020\n", encoding="utf-8")
    config = {"pipeline": "translation", "translation": {"require_entities": False, "special_units": {"bibliography": "controlled"}}}
    context = BookCommandContext(config, tmp_path / "config.yaml", "Book", output)
    monkeypatch.setattr(commands, "load_book_context", lambda *a, **k: context)
    monkeypatch.setattr(commands, "_resolve_pdf_markdown_source", lambda *_: (source_dir, "polished"))
    monkeypatch.setattr(commands, "load_pdf_file_roles", lambda *_: {"refs.md": "bibliography"})
    monkeypatch.setattr(toc_workflow, "validate_toc_translation_subagent", lambda *_: {"valid": True})
    monkeypatch.setattr("pdf2epub.special_unit_context.build_special_unit_context", lambda _out, _file, kind, **_: context_for(kind, source_dir / "refs.md") | {"contract_sha256": "test-contract", "entries": []})
    args = SimpleNamespace(config="config.yaml", kind="bibliography", file="refs.md", skip_entities=False)
    assert commands.special_unit_prepare_command(args) == 0
    result_path = output / json.loads((output / "special_unit_bibliography_refs_manifest.json").read_text(encoding="utf-8"))["result_file"]
    result = json.loads(result_path.read_text(encoding="utf-8"))
    result["heading_translation"] = "参考文献"
    result_path.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
    assert commands.special_unit_apply_command(args) == 0
    report = validate_markdown_subagent(
        output, "translate", source_dir, output / "translated",
        file_roles={"refs.md": "bibliography"}, selected_files=["refs.md"],
        target_language="Chinese",
    )
    assert "refs.md" in report["valid_files"]


def test_translate_handoff_keeps_full_inventory_and_separates_controlled_pending(tmp_path):
    from pdf2epub.markdown_handoff import prepare_markdown_subagent
    source = tmp_path / "polished_markdown" / "validated"
    source.mkdir(parents=True)
    (source / "refs.md").write_text("# References\n\n- A 2020\n", encoding="utf-8")
    (source / "chapter.md").write_text("# Chapter\n\nText.\n", encoding="utf-8")
    paths = prepare_markdown_subagent(
        tmp_path, "translate", source, tmp_path / "translated", "English", "Chinese",
        file_roles={"refs.md": "bibliography"}, controlled_files={"refs.md": "bibliography"},
    )
    manifest = json.loads(paths["manifest"].read_text(encoding="utf-8"))
    assert set(manifest["files"]) == {"refs.md", "chapter.md"}
    assert manifest["pending_files"] == ["chapter.md"]
    assert manifest["special_pending_files"] == ["refs.md"]


def test_index_fixed_mapping_and_cross_reference_are_enforced(tmp_path):
    source = tmp_path / "index.md"
    source.write_text("Alpha, 1\nBeta see Alpha\n", encoding="utf-8")
    context = context_for("index", source) | {"entries": [{"kind": "domain", "policy": "fixed", "source": "Alpha", "target": "阿尔法"}]}
    out = tmp_path
    target = tmp_path / "translated"
    prepare("index", source, out, target, context=context)
    manifest_path = out / "special_unit_index_index_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    result_path = out / manifest["result_file"]
    result = json.loads(result_path.read_text(encoding="utf-8"))
    result["translations"][0].update(term="阿尔法")
    result["translations"][1].update(term="贝塔", cross_target="阿尔法")
    result_path.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
    applied = apply("index", source, out, target, context=context)
    assert applied.read_text(encoding="utf-8") == "阿尔法, 1\n贝塔 see 阿尔法\n"
    result["translations"][1]["cross_target"] = "不存在"
    result_path.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(ValueError, match="cross-reference"):
        apply("index", source, out, target, context=context)


def test_index_rejects_ambiguous_fixed_mapping_and_markdown_injection(tmp_path):
    source = tmp_path / "index.md"
    source.write_text("Alpha, 12\n", encoding="utf-8")
    ambiguous = context_for("index", source) | {"entries": [
        {"kind": "domain", "policy": "fixed", "source": "Alpha", "target": "甲"},
        {"kind": "domain", "policy": "fixed", "source": "Alpha", "target": "乙"},
    ]}
    out, target = tmp_path, tmp_path / "translated"
    with pytest.raises(ValueError, match="strict controlled structure"):
        prepare("index", source, out, target, context=ambiguous)

    context = context_for("index", source)
    prepare("index", source, out, target, context=context)
    manifest = json.loads((out / "special_unit_index_index_manifest.json").read_text(encoding="utf-8"))
    result_path = out / manifest["result_file"]
    result = json.loads(result_path.read_text(encoding="utf-8"))
    result["translations"][0]["term"] = "[click](https://example.invalid)"
    result_path.write_text(json.dumps(result), encoding="utf-8")
    with pytest.raises(ValueError, match="plain text"):
        apply("index", source, out, target, context=context)


def test_special_policy_is_opt_in_and_rejects_unknown_or_unsupported(tmp_path):
    from pdf2epub.pipeline_policy import PipelinePolicy
    assert PipelinePolicy.from_config({}).special_unit_modes == {}
    with pytest.raises(ValueError, match="unknown translation.special_units"):
        PipelinePolicy.from_config({"translation": {"special_units": {"notes": "controlled"}}})
    with pytest.raises(ValueError, match="only by the PDF translation pipeline"):
        PipelinePolicy.from_config({"pipeline": "epub_conversion", "translation": {"special_units": {"index": "controlled"}}})
