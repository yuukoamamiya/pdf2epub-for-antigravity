"""Operator-facing error reporting shared by command handlers.

Handlers catch expected failures and return a non-zero exit code.  Without a
traceback, a deep hash/contract error degrades to a single line that cannot be
diagnosed.  This helper keeps the one-line default and expands the traceback
only when the operator asked for it (``--debug`` or ``PDF2EPUB_DEBUG=1``).
"""
from __future__ import annotations

import os
from typing import Any

from loguru import logger


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


__all__ = ["debug_enabled", "log_command_error"]
