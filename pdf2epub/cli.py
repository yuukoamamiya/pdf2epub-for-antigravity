#!/usr/bin/env python3
"""
Unified CLI for pdf2epub markdown processing.

This module provides a single entrypoint for all markdown processing operations
including polishing OCR output and translating content.
"""

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any, Dict
from loguru import logger
from pdf2epub.utils.logging_config import configure_logging
from pdf2epub.utils.common import (
    book_output_dir,
    load_config,
    resolve_book_input_path,
    sanitize_filename,
)
from pdf2epub.utils.encoding import configure_utf8_stdio
from pdf2epub.commands.novel import (
    _convert_txt_to_xhtml,
    build_novel_epub_command,
    translate_novel_command,
    translate_novel_validate_command,
)
from pdf2epub.commands.tex import (
    translate_arxiv_command,
    translate_arxiv_validate_command,
)
from pdf2epub.commands.pdf import (
    _resolve_pdf_markdown_source,
    build_epub_command,
)
from pdf2epub.commands.entities import (
    _entity_context_is_current,
    _prepare_entity_subagent_task,
    extract_entities_command,
    extract_entities_validate_command,
)
from pdf2epub.commands.html import (
    _prepare_html_command,
    build_html_epub_command,
    html_prepare_command,
    html_validate_command,
)
from pdf2epub.commands.markdown import (
    _load_pdf_file_contexts,
    _load_pdf_file_roles,
    _prepare_pdf_markdown_task,
    _run_readiness_check,
    _validate_translation_entities,
    _validate_pdf_markdown_task,
    check_ready_command,
    polish_command,
    polish_validate_command,
    translate_command,
    translate_validate_command,
)
from pdf2epub.commands.ocr import ocr_pages_command
from pdf2epub.commands.refine import (
    refine_command,
    refine_local_command,
    refine_prepare_command,
)
from pdf2epub.commands.glossary import glossary_candidates_command
from pdf2epub.commands.registry import register_command_parsers
from pdf2epub.commands.toc import (
    translate_toc_command,
    translate_toc_validate_command,
)

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
        epilog="""
===============================================================================
RECOMMENDED WORKFLOW / 推荐工作流 (uses toc_tree.json):
===============================================================================

  # Complete pipeline for a PDF book:
  pdf2epub ocr-pages -i mybook.pdf   # Page-level OCR
  pdf2epub refine-prepare            # Prepare Subagent TOC analysis
  # Antigravity Subagent writes toc_tree.json
  pdf2epub refine-local              # Validate TOC and merge OCR pages locally
  pdf2epub polish                    # Prepare Subagent polishing task
  # Antigravity Subagent writes polished_markdown/*.md
  pdf2epub polish-validate
  pdf2epub build-epub                # Generate EPUB from validated output

  # With translation:
  pdf2epub ocr-pages -i mybook.pdf
  pdf2epub refine-prepare
  # Antigravity Subagent writes toc_tree.json
  pdf2epub refine-local
  pdf2epub polish --content-type japanese
  # Antigravity Subagent writes polished_markdown/*.md
  pdf2epub polish-validate
  pdf2epub extract-entities
  # Antigravity Subagent writes translation_entities.json
  pdf2epub extract-entities-validate
  pdf2epub translate --target-language Chinese
  # Antigravity Subagent writes translated/*.md and toc_tree_translated.json
  pdf2epub translate-validate
  pdf2epub build-epub --translated

  # Translate only the PDF directory tree:
  pdf2epub translate-toc
  # Antigravity Subagent writes toc_tree_translated.json
  pdf2epub translate-toc-validate

  # EPUB Translation (preserves original formatting):
  pdf2epub html-prepare -i mybook.epub       # Extract locally
  # Antigravity Subagent writes translated_compressed/* and translated_metadata.json
  pdf2epub html-validate
  pdf2epub build-html-epub                    # Build translated EPUB

  # Novel Translation (text mode for light novels):
  pdf2epub translate-novel -i mybook.epub     # Prepare Subagent task
  # Subagent writes translated_novel/* and translated_metadata.json
  pdf2epub translate-novel-validate
  pdf2epub build-novel-epub                   # Rebuild EPUB locally

  # arXiv/TeX Translation (compile-gated whole mode):
  pdf2epub translate-arxiv 2503.01800
  pdf2epub translate-arxiv ./latex-source --main-tex paper.tex

===============================================================================
        """
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
