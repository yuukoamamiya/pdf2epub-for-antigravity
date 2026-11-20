#!/usr/bin/env python3
"""
Unified CLI for pdf2epub markdown processing.

This module provides a single entrypoint for all markdown processing operations
including polishing OCR output and translating content.
"""

import argparse
import os
import sys

from pdf2epub.utils.encoding import configure_utf8_stdio
from pdf2epub.commands.registry import register_command_parsers
from pdf2epub.workflow_stages import render_cli_epilog
from pdf2epub.utils.logging_config import configure_logging

# Windows consoles may still default to an active code page such as GBK.
# Configure before loguru captures stderr so international filenames cannot
# abort an otherwise successful CLI command.
configure_utf8_stdio()

# Configure logger
logger = configure_logging()


def main():
    """Main CLI entrypoint."""
    parser = argparse.ArgumentParser(
        description="PDF to EPUB markdown processor",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=render_cli_epilog(),
    )
    
    # Global arguments
    parser.add_argument("-c", "--config", default="config.yaml", 
                        help="Path to config file")
    parser.add_argument(
        "--debug",
        action="store_true",
        help=(
            "Print the full traceback for unexpected errors instead of a "
            "one-line message (PDF2EPUB_DEBUG=1 has the same effect)"
        ),
    )
    
    # Create subcommands
    subparsers = parser.add_subparsers(dest="command", help="Command to run")
    subparsers.required = True
    register_command_parsers(subparsers)
    # Parse arguments
    args = parser.parse_args()
    
    # Execute the appropriate command
    return run_command(args)


def run_command(args) -> int:
    """Run one command handler with a single operator-facing error boundary.

    Handlers already return non-zero exit codes for expected gate failures.
    An unexpected exception used to escape as a raw traceback; now it is
    reported as one line and only expanded with ``--debug``.
    """
    try:
        return args.func(args)
    except KeyboardInterrupt:
        logger.error("Interrupted")
        return 130
    except SystemExit:
        raise
    except Exception as exc:  # noqa: BLE001 - top-level CLI boundary
        if getattr(args, "debug", False) or os.environ.get("PDF2EPUB_DEBUG"):
            logger.opt(exception=exc).error("Unhandled error")
        else:
            logger.error(f"{type(exc).__name__}: {exc}")
            logger.info(
                "Re-run with --debug (or PDF2EPUB_DEBUG=1) for a traceback."
            )
        return 1


if __name__ == "__main__":
    sys.exit(main())
