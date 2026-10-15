#!/usr/bin/env python3
"""
Unified CLI for pdf2epub markdown processing.

This module provides a single entrypoint for all markdown processing operations
including polishing OCR output and translating content.
"""

import argparse
from importlib import import_module
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


# A few older library callers imported workflow helpers from ``cli`` because
# the original entrypoint re-exported every command module.  Keep those names
# available lazily while removing the eager dependency on every workflow from
# normal CLI startup.  New code should import the owning module directly.
_LEGACY_EXPORTS = {
    "_convert_txt_to_xhtml": ("pdf2epub.commands.novel", "_convert_txt_to_xhtml"),
    "build_novel_epub_command": ("pdf2epub.commands.novel", "build_novel_epub_command"),
    "translate_novel_command": ("pdf2epub.commands.novel", "translate_novel_command"),
    "translate_novel_validate_command": ("pdf2epub.commands.novel", "translate_novel_validate_command"),
    "translate_arxiv_command": ("pdf2epub.commands.tex", "translate_arxiv_command"),
    "translate_arxiv_validate_command": ("pdf2epub.commands.tex", "translate_arxiv_validate_command"),
    "_resolve_pdf_markdown_source": ("pdf2epub.commands.pdf", "_resolve_pdf_markdown_source"),
    "build_epub_command": ("pdf2epub.commands.pdf", "build_epub_command"),
    "_entity_context_is_current": ("pdf2epub.commands.entities", "_entity_context_is_current"),
    "_prepare_entity_subagent_task": ("pdf2epub.commands.entities", "_prepare_entity_subagent_task"),
    "extract_entities_command": ("pdf2epub.commands.entities", "extract_entities_command"),
    "extract_entities_validate_command": ("pdf2epub.commands.entities", "extract_entities_validate_command"),
    "_prepare_html_command": ("pdf2epub.commands.html", "_prepare_html_command"),
    "build_html_epub_command": ("pdf2epub.commands.html", "build_html_epub_command"),
    "html_prepare_command": ("pdf2epub.commands.html", "html_prepare_command"),
    "html_validate_command": ("pdf2epub.commands.html", "html_validate_command"),
    "_load_pdf_file_contexts": ("pdf2epub.commands.markdown", "_load_pdf_file_contexts"),
    "_load_pdf_file_roles": ("pdf2epub.commands.markdown", "_load_pdf_file_roles"),
    "_prepare_pdf_markdown_task": ("pdf2epub.commands.markdown", "_prepare_pdf_markdown_task"),
    "_run_readiness_check": ("pdf2epub.commands.markdown", "_run_readiness_check"),
    "_validate_translation_entities": ("pdf2epub.commands.markdown", "_validate_translation_entities"),
    "_validate_pdf_markdown_task": ("pdf2epub.commands.markdown", "_validate_pdf_markdown_task"),
    "check_ready_command": ("pdf2epub.commands.markdown", "check_ready_command"),
    "polish_command": ("pdf2epub.commands.markdown", "polish_command"),
    "polish_validate_command": ("pdf2epub.commands.markdown", "polish_validate_command"),
    "translate_command": ("pdf2epub.commands.markdown", "translate_command"),
    "translate_validate_command": ("pdf2epub.commands.markdown", "translate_validate_command"),
    "ocr_pages_command": ("pdf2epub.commands.ocr", "ocr_pages_command"),
    "refine_command": ("pdf2epub.commands.refine", "refine_command"),
    "refine_local_command": ("pdf2epub.commands.refine", "refine_local_command"),
    "refine_prepare_command": ("pdf2epub.commands.refine", "refine_prepare_command"),
    "glossary_candidates_command": ("pdf2epub.commands.glossary", "glossary_candidates_command"),
    "translate_toc_command": ("pdf2epub.commands.toc", "translate_toc_command"),
    "translate_toc_validate_command": ("pdf2epub.commands.toc", "translate_toc_validate_command"),
}


def __getattr__(name: str):
    """Resolve legacy helper exports only when an old caller asks for them."""
    target = _LEGACY_EXPORTS.get(name)
    if target is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    module_name, attribute = target
    value = getattr(import_module(module_name), attribute)
    globals()[name] = value
    return value


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
  # For visual OCR with ocr.secondary.enabled=true:
  pdf2epub ocr-correct
  # Antigravity Subagent writes ocr_corrected_pages/*.md
  pdf2epub ocr-correct-validate       # Otherwise skip both correction commands
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
  # Only for visual OCR when ocr.secondary.enabled is true:
  pdf2epub ocr-correct                # Otherwise skip both correction commands
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
