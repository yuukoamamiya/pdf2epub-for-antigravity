from pathlib import Path
from types import SimpleNamespace

import pytest

from pdf2epub.tex_translation.compiler import TexCompiler


@pytest.mark.parametrize(
    ("shell_escape", "expected_shell_flag"),
    [(False, "-no-shell-escape"), (True, "-shell-escape")],
)
def test_compile_disables_latexmk_rc_files_and_uses_absolute_main_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    shell_escape: bool,
    expected_shell_flag: str,
):
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    main_path = project_dir / "-main.tex"
    main_path.write_text("\\documentclass{article}\n", encoding="utf-8")
    # This file must never be loaded by latexmk, even when explicit shell escape is
    # enabled. The test inspects the invocation without executing TeX or Perl.
    (project_dir / ".latexmkrc").write_text(
        "system('touch should-not-run');\n", encoding="utf-8"
    )
    calls = []

    monkeypatch.setattr(
        "pdf2epub.tex_translation.compiler.shutil.which", lambda _: "/latexmk"
    )

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        main_path.with_suffix(".pdf").write_bytes(b"synthetic pdf")
        return SimpleNamespace(stdout="compiled", returncode=0)

    monkeypatch.setattr("pdf2epub.tex_translation.compiler.subprocess.run", fake_run)
    log_path = tmp_path / "logs" / "compile.log"

    result = TexCompiler(shell_escape=shell_escape).compile(
        project_dir, "-main.tex", log_path
    )

    command, kwargs = calls[0]
    assert "-norc" in command
    assert expected_shell_flag in command
    assert command[-1] == str(main_path.resolve())
    assert kwargs["cwd"] == project_dir.resolve()
    assert (project_dir / "should-not-run").exists() is False
    assert result.success is True
    assert result.command == tuple(command)


def test_compile_finds_pdf_in_project_root_for_subdirectory_main_tex(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    project_dir = tmp_path / "project"
    sub_dir = project_dir / "chapters"
    sub_dir.mkdir(parents=True)
    main_path = sub_dir / "main.tex"
    main_path.write_text("\\documentclass{article}\n", encoding="utf-8")

    monkeypatch.setattr("pdf2epub.tex_translation.compiler.shutil.which", lambda _: "/latexmk")

    def fake_run(command, **kwargs):
        (project_dir / "main.pdf").write_bytes(b"root pdf")
        return SimpleNamespace(stdout="ok", returncode=0)

    monkeypatch.setattr("pdf2epub.tex_translation.compiler.subprocess.run", fake_run)
    result = TexCompiler().compile(project_dir, "chapters/main.tex", tmp_path / "compile.log")
    assert result.success is True
    assert result.pdf_path == (project_dir / "main.pdf").resolve()
