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
        epilog="""
===============================================================================
RECOMMENDED WORKFLOW / 推荐工作流 (uses toc_tree.json):
===============================================================================

  # Complete pipeline for a PDF book:
  pdf2epub ocr-pages -i mybook.pdf   # Page-level OCR
  # For dual OCR, or single OCR when commentary anomalies are detected:
  pdf2epub ocr-correct
  # Antigravity Subagent writes ocr_corrected_pages/*.md
  pdf2epub ocr-correct-validate       # Skip both commands when single OCR has no anomaly pages
  pdf2epub refine-prepare            # Prepare Subagent TOC analysis
  # Antigravity Subagent writes toc_tree.json
  pdf2epub illustration-prepare      # Candidate report; no Subagent if there are no candidates
  # Antigravity Subagent writes illustration_decisions.json when candidates exist
  pdf2epub illustration-validate
  pdf2epub illustration-apply
  pdf2epub refine-local              # Validate TOC and merge OCR pages locally
  pdf2epub footnote-prepare          # Detect page-footnote candidates locally
  # Antigravity Subagent writes footnote_decisions.json when review is needed
  pdf2epub footnote-validate
  pdf2epub footnote-apply            # Move confirmed footnotes to chapter ends
  pdf2epub polish                    # Prepare Subagent polishing task
  # Antigravity Subagent writes polished_markdown/*.md
  pdf2epub polish-validate
  pdf2epub build-epub                # Generate EPUB from validated output

  # With translation:
  pdf2epub ocr-pages -i mybook.pdf
  # For dual OCR, or single OCR when commentary anomalies are detected:
  pdf2epub ocr-correct                # Skip both commands when single OCR has no anomaly pages
  # Antigravity Subagent writes ocr_corrected_pages/*.md
  pdf2epub ocr-correct-validate
  pdf2epub refine-prepare
  # Antigravity Subagent writes toc_tree.json
  pdf2epub illustration-prepare
  # Antigravity Subagent writes illustration_decisions.json when candidates exist
  pdf2epub illustration-validate
  pdf2epub illustration-apply
  pdf2epub refine-local
  pdf2epub footnote-prepare
  # Antigravity Subagent writes footnote_decisions.json when review is needed
  pdf2epub footnote-validate
  pdf2epub footnote-apply
  pdf2epub polish --content-type japanese
  # Antigravity Subagent writes polished_markdown/*.md
  pdf2epub polish-validate
  pdf2epub extract-entities
  # Antigravity Subagent writes translation_entities.json
  pdf2epub extract-entities-validate
  pdf2epub translate-toc
  # Antigravity Subagent writes toc_translation_result.json; local validation
  # merges it into toc_tree_translated.json
  pdf2epub translate-toc-validate
  pdf2epub translate --target-language Chinese
  # Antigravity Subagent writes translated/*.md; it must not modify TOC outputs
  pdf2epub translate-validate
  pdf2epub build-epub --translated

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
