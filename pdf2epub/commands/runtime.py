"""Shared runtime context for command handlers.

Command modules still own their workflow-specific decisions, but configuration
loading, book-title validation, output-directory resolution, and file logging
are handled consistently here.
"""

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from loguru import logger

from pdf2epub.utils.common import book_output_dir, load_config
from pdf2epub.utils.logging_config import configure_logging


@dataclass(frozen=True)
class BookCommandContext:
    """Configuration and filesystem context shared by book commands."""

    config: dict
    config_path: Path
    book_title: str
    output_dir: Path


def load_command_config(args: Any) -> tuple[dict, Path]:
    """Load a command's config and return it with its resolved path."""
    config_path = Path(getattr(args, "config", "config.yaml")).expanduser()
    return load_config(config_path), config_path


def load_book_context(
    args: Any,
    operation: str,
    *,
    title: Optional[str] = None,
) -> Optional[BookCommandContext]:
    """Load config, require a title, and initialize operation logging.

    Commands that infer a title from an input file should keep that specialized
    behavior and pass the inferred value through ``title``.
    """
    config, config_path = load_command_config(args)
    book_title = title or config.get("title")
    if not book_title:
        logger.error("No title found in config.yaml")
        return None
    configure_logging(book_title, operation)
    return BookCommandContext(
        config=config,
        config_path=config_path,
        book_title=book_title,
        output_dir=book_output_dir(book_title),
    )


__all__ = ["BookCommandContext", "load_book_context", "load_command_config"]
