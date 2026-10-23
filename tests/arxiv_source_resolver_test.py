import pytest

from pdf2epub.tex_translation.arxiv import ArxivSourceResolver
import pdf2epub.tex_translation.arxiv as arxiv_module


def test_materialize_same_destination_rejects_different_local_source_and_preserves_snapshot(tmp_path):
    source_a = tmp_path / "source-a"
    source_b = tmp_path / "source-b"
    destination = tmp_path / "run" / "source"
    source_a.mkdir()
    source_b.mkdir()
    (source_a / "main.tex").write_text("\\title{A}\n", encoding="utf-8")
    (source_b / "main.tex").write_text("\\title{B}\n", encoding="utf-8")

    resolver = ArxivSourceResolver()
    first = resolver.materialize(source_a, destination)
    with pytest.raises(ValueError, match="different input"):
        resolver.materialize(source_b, destination)

    assert first.source_id == "source-a"
    assert (destination / "main.tex").read_text(encoding="utf-8") == "\\title{A}\n"


def test_materialize_reuses_matching_completed_snapshot(tmp_path):
    source = tmp_path / "source"
    destination = tmp_path / "run" / "source"
    source.mkdir()
    (source / "main.tex").write_text("\\title{A}\n", encoding="utf-8")

    resolver = ArxivSourceResolver()
    first = resolver.materialize(source, destination)
    second = resolver.materialize(source, destination)

    assert second == first
    assert sorted(path.name for path in destination.iterdir()) == ["main.tex"]


def test_materialize_restore_keeps_tex_entry_point(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    (project / "paper.tex").write_text("\\documentclass{article}\n", encoding="utf-8")
    destination = tmp_path / "run" / "source"
    resolver = ArxivSourceResolver()

    first = resolver.materialize(project / "paper.tex", destination)
    resumed = resolver.materialize(project / "paper.tex", destination)

    assert first.suggested_main_tex == "paper.tex"
    assert resumed.suggested_main_tex == "paper.tex"


def test_materialize_recovers_matching_legacy_local_snapshot(tmp_path):
    source = tmp_path / "source"
    destination = tmp_path / "run" / "source"
    source.mkdir()
    destination.mkdir(parents=True)
    (source / "main.tex").write_text("\\title{A}\n", encoding="utf-8")
    (destination / "main.tex").write_text("\\title{A}\n", encoding="utf-8")

    result = ArxivSourceResolver().materialize(source, destination)

    assert result.source_id == "source"
    assert (destination.parent / ".source.source.json").is_file()


def test_materialize_fails_closed_for_unverifiable_legacy_snapshot(tmp_path):
    source = tmp_path / "source"
    destination = tmp_path / "run" / "source"
    source.mkdir()
    destination.mkdir(parents=True)
    (source / "main.tex").write_text("\\title{B}\n", encoding="utf-8")
    (destination / "main.tex").write_text("\\title{A}\n", encoding="utf-8")

    with pytest.raises(ValueError, match="no verifiable source identity"):
        ArxivSourceResolver().materialize(source, destination)
    assert (destination / "main.tex").read_text(encoding="utf-8") == "\\title{A}\n"


def test_materialize_detects_changed_contents_at_same_input_path(tmp_path):
    source = tmp_path / "source"
    destination = tmp_path / "run" / "source"
    source.mkdir()
    source_file = source / "main.tex"
    source_file.write_text("\\title{A}\n", encoding="utf-8")
    resolver = ArxivSourceResolver()
    resolver.materialize(source, destination)

    source_file.write_text("\\title{B}\n", encoding="utf-8")
    with pytest.raises(ValueError, match="different input"):
        resolver.materialize(source, destination)
    assert (destination / "main.tex").read_text(encoding="utf-8") == "\\title{A}\n"


def test_materialize_rejects_modified_completed_snapshot(tmp_path):
    source = tmp_path / "source"
    destination = tmp_path / "run" / "source"
    source.mkdir()
    (source / "main.tex").write_text("\\title{A}\n", encoding="utf-8")
    resolver = ArxivSourceResolver()
    resolver.materialize(source, destination)
    (destination / "main.tex").write_text("\\title{tampered}\n", encoding="utf-8")

    with pytest.raises(ValueError, match="failed integrity check"):
        resolver.materialize(source, destination)


def test_materialize_failure_leaves_no_partial_snapshot(tmp_path):
    archive = tmp_path / "broken.gz"
    archive.write_bytes(b"\x1f\x8bnot a gzip archive")
    destination = tmp_path / "run" / "source"

    with pytest.raises((ValueError, OSError)):
        ArxivSourceResolver().materialize(archive, destination)

    assert not destination.exists()
    assert not (destination.parent / ".source.source.json").exists()
    assert not list(destination.parent.glob(".source.staging-*"))


def test_marker_write_failure_leaves_remote_snapshot_untrusted_on_resume(tmp_path, monkeypatch):
    destination = tmp_path / "run" / "source"
    resolver = ArxivSourceResolver()

    def fake_download(_arxiv_id, target):
        (target / "main.tex").write_text("\\title{A}\n", encoding="utf-8")

    def fail_marker(*_args, **_kwargs):
        raise OSError("marker write failed")

    monkeypatch.setattr(resolver, "_download_arxiv", fake_download)
    monkeypatch.setattr(arxiv_module, "atomic_write_json", fail_marker)
    with pytest.raises(OSError, match="marker write failed"):
        resolver.materialize("2401.12345", destination)
    assert (destination / "main.tex").is_file()

    with pytest.raises(ValueError, match="no verifiable source identity"):
        resolver.materialize("2401.12345", destination)
