import json
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest

from pdf2epub.commands import tex as tex_commands


def _prepare_tex_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    source_dir = tmp_path / "fixture_source"
    source_dir.mkdir()
    (source_dir / "main.tex").write_bytes(
        (
            "\\documentclass{article}\n"
            "\\begin{document}\n"
            "Hello from the source.\n"
            "\\end{document}\n"
        ).encode("utf-8")
    )

    class Resolver:
        def source_id(self, source):
            return "sample"

        def materialize(self, source, destination):
            shutil.copytree(source_dir, destination, dirs_exist_ok=True)
            return SimpleNamespace(suggested_main_tex="main.tex")

    monkeypatch.setattr(
        "pdf2epub.tex_translation.arxiv.ArxivSourceResolver", Resolver
    )
    monkeypatch.setattr(
        "pdf2epub.subagent_runtime.resolve_subagent_model",
        lambda *args: "mock-model",
    )
    monkeypatch.setattr(
        "pdf2epub.subagent_runtime.estimate_tokens",
        lambda text: len(text.split()),
    )
    monkeypatch.setattr(
        tex_commands,
        "load_command_config",
        lambda args: ({"tex_translation": {"unit_chars": 12_000}}, None),
    )

    run_dir = tmp_path / "run"
    prepare_args = SimpleNamespace(
        source="sample",
        output_dir=str(run_dir),
        source_language=None,
        target_language=None,
        main_tex=None,
        unit_chars=None,
        resume=False,
    )
    assert tex_commands.translate_arxiv_command(prepare_args) == 0

    manifest_path = run_dir / ".pdf2epub" / "tex_subagent_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    for unit in manifest["units"]:
        source_path = run_dir / unit["source_file"]
        target_path = run_dir / unit["target_file"]
        target_path.parent.mkdir(parents=True, exist_ok=True)
        target_path.write_text(source_path.read_text(encoding="utf-8"), encoding="utf-8")
    return run_dir


def _validate_args(run_dir: Path) -> SimpleNamespace:
    return SimpleNamespace(output_dir=str(run_dir), compile_timeout=5)


def test_translate_arxiv_validate_writes_validated_checkpoint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    run_dir = _prepare_tex_run(tmp_path, monkeypatch)
    compile_calls = []

    class SuccessfulCompiler:
        def __init__(self, **kwargs):
            pass

        def compile(self, *args):
            compile_calls.append(args)
            return SimpleNamespace(
                success=True, pdf_path=tmp_path / "compiled.pdf", tail=lambda: ""
            )

    monkeypatch.setattr(
        "pdf2epub.tex_translation.compiler.TexCompiler", SuccessfulCompiler
    )

    assert tex_commands.translate_arxiv_validate_command(_validate_args(run_dir)) == 0

    manifest = json.loads(
        (run_dir / ".pdf2epub" / "tex_subagent_manifest.json").read_text(
            encoding="utf-8"
        )
    )
    assert manifest["validated_units"] == manifest["completed_units"]
    assert manifest["pending_units"] == []
    assert len(compile_calls) == 1


def test_translate_arxiv_validate_records_invalid_unit_without_compiling(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    run_dir = _prepare_tex_run(tmp_path, monkeypatch)
    manifest_path = run_dir / ".pdf2epub" / "tex_subagent_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    target_path = run_dir / manifest["units"][0]["target_file"]
    target_path.write_text("", encoding="utf-8")

    class UnexpectedCompiler:
        def __init__(self, **kwargs):
            pytest.fail("compiler must not run for an invalid unit")

    monkeypatch.setattr(
        "pdf2epub.tex_translation.compiler.TexCompiler", UnexpectedCompiler
    )

    assert tex_commands.translate_arxiv_validate_command(_validate_args(run_dir)) == 1

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["validated_units"] == []
    assert manifest["pending_units"] == [manifest["units"][0]["id"]]


def test_translate_arxiv_validate_leaves_checkpoint_empty_when_compile_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    run_dir = _prepare_tex_run(tmp_path, monkeypatch)

    class FailedCompiler:
        def __init__(self, **kwargs):
            pass

        def compile(self, *args):
            return SimpleNamespace(success=False, tail=lambda: "synthetic compile error")

    monkeypatch.setattr(
        "pdf2epub.tex_translation.compiler.TexCompiler", FailedCompiler
    )

    assert tex_commands.translate_arxiv_validate_command(_validate_args(run_dir)) == 1

    manifest = json.loads(
        (run_dir / ".pdf2epub" / "tex_subagent_manifest.json").read_text(
            encoding="utf-8"
        )
    )
    assert manifest["validated_units"] == []
    assert len(manifest["completed_units"]) == 1
