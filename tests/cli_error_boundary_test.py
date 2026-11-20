"""The CLI has one error boundary, and tracebacks are opt-in."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from loguru import logger

from pdf2epub.cli import run_command
from pdf2epub.command_errors import log_command_error


@pytest.fixture
def records():
    captured: list[dict] = []
    sink = logger.add(lambda message: captured.append(message.record))
    try:
        yield captured
    finally:
        logger.remove(sink)


def _raiser(exc: BaseException) -> SimpleNamespace:
    def func(_args):
        raise exc

    return SimpleNamespace(func=func, debug=False)


def test_unexpected_error_becomes_one_line(records, monkeypatch):
    monkeypatch.delenv("PDF2EPUB_DEBUG", raising=False)

    assert run_command(_raiser(ValueError("boom"))) == 1

    messages = [record["message"] for record in records]
    assert any("ValueError: boom" in message for message in messages)
    assert any("--debug" in message for message in messages)
    assert all(record["exception"] is None for record in records)


def test_debug_flag_expands_the_traceback(records, monkeypatch):
    monkeypatch.delenv("PDF2EPUB_DEBUG", raising=False)
    args = _raiser(ValueError("boom"))
    args.debug = True

    assert run_command(args) == 1

    assert any(record["exception"] is not None for record in records)


def test_debug_environment_variable_expands_the_traceback(records, monkeypatch):
    monkeypatch.setenv("PDF2EPUB_DEBUG", "1")

    assert run_command(_raiser(ValueError("boom"))) == 1

    assert any(record["exception"] is not None for record in records)


def test_handler_error_helper_keeps_one_line_and_names_the_type(records, monkeypatch):
    monkeypatch.delenv("PDF2EPUB_DEBUG", raising=False)

    log_command_error(
        "Could not prepare translate task",
        ValueError("boom"),
        args=SimpleNamespace(debug=False),
    )

    messages = [record["message"] for record in records]
    assert any("Could not prepare translate task: ValueError: boom" in message for message in messages)
    assert any("--debug" in message for message in messages)
    assert all(record["exception"] is None for record in records)


def test_handler_error_helper_expands_with_debug(records):
    log_command_error(
        "Could not prepare translate task",
        ValueError("boom"),
        args=SimpleNamespace(debug=True),
    )

    assert any(record["exception"] is not None for record in records)


def test_handler_exit_codes_pass_through(records):
    assert run_command(SimpleNamespace(func=lambda _args: 2, debug=False)) == 2
    assert run_command(SimpleNamespace(func=lambda _args: 0, debug=False)) == 0
    assert records == []


def test_keyboard_interrupt_returns_130(records):
    assert run_command(_raiser(KeyboardInterrupt())) == 130
    assert any("Interrupted" in record["message"] for record in records)


def test_system_exit_is_not_swallowed(records):
    with pytest.raises(SystemExit):
        run_command(_raiser(SystemExit(3)))
