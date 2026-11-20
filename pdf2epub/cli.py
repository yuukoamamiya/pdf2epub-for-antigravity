#!/usr/bin/env python3
"""
Unified CLI for pdf2epub markdown processing.

This module provides a single entrypoint for all markdown processing operations
including polishing OCR output and translating content.
"""

import argparse
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
    
    # Create subcommands
    subparsers = parser.add_subparsers(dest="command", help="Command to run")
    subparsers.required = True
    register_command_parsers(subparsers)
    # Parse arguments
    args = parser.parse_args()
    
    # Execute the appropriate command
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
