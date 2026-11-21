"""Operator-facing error reporting shared by command handlers.

Handlers catch expected failures and return a non-zero exit code.  Without a
traceback, a deep hash/contract error degrades to a single line that cannot be
diagnosed.  This helper keeps the one-line default and expands the traceback
only when the operator asked for it (``--debug`` or ``PDF2EPUB_DEBUG=1``).
"""
from __future__ import annotations

import os
import traceback
from pathlib import Path
from typing import Any, Optional

from loguru import logger


class CommandError(RuntimeError):
    """A command failed at a location that must be reported to the operator."""


class EpubBuildError(CommandError):
    """EPUB structural packaging failed and must not continue."""


class HtmlParseError(CommandError):
    """EPUB/HTML parsing failed and must not silently fall back."""


def _source_file_name(frame_file: str) -> str:
    """Return the bare module filename used in ``file:line`` locations."""
    return Path(str(frame_file)).name or "<unknown>"


def error_location(exc: BaseException, *, member: Optional[str] = None) -> str:
    """Return ``file:line:member`` for the innermost frame of ``exc``."""
    frames = traceback.extract_tb(exc.__traceback__)
    if frames:
        frame = frames[-1]
        location = f"{_source_file_name(frame.filename)}:{frame.lineno}"
    else:
        location = "<unknown>:0"
    return f"{location}:{member}" if member else location


def wrap_error(
    exc: BaseException,
    error_cls: type[CommandError] = CommandError,
    *,
    member: Optional[str] = None,
    action: str = "",
) -> CommandError:
    """Wrap ``exc`` with its ``file:line:member`` location and action."""
    location = error_location(exc, member=member)
    message = f"{location} {action}".rstrip()
    return error_cls(f"{message}: {type(exc).__name__}: {exc}")


def log_warn_only(
    exc: BaseException,
    *,
    member: Optional[str] = None,
    action: str,
) -> None:
    """Report a deliberate non-fatal fallback, explicitly marked warn-only."""
    location = error_location(exc, member=member)
    logger.warning(
        f"{location} {action} (warn_only=True): {type(exc).__name__}: {exc}"
    )


def located_reason(
    path: Any,
    *,
    line: int = 1,
    column: int = 1,
    reason: str,
    details: str = "",
    recovery: str = "",
) -> str:
    """Format an operator-facing ``posix:line:col reason`` message."""
    posix = Path(str(path)).as_posix()
    message = f"{posix}:{line}:{column} {reason}"
    if details:
        message += f"（{details}）"
    if recovery:
        message += f"（恢复：{recovery}）"
    return message


def debug_enabled(args: Any = None) -> bool:
    """Return True when the operator requested a traceback."""
    return bool(getattr(args, "debug", False)) or bool(os.environ.get("PDF2EPUB_DEBUG"))


def log_command_error(message: str, exc: BaseException, *, args: Any = None) -> None:
    """Report a caught exception, expanding it only in debug mode."""
    if debug_enabled(args):
        logger.opt(exception=exc).error(message)
        return
    logger.error(f"{message}: {type(exc).__name__}: {exc}")
    logger.info("Re-run with --debug (or PDF2EPUB_DEBUG=1) for a traceback.")


__all__ = [
    "CommandError",
    "EpubBuildError",
    "HtmlParseError",
    "debug_enabled",
    "error_location",
    "located_reason",
    "log_command_error",
    "log_warn_only",
    "wrap_error",
]
