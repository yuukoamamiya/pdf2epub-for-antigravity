from pathlib import Path
from types import SimpleNamespace

import pytest
from loguru import logger

import pdf2epub.commands.pdf as pdf_commands
from pdf2epub.commands.pdf import _warn_if_translated_checkpoint_is_ignored
from pdf2epub.pipeline_policy import PipelinePolicy


@pytest.fixture
def warning_messages():
    messages = []
    sink_id = logger.add(lambda message: messages.append(message.record["message"]))
    try:
        yield messages
    finally:
        logger.remove(sink_id)


def _translation_policy() -> PipelinePolicy:
    return PipelinePolicy.from_config({"pipeline": "translation"})


def _conversion_policy() -> PipelinePolicy:
    return PipelinePolicy.from_config({"pipeline": "epub_conversion"})


def test_warns_when_current_translation_is_not_packaged(
    tmp_path: Path, monkeypatch, warning_messages
):
    monkeypatch.setattr(
        pdf_commands, "validation_receipt_is_current", lambda *args, **kwargs: True
    )

    warned = _warn_if_translated_checkpoint_is_ignored(
        _translation_policy(),
        SimpleNamespace(translated=False),
        tmp_path,
        tmp_path / "polished_markdown" / "validated",
        config_path=None,
    )

    assert warned is True
    assert any("--translated" in message for message in warning_messages)
    assert any("只构建原稿 EPUB" in message for message in warning_messages)


def test_does_not_warn_when_translated_flag_is_set(tmp_path: Path, monkeypatch):
    calls = []
    monkeypatch.setattr(
        pdf_commands,
        "validation_receipt_is_current",
        lambda *args, **kwargs: calls.append((args, kwargs)) or True,
    )

    warned = _warn_if_translated_checkpoint_is_ignored(
        _translation_policy(),
        SimpleNamespace(translated=True),
        tmp_path,
        tmp_path / "polished_markdown" / "validated",
        config_path=None,
    )

    assert warned is False
    assert calls == []


def test_does_not_warn_for_conversion_pipeline(tmp_path: Path, monkeypatch):
    calls = []
    monkeypatch.setattr(
        pdf_commands,
        "validation_receipt_is_current",
        lambda *args, **kwargs: calls.append((args, kwargs)) or True,
    )

    warned = _warn_if_translated_checkpoint_is_ignored(
        _conversion_policy(),
        SimpleNamespace(translated=False),
        tmp_path,
        tmp_path / "ocr_markdown",
        config_path=None,
    )

    assert warned is False
    assert calls == []


def test_does_not_warn_when_receipt_is_stale(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(
        pdf_commands, "validation_receipt_is_current", lambda *args, **kwargs: False
    )

    warned = _warn_if_translated_checkpoint_is_ignored(
        _translation_policy(),
        SimpleNamespace(translated=False),
        tmp_path,
        tmp_path / "polished_markdown" / "validated",
        config_path=None,
    )

    assert warned is False


def test_receipt_check_errors_are_silently_ignored(tmp_path: Path, monkeypatch):
    def fail(*args, **kwargs):
        raise OSError("receipt unreadable")

    monkeypatch.setattr(pdf_commands, "validation_receipt_is_current", fail)

    warned = _warn_if_translated_checkpoint_is_ignored(
        _translation_policy(),
        SimpleNamespace(translated=False),
        tmp_path,
        tmp_path / "polished_markdown" / "validated",
        config_path=None,
    )

    assert warned is False


def test_receipt_is_checked_against_polished_source_and_translated_target(
    tmp_path: Path, monkeypatch
):
    captured = {}

    def fake(report_path, source_dir, target_dir, **kwargs):
        captured.update(
            report_path=report_path,
            source_dir=source_dir,
            target_dir=target_dir,
            kwargs=kwargs,
        )
        return True

    monkeypatch.setattr(pdf_commands, "validation_receipt_is_current", fake)
    source_dir = tmp_path / "polished_markdown" / "validated"
    config_path = tmp_path / "config.yaml"

    _warn_if_translated_checkpoint_is_ignored(
        _translation_policy(),
        SimpleNamespace(translated=False),
        tmp_path,
        source_dir,
        config_path=config_path,
    )

    assert captured["report_path"] == tmp_path / "translate_validation.json"
    assert captured["source_dir"] == source_dir
    assert captured["target_dir"] == tmp_path / "translated" / "validated"
    assert captured["kwargs"]["task"] == "translate"
    assert captured["kwargs"]["output_dir"] == tmp_path
    assert captured["kwargs"]["config_path"] == config_path
    assert captured["kwargs"]["require_build_inputs"] is True
