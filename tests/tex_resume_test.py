import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from pdf2epub.commands import tex as tex_commands


def _make_source(path: Path) -> Path:
    path.mkdir()
    (path / "main.tex").write_bytes(
        (
            "\\documentclass{article}\n"
            "\\begin{document}\n"
            "A short source paragraph for checkpoint validation.\n"
            "\\end{document}\n"
        ).encode("utf-8")
    )
    return path


def _args(
    source: Path,
    run_dir: Path,
    *,
    resume=False,
    target_language="Simplified Chinese",
    unit_chars=12_000,
):
    return SimpleNamespace(
        source=str(source),
        output_dir=str(run_dir),
        source_language="English",
        target_language=target_language,
        main_tex=None,
        unit_chars=unit_chars,
        resume=resume,
    )


def _manifest(run_dir: Path) -> dict:
    return json.loads(
        (run_dir / ".pdf2epub" / "tex_subagent_manifest.json").read_text(
            encoding="utf-8"
        )
    )


def _copy_units_to_targets(run_dir: Path) -> None:
    manifest = _manifest(run_dir)
    for unit in manifest["units"]:
        source_path = run_dir / unit["source_file"]
        target_path = run_dir / unit["target_file"]
        target_path.parent.mkdir(parents=True, exist_ok=True)
        target_path.write_bytes(source_path.read_bytes())


def _install_successful_compiler(monkeypatch: pytest.MonkeyPatch) -> None:
    class SuccessfulCompiler:
        def __init__(self, **kwargs):
            pass

        def compile(self, *args):
            return SimpleNamespace(success=True, pdf_path=Path("mock.pdf"), tail=lambda: "")

    monkeypatch.setattr(
        "pdf2epub.tex_translation.compiler.TexCompiler", SuccessfulCompiler
    )


def _prepare(
    source: Path,
    run_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    resume: bool,
    target_language: str = "Simplified Chinese",
    unit_chars: int = 12_000,
) -> None:
    monkeypatch.setattr(
        "pdf2epub.commands.tex.load_command_config",
        lambda args: ({"tex_translation": {}}, None),
    )
    monkeypatch.setattr(
        "pdf2epub.subagent_runtime.resolve_subagent_model",
        lambda *args: "mock-model",
    )
    monkeypatch.setattr(
        "pdf2epub.subagent_runtime.estimate_tokens",
        lambda text: len(text.split()),
    )
    assert tex_commands.translate_arxiv_command(
        _args(
            source,
            run_dir,
            resume=resume,
            target_language=target_language,
            unit_chars=unit_chars,
        )
    ) == 0


def _validate(run_dir: Path) -> int:
    return tex_commands.translate_arxiv_validate_command(
        SimpleNamespace(output_dir=str(run_dir), compile_timeout=5)
    )


@pytest.fixture
def tex_run(tmp_path: Path) -> tuple[Path, Path]:
    return _make_source(tmp_path / "local_source"), tmp_path / "run"


def test_resume_preserves_validated_units_across_consecutive_prepares(
    tex_run: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
):
    source, run_dir = tex_run
    _install_successful_compiler(monkeypatch)
    _prepare(source, run_dir, monkeypatch, resume=False)
    _copy_units_to_targets(run_dir)
    assert _validate(run_dir) == 0

    first_validated = _manifest(run_dir)["validated_units"]
    assert first_validated
    manifest = _manifest(run_dir)
    assert manifest["schema_version"] == 2
    for unit in manifest["units"]:
        target_path = run_dir / unit["target_file"]
        assert manifest["validated_target_sha256"][unit["id"]] == hashlib.sha256(
            target_path.read_bytes()
        ).hexdigest()
    for _ in range(2):
        _prepare(source, run_dir, monkeypatch, resume=True)
        manifest = _manifest(run_dir)
        assert manifest["validated_units"] == first_validated
        assert manifest["completed_units"] == first_validated
        assert manifest["pending_units"] == []


def test_resume_invalidates_checkpoint_when_target_was_modified(
    tex_run: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
):
    source, run_dir = tex_run
    _install_successful_compiler(monkeypatch)
    _prepare(source, run_dir, monkeypatch, resume=False)
    _copy_units_to_targets(run_dir)
    assert _validate(run_dir) == 0

    manifest = _manifest(run_dir)
    target_path = run_dir / manifest["units"][0]["target_file"]
    target_path.write_text("changed after compilation", encoding="utf-8")
    _prepare(source, run_dir, monkeypatch, resume=True)

    manifest = _manifest(run_dir)
    assert manifest["validated_units"] == []
    assert manifest["completed_units"] == []
    assert manifest["pending_units"] == [unit["id"] for unit in manifest["units"]]


def test_compile_cannot_checkpoint_target_changed_during_compilation(
    tex_run: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
):
    source, run_dir = tex_run
    _prepare(source, run_dir, monkeypatch, resume=False)
    _copy_units_to_targets(run_dir)
    target_path = run_dir / _manifest(run_dir)["units"][0]["target_file"]

    class MutatingCompiler:
        def __init__(self, **kwargs):
            pass

        def compile(self, *args):
            target_path.write_text("changed during compile", encoding="utf-8")
            return SimpleNamespace(
                success=True, pdf_path=Path("mock.pdf"), tail=lambda: ""
            )

    monkeypatch.setattr(
        "pdf2epub.tex_translation.compiler.TexCompiler", MutatingCompiler
    )

    assert _validate(run_dir) == 1

    manifest = _manifest(run_dir)
    assert manifest["validated_units"] == []
    assert manifest["validated_target_sha256"] == {}


def test_resume_invalidates_checkpoint_when_target_language_changes(
    tex_run: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
):
    source, run_dir = tex_run
    _install_successful_compiler(monkeypatch)
    _prepare(source, run_dir, monkeypatch, resume=False)
    _copy_units_to_targets(run_dir)
    assert _validate(run_dir) == 0

    _prepare(
        source,
        run_dir,
        monkeypatch,
        resume=True,
        target_language="German",
    )

    manifest = _manifest(run_dir)
    assert manifest["target_language"] == "German"
    assert manifest["validated_units"] == []
    assert manifest["completed_units"] == []
    assert manifest["pending_units"] == [unit["id"] for unit in manifest["units"]]


def test_resume_invalidates_checkpoint_when_layout_changes(
    tex_run: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
):
    source, run_dir = tex_run
    _install_successful_compiler(monkeypatch)
    _prepare(source, run_dir, monkeypatch, resume=False)
    _copy_units_to_targets(run_dir)
    assert _validate(run_dir) == 0

    _prepare(source, run_dir, monkeypatch, resume=True, unit_chars=1_000)

    manifest = _manifest(run_dir)
    assert manifest["validated_units"] == []
    assert manifest["completed_units"] == []
    assert set(manifest["pending_units"]) == {unit["id"] for unit in manifest["units"]}


def test_legacy_manifest_without_target_hash_does_not_reuse_checkpoints(
    tex_run: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
):
    source, run_dir = tex_run
    _install_successful_compiler(monkeypatch)
    _prepare(source, run_dir, monkeypatch, resume=False)
    _copy_units_to_targets(run_dir)
    assert _validate(run_dir) == 0

    manifest_path = run_dir / ".pdf2epub" / "tex_subagent_manifest.json"
    manifest = _manifest(run_dir)
    manifest["schema_version"] = 1
    manifest.pop("validated_target_sha256", None)
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    _prepare(source, run_dir, monkeypatch, resume=True)

    manifest = _manifest(run_dir)
    assert manifest["validated_units"] == []
    assert manifest["completed_units"] == []
