"""End-to-end freshness and recovery gates for controlled special units."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from pdf2epub.commands.runtime import BookCommandContext
from pdf2epub.markdown_handoff import prepare_markdown_subagent
from pdf2epub.markdown_subagent_validation import validate_markdown_subagent
from pdf2epub.special_unit_context import build_special_unit_context
from pdf2epub.special_unit_translation import apply, prepare
from pdf2epub.validation_receipts import build_input_snapshot, validation_receipt_is_current
from pdf2epub.workflow_contracts import MARKDOWN_VALIDATION_SCHEMA_VERSION


CONFIG = {
    "pipeline": "translation",
    "translation": {
        "source_language": "English",
        "target_language": "Chinese",
        "require_entities": False,
        "special_units": {"index": "controlled"},
    },
}
SOURCE_NAME = "index.md"
HEADING_CONTEXT = {
    "node_id": "index-node",
    "anchor": "toc-index-node",
    "parent_node_id": None,
    "projection_version": 2,
    "toc_title": "索引",
    "binding_mode": "visible",
    "children": [],
}


def _write_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


def _setup(tmp_path: Path):
    output = tmp_path / "output"
    source_dir = output / "polished_markdown" / "validated"
    target_dir = output / "translated"
    source_dir.mkdir(parents=True)
    source = source_dir / SOURCE_NAME
    source.write_text("# Index\n\nAlpha, 12\n", encoding="utf-8")
    _write_json(output / "toc_tree.json", {"schema_version": 2, "chapters": [{"node_id": "index-node", "title": "Index", "children": []}]})
    _write_json(
        output / "toc_tree_translated.json",
        {"schema_version": 2, "chapters": [{"node_id": "index-node", "title": "索引", "children": []}]},
    )
    _write_json(
        output / "toc_translation_source.json",
        {"contract_version": 3, "toc": {"schema_version": 2, "chapters": [{"node_id": "index-node", "title": "Index", "children": []}]}},
    )
    _write_json(
        output / "ocr_markdown" / "tree_progress.json",
        {"units": [{"index_path": [1], "node_id": "index-node", "file": SOURCE_NAME}]},
    )

    unit_context_path = output / "translation_glossaries" / "unit_contexts" / SOURCE_NAME.replace(".md", ".json")
    _write_json(
        unit_context_path,
        {
            "schema_version": 1,
            "source_file": SOURCE_NAME,
            "entries": [
                {"kind": "domain", "source": "Alpha", "target": "阿尔法", "policy": "fixed"}
            ],
        },
    )
    parent_context_path = output / "translation_glossaries" / "entities.json"
    _write_json(parent_context_path, {"revision": 1})
    return {
        "output": output,
        "source_dir": source_dir,
        "target_dir": target_dir,
        "source": source,
        "unit_context_path": unit_context_path,
        "parent_context_path": parent_context_path,
    }


def _prepare_translate(fx, *, source_language="English", target_language="Chinese", controlled=True):
    return prepare_markdown_subagent(
        fx["output"],
        "translate",
        fx["source_dir"],
        fx["target_dir"],
        source_language,
        target_language,
        config={},
        file_roles={SOURCE_NAME: "index"},
        context_files={"translation_entities": fx["parent_context_path"]},
        heading_contexts={SOURCE_NAME: HEADING_CONTEXT},
        unit_context_files={SOURCE_NAME: fx["unit_context_path"]},
        controlled_files={SOURCE_NAME: "index"} if controlled else None,
        resume=False,
    )


def _resume(fx, *, source_language="English", target_language="Chinese", controlled=True):
    paths = prepare_markdown_subagent(
        fx["output"],
        "translate",
        fx["source_dir"],
        fx["target_dir"],
        source_language,
        target_language,
        config={},
        file_roles={SOURCE_NAME: "index"},
        context_files={"translation_entities": fx["parent_context_path"]},
        heading_contexts={SOURCE_NAME: HEADING_CONTEXT},
        unit_context_files={SOURCE_NAME: fx["unit_context_path"]},
        controlled_files={SOURCE_NAME: "index"} if controlled else None,
        resume=True,
    )
    return json.loads(paths["manifest"].read_text(encoding="utf-8"))


def _finish_special_unit(fx):
    context = build_special_unit_context(
        fx["output"], SOURCE_NAME, "index",
        source_language="English", target_language="Chinese",
    )
    special_manifest = prepare(
        "index", fx["source"], fx["output"], fx["target_dir"], context=context
    )
    result_path = fx["output"] / special_manifest["result_file"]
    result = json.loads(result_path.read_text(encoding="utf-8"))
    result["heading_translation"] = "索引"
    result["translations"][0]["term"] = "阿尔法"
    result_path.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
    target = apply("index", fx["source"], fx["output"], fx["target_dir"], context=context)
    return context, special_manifest, result_path, target


def _validate_normally(fx):
    return validate_markdown_subagent(
        fx["output"],
        "translate",
        fx["source_dir"],
        fx["target_dir"],
        structural_patterns=(r"^#{1,6}\s", r"\[\^[^\]]+\]"),
        file_roles={SOURCE_NAME: "index"},
        selected_files=None,
        target_language="Chinese",
    )


def _run_formal_file_validator(fx, monkeypatch, *, config=None):
    import pdf2epub.commands.markdown as command

    config = config or CONFIG
    context = BookCommandContext(
        config, fx["output"].parent / "config.yaml", "Book", fx["output"]
    )
    monkeypatch.setattr(command, "load_book_context", lambda *_args, **_kwargs: context)
    monkeypatch.setattr(command, "load_config", lambda *_args, **_kwargs: config)
    monkeypatch.setattr(command, "_resolve_pdf_markdown_source", lambda *_args: (fx["source_dir"], "polished"))
    monkeypatch.setattr(command, "load_pdf_file_roles", lambda *_args: {SOURCE_NAME: "index"})
    args = SimpleNamespace(config="config.yaml", file=SOURCE_NAME, check_only=False)
    return command.translate_validate_command(args)


def _validated_checkpoint(fx):
    report = _validate_normally(fx)
    assert SOURCE_NAME in report["valid_files"], report
    assert not report["missing"]
    assert not report["invalid"]
    return report


def test_prepare_apply_validate_and_resume_reuse_controlled_checkpoint(tmp_path):
    fx = _setup(tmp_path)
    paths = _prepare_translate(fx)
    context, special_manifest, result_path, target = _finish_special_unit(fx)

    assert paths["manifest"].is_file()
    assert special_manifest["context_contract_sha256"] == context["contract_sha256"]
    assert result_path.is_file()
    assert target.read_text(encoding="utf-8") == "# 索引\n\n阿尔法, 12\n"
    _validated_checkpoint(fx)

    resumed = _resume(fx)
    assert SOURCE_NAME in resumed["completed_files"]
    assert SOURCE_NAME not in resumed["special_pending_files"]


@pytest.mark.parametrize("artifact", ["result", "unit_context", "parent_context", "target", "source"])
def test_changed_consumed_artifact_cannot_reuse_or_validate(tmp_path, monkeypatch, artifact):
    fx = _setup(tmp_path)
    _prepare_translate(fx)
    _context, _manifest, result_path, target = _finish_special_unit(fx)
    _validated_checkpoint(fx)

    if artifact == "result":
        result = json.loads(result_path.read_text(encoding="utf-8"))
        result["translations"][0]["term"] = "另一个译词"
        result_path.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
    elif artifact == "unit_context":
        _write_json(fx["unit_context_path"], {"entries": []})
    elif artifact == "parent_context":
        _write_json(fx["parent_context_path"], {"revision": 2})
    elif artifact == "target":
        target.write_text("# 索引\n\n别的译词, 12\n", encoding="utf-8")
    else:
        fx["source"].write_text("# Index\n\nAlpha, 12\nBeta, 14\n", encoding="utf-8")

    # Stale parent snapshots must fail closed. Other changes leave a pending
    # controlled unit rather than reusing its prior local validation receipt.
    if artifact == "parent_context":
        assert _run_formal_file_validator(fx, monkeypatch) == 1
        resumed = _resume(fx)
        assert SOURCE_NAME in resumed["special_pending_files"]
        assert SOURCE_NAME not in resumed["completed_files"]
    else:
        resumed = _resume(fx)
        assert SOURCE_NAME in resumed["special_pending_files"]
        assert SOURCE_NAME not in resumed["completed_files"]
        assert _run_formal_file_validator(fx, monkeypatch) == 1


@pytest.mark.parametrize(
    "source_language,target_language,controlled",
    [("German", "Chinese", True), ("English", "German", True), ("English", "Chinese", False)],
)
def test_language_or_controlled_mode_change_does_not_reuse_checkpoint(
    tmp_path, source_language, target_language, controlled
):
    fx = _setup(tmp_path)
    _prepare_translate(fx)
    _finish_special_unit(fx)
    _validated_checkpoint(fx)
    resumed = _resume(
        fx, source_language=source_language, target_language=target_language, controlled=controlled
    )
    assert SOURCE_NAME not in resumed["completed_files"]
    assert SOURCE_NAME in resumed["pending_files"] or SOURCE_NAME in resumed["special_pending_files"]


@pytest.mark.parametrize("change", ["source", "context"])
def test_repeated_prepare_preserves_same_contract_result_but_new_contract_gets_template(tmp_path, change):
    fx = _setup(tmp_path)
    _prepare_translate(fx)
    context, manifest, result_path, _target = _finish_special_unit(fx)
    result = json.loads(result_path.read_text(encoding="utf-8"))
    result["translations"][0]["term"] = "用户已确认"
    result_path.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")

    same = build_special_unit_context(
        fx["output"], SOURCE_NAME, "index", source_language="English", target_language="Chinese"
    )
    repeated = prepare("index", fx["source"], fx["output"], fx["target_dir"], context=same)
    assert repeated["result_file"] == manifest["result_file"]
    assert json.loads(result_path.read_text(encoding="utf-8"))["translations"][0]["term"] == "用户已确认"

    if change == "source":
        fx["source"].write_text("# Index\n\nAlpha, 12\nBeta, 14\n", encoding="utf-8")
    else:
        context_data = json.loads(fx["unit_context_path"].read_text(encoding="utf-8"))
        context_data["entries"].append(
            {"kind": "domain", "source": "Beta", "target": "贝塔", "policy": "preferred"}
        )
        _write_json(fx["unit_context_path"], context_data)
    # Re-prepare the translate manifest so its unit projection hash is current.
    _prepare_translate(fx)
    changed = build_special_unit_context(
        fx["output"], SOURCE_NAME, "index", source_language="English", target_language="Chinese"
    )
    changed_manifest = prepare("index", fx["source"], fx["output"], fx["target_dir"], context=changed)
    assert changed_manifest["result_file"] != manifest["result_file"]
    new_template = json.loads((fx["output"] / changed_manifest["result_file"]).read_text(encoding="utf-8"))
    assert new_template["translations"][0]["term"] == ""
    assert json.loads(result_path.read_text(encoding="utf-8"))["translations"][0]["term"] == "用户已确认"


def _write_complete_build_receipt(fx, config_path):
    output = fx["output"]
    (output / "pages").mkdir(exist_ok=True)
    (output / "pages" / "page_001.md").write_text("page", encoding="utf-8")
    toc_hash = hashlib.sha256((output / "toc_tree.json").read_bytes()).hexdigest()
    from pdf2epub.utils.fingerprints import pages_fingerprint

    pages_hash = pages_fingerprint(output / "pages")
    progress_path = output / "ocr_markdown" / "tree_progress.json"
    progress = json.loads(progress_path.read_text(encoding="utf-8"))
    progress["fingerprint"] = {"toc_sha256": toc_hash, "pages_sha256": pages_hash}
    _write_json(progress_path, progress)
    config_path.write_text("title: Book\n", encoding="utf-8")
    source_hash = hashlib.sha256(fx["source"].read_bytes()).hexdigest()
    target_hash = hashlib.sha256((fx["target_dir"] / SOURCE_NAME).read_bytes()).hexdigest()
    snapshot = build_input_snapshot(output, config_path, translated=True)
    report_path = output / "translate_validation.json"
    _write_json(
        report_path,
        {
            "schema_version": MARKDOWN_VALIDATION_SCHEMA_VERSION,
            "task": "translate",
            "scope": "full",
            "all_passed": True,
            "source_sha256": {SOURCE_NAME: source_hash},
            "target_sha256": {SOURCE_NAME: target_hash},
            "build_inputs": snapshot,
        },
    )
    return report_path


@pytest.mark.parametrize("consumed_context", ["parent", "unit"])
def test_full_build_receipt_invalidated_when_actual_consumed_context_changes(
    tmp_path, consumed_context
):
    fx = _setup(tmp_path)
    _prepare_translate(fx)
    _finish_special_unit(fx)
    _validated_checkpoint(fx)
    config_path = tmp_path / "config.yaml"
    report_path = _write_complete_build_receipt(fx, config_path)
    assert validation_receipt_is_current(
        report_path,
        fx["source_dir"],
        fx["target_dir"],
        task="translate",
        output_dir=fx["output"],
        config_path=config_path,
        require_build_inputs=True,
    )

    changed = fx["parent_context_path"] if consumed_context == "parent" else fx["unit_context_path"]
    _write_json(changed, {"revision": "changed"})
    assert not validation_receipt_is_current(
        report_path,
        fx["source_dir"],
        fx["target_dir"],
        task="translate",
        output_dir=fx["output"],
        config_path=config_path,
        require_build_inputs=True,
    )
