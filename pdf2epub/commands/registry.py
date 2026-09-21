"""Argparse registration for the pdf2epub command groups."""

from pdf2epub.commands.entities import (
    extract_entities_command,
    extract_entities_validate_command,
)
from pdf2epub.commands.glossary import glossary_candidates_command
from pdf2epub.commands.html import (
    build_html_epub_command,
    html_prepare_command,
    html_skeleton_restore_command,
    html_skeleton_retry_command,
    html_validate_command,
)
from pdf2epub.commands.markdown import (
    check_ready_command,
    polish_command,
    polish_validate_command,
    translate_command,
    translate_validate_command,
)
from pdf2epub.commands.novel import (
    build_novel_epub_command,
    translate_novel_command,
    translate_novel_validate_command,
)
from pdf2epub.commands.ocr import ocr_pages_command
from pdf2epub.commands.pdf import build_epub_command
from pdf2epub.commands.refine import (
    refine_command,
    refine_local_command,
    refine_prepare_command,
)
from pdf2epub.commands.tex import (
    translate_arxiv_command,
    translate_arxiv_validate_command,
)
from pdf2epub.commands.toc import (
    translate_toc_command,
    translate_toc_validate_command,
)


def register_command_parsers(subparsers) -> None:
    """Register every workflow command on an argparse subparser collection."""

    check_ready_parser = subparsers.add_parser(
        "check-ready",
        help="Run deterministic pre-flight checks before translation or packaging",
    )
    check_ready_parser.add_argument(
        "--stage",
        choices=["translate", "package"],
        default="translate",
        help="Readiness stage to check (default: translate)",
    )
    check_ready_parser.add_argument(
        "--skip-entities",
        action="store_true",
        help="Explicitly skip the book-specific entity glossary gate",
    )
    check_ready_parser.set_defaults(func=check_ready_command)

    glossary_candidates_parser = subparsers.add_parser(
        "glossary-candidates",
        help="Scan local external glossary files and write a candidate report",
    )
    glossary_candidates_parser.add_argument(
        "--glossary-dir",
        help="Glossary directory (default: <config directory>/glossaries)",
    )
    glossary_candidates_parser.add_argument(
        "--source-language",
        help="Source language (default: from config or English)",
    )
    glossary_candidates_parser.add_argument(
        "--target-language",
        help="Target language (default: from config or Chinese)",
    )
    glossary_candidates_parser.set_defaults(func=glossary_candidates_command)

    # Breakdown subcommand (DEPRECATED)

    # OCR Pages subcommand (new workflow)
    ocr_pages_parser = subparsers.add_parser(
        "ocr-pages",
        help="Page-level OCR (for refined breakdown workflow)",
        description="Extract text from each PDF page individually for refined breakdown"
    )
    ocr_pages_parser.add_argument(
        "-i", "--input",
        help="Path to PDF file (default: auto-detect from output directory)"
    )
    ocr_pages_parser.add_argument(
        "--resume",
        action="store_true",
        help="Resume from previous progress"
    )
    ocr_pages_parser.add_argument(
        "--start-page",
        type=int,
        help="First page to process (default: 1)"
    )
    ocr_pages_parser.add_argument(
        "--end-page",
        type=int,
        help="Last page to process (default: all pages)"
    )
    ocr_pages_parser.add_argument(
        "--max-workers",
        type=int,
        default=None,
        help="Number of parallel OCR workers (default: from config or 5)"
    )
    ocr_pages_parser.set_defaults(func=ocr_pages_command)

    # Refine subcommand (refined breakdown with boundary verification)
    refine_parser = subparsers.add_parser(
        "refine",
        help="Prepare PDF structure analysis for an Antigravity Subagent",
        description=(
            "Alias for refine-prepare. Structure analysis is performed by a "
            "workspace Subagent; the local program never calls a translation API."
        )
    )
    refine_parser.add_argument(
        "--max-tokens",
        type=int,
        default=None,
        help="Maximum tokens per unit (default: from config or 8000)"
    )
    refine_parser.set_defaults(func=refine_command)

    # Antigravity Subagent refine workflow (no API calls in these commands)
    refine_prepare_parser = subparsers.add_parser(
        "refine-prepare",
        help="Prepare PDF TOC analysis for an Antigravity subagent",
        description=(
            "Write a prompt and manifest for a workspace subagent to inspect "
            "OCR pages and produce toc_tree.json."
        ),
    )
    refine_prepare_parser.add_argument(
        "--max-tokens",
        type=int,
        default=None,
        help="Maximum tokens per unit (default: from config or 8000)",
    )
    refine_prepare_parser.set_defaults(func=refine_prepare_command)

    refine_local_parser = subparsers.add_parser(
        "refine-local",
        help="Generate PDF work units from a subagent TOC (no API calls)",
        description=(
            "Validate toc_tree.json and deterministically merge OCR pages into "
            "ocr_markdown work units."
        ),
    )
    refine_local_parser.add_argument(
        "--resume",
        action="store_true",
        help="Resume from previously generated local units",
    )
    refine_local_parser.add_argument(
        "--max-tokens",
        type=int,
        default=None,
        help="Maximum tokens per unit (default: from config or 8000)",
    )
    refine_local_parser.set_defaults(func=refine_local_command)

    # Polish subcommand
    polish_parser = subparsers.add_parser(
        "polish",
        help="Prepare OCR Markdown for a polishing Subagent",
        description="Create a local Subagent hand-off; does not call an API",
    )
    polish_parser.add_argument(
        "--content-type",
        choices=["academic", "japanese", "general", "auto"],
        default="auto",
        help="Type of content to polish (default: auto-detect)"
    )
    polish_parser.add_argument(
        "--resume",
        action="store_true",
        help="Keep existing non-empty Subagent outputs and prepare only pending files",
    )
    polish_parser.set_defaults(func=polish_command)

    polish_validate_parser = subparsers.add_parser(
        "polish-validate",
        help="Validate and stage Subagent polishing output (no API calls)",
    )
    polish_validate_parser.set_defaults(func=polish_validate_command)

    # Translate subcommand
    translate_parser = subparsers.add_parser(
        "translate",
        help="Prepare polished Markdown for a translation Subagent",
        description="Create a local Subagent hand-off; does not call an API",
    )
    translate_parser.add_argument(
        "--source-language",
        help="Source language (default: from config or English)"
    )
    translate_parser.add_argument(
        "--target-language",
        help="Target language (default: from config or Chinese)"
    )
    translate_parser.add_argument(
        "--resume",
        action="store_true",
        help="Keep existing non-empty Subagent outputs and prepare only pending files",
    )
    translate_parser.add_argument(
        "--skip-entities",
        action="store_true",
        help="Skip the translation glossary gate for a genuinely non-terminological task",
    )
    translate_parser.set_defaults(func=translate_command)

    translate_toc_parser = subparsers.add_parser(
        "translate-toc",
        help="Prepare the PDF TOC translation Subagent task",
        description="Create the independent JSON TOC translation hand-off; no API calls",
    )
    translate_toc_parser.add_argument(
        "--source-language",
        help="Source language (default: from config or English)",
    )
    translate_toc_parser.add_argument(
        "--target-language",
        help="Target language (default: from config or Chinese)",
    )
    translate_toc_parser.set_defaults(func=translate_toc_command)

    translate_toc_validate_parser = subparsers.add_parser(
        "translate-toc-validate",
        help="Validate the translated PDF TOC JSON",
    )
    translate_toc_validate_parser.set_defaults(func=translate_toc_validate_command)

    translate_validate_parser = subparsers.add_parser(
        "translate-validate",
        help="Validate and stage Subagent translation output (no API calls)",
    )
    translate_validate_parser.add_argument(
        "--file",
        help=(
            "Validate one Markdown unit and update its resumable checkpoint; "
            "book-level gates still require a later full validation"
        ),
    )
    translate_validate_parser.add_argument(
        "--fix-reference-heading",
        action="store_true",
        help=(
            "Repair only a high-confidence extra Markdown heading for a plain "
            "end-of-book references label"
        ),
    )
    translate_validate_parser.set_defaults(func=translate_validate_command)

    # arXiv / TeX whole-mode translation
    translate_arxiv_parser = subparsers.add_parser(
        "translate-arxiv",
        help="Prepare an arXiv/local TeX project for a translation Subagent",
        description=(
            "Download or copy a TeX source tree and create a local Subagent "
            "hand-off; no translation API is called."
        ),
    )
    translate_arxiv_parser.add_argument(
        "source",
        help="arXiv ID/URL, local source directory, archive, or main .tex file",
    )
    translate_arxiv_parser.add_argument(
        "--main-tex",
        help="Compilation entry point relative to the source root (auto-detected)",
    )
    translate_arxiv_parser.add_argument(
        "--output-dir",
        help="Run directory (default: output/arxiv/<source-id>)",
    )
    translate_arxiv_parser.add_argument(
        "--source-language",
        help="Source language (default: English)",
    )
    translate_arxiv_parser.add_argument(
        "--target-language",
        help="Target language (default: Simplified Chinese)",
    )
    translate_arxiv_parser.add_argument(
        "--unit-chars",
        type=int,
        help="Approximate characters per TeX transaction (default: 12000)",
    )
    translate_arxiv_parser.add_argument(
        "--compile-timeout",
        type=int,
        help="Seconds allowed for each full-project compile (default: 180)",
    )
    translate_arxiv_parser.add_argument(
        "--resume",
        action="store_true",
        help="Keep existing translated TeX unit files and prepare only pending units",
    )
    translate_arxiv_parser.set_defaults(func=translate_arxiv_command)

    translate_arxiv_validate_parser = subparsers.add_parser(
        "translate-arxiv-validate",
        help="Compile a Subagent-edited TeX project locally (no API calls)",
    )
    translate_arxiv_validate_parser.add_argument(
        "--output-dir", required=True, help="Run directory created by translate-arxiv"
    )
    translate_arxiv_validate_parser.add_argument(
        "--compile-timeout", type=int, help="XeLaTeX timeout in seconds (default: 180)"
    )
    translate_arxiv_validate_parser.set_defaults(func=translate_arxiv_validate_command)

    # Entity extraction subcommand
    entity_parser = subparsers.add_parser(
        "extract-entities",
        help="Prepare entity extraction for a translation Subagent",
        description=(
            "Create a local Subagent hand-off for extracting characters, "
            "places, and terms; no model API is called."
        )
    )
    entity_parser.add_argument(
        "-i", "--input",
        default=None,  # Will be resolved to book_folder/input.pdf in the command
        help="Path to input PDF file (default: output/<book_title>/input.pdf)"
    )
    entity_parser.add_argument(
        "--source-lang",
        default=None,
        help="Source language (default: from config or English)"
    )
    entity_parser.add_argument(
        "--target-lang",
        default=None,
        help="Target language (default: from config or Chinese)"
    )
    entity_parser.set_defaults(func=extract_entities_command)

    entity_validate_parser = subparsers.add_parser(
        "extract-entities-validate",
        help="Validate entity JSON written by a Subagent",
        description="Validate the optional translation entity hand-off locally.",
    )
    entity_validate_parser.set_defaults(func=extract_entities_validate_command)

    # Build EPUB subcommand (toc_tree.json driven - new approach)
    build_epub_parser = subparsers.add_parser(
        "build-epub",
        help="Build EPUB from toc_tree.json structure (recommended)",
        description="Create EPUB file using toc_tree.json as the structure authority"
    )
    build_epub_parser.add_argument(
        "--translated",
        action="store_true",
        help="Build EPUB from translated markdown instead of polished"
    )
    build_epub_parser.add_argument(
        "--cover",
        help="Path to cover image file"
    )
    build_epub_parser.set_defaults(func=build_epub_command)

    # HTML Prepare subcommand (pure local extraction and compression)
    html_prepare_parser = subparsers.add_parser(
        "html-prepare",
        help="Extract and prepare compressed HTML units from EPUB (pure local, no LLM)",
        description="Extract XHTML from EPUB and compress into units for translation."
    )
    html_prepare_parser.add_argument(
        "-i", "--input",
        help="Input file: EPUB, AZW3, or MOBI (default: output/<book_title>/input.epub)"
    )
    html_prepare_parser.add_argument(
        "--source-language",
        help="Source language (default: from EPUB metadata or Japanese)"
    )
    html_prepare_parser.add_argument(
        "--target-language",
        help="Target language (default: from config or Chinese)"
    )
    html_prepare_parser.add_argument(
        "--resume",
        action="store_true",
        help="Keep existing translated HTML units and prepare only pending files",
    )
    html_prepare_parser.add_argument(
        "--skip-entities",
        action="store_true",
        help="Skip EPUB book-wide terminology extraction (external glossaries still apply)",
    )
    html_prepare_parser.set_defaults(func=html_prepare_command)

    # HTML Validate subcommand (pure local validation)
    html_validate_parser = subparsers.add_parser(
        "html-validate",
        help="Validate translated compressed units against originals (pure local, no LLM)",
        description="Validate that all units are translated with matching line counts and intact HTML tag structures."
    )
    html_validate_parser.add_argument(
        "-i", "--input",
        help="Input file: EPUB, AZW3, or MOBI (default: output/<book_title>/input.epub)"
    )
    html_validate_parser.add_argument(
        "--file",
        help="Validate one compressed translation unit only (for example 20_Chapter1.md); skips book-level checks",
    )
    html_validate_parser.set_defaults(func=html_validate_command)

    html_skeleton_retry_parser = subparsers.add_parser(
        "html-skeleton-retry",
        help="Prepare one complex HTML unit for protected-token retry translation",
    )
    html_skeleton_retry_parser.add_argument("--file", required=True)
    html_skeleton_retry_parser.add_argument("--resume", action="store_true")
    html_skeleton_retry_parser.set_defaults(func=html_skeleton_retry_command)

    html_skeleton_restore_parser = subparsers.add_parser(
        "html-skeleton-restore",
        help="Restore a completed protected-token HTML retry into the normal target",
    )
    html_skeleton_restore_parser.add_argument("--file", required=True)
    html_skeleton_restore_parser.set_defaults(func=html_skeleton_restore_command)

    # Build HTML EPUB subcommand (rebuild EPUB with translated HTML)
    build_html_epub_parser = subparsers.add_parser(
        "build-html-epub",
        help="Build EPUB from translated HTML (preserves original formatting)",
        description="Rebuild EPUB by replacing XHTML content with translations. AZW3/MOBI auto-converted."
    )
    build_html_epub_parser.add_argument(
        "-i", "--input",
        help="Input file: EPUB, AZW3, or MOBI (default: output/<book_title>/input.epub)"
    )
    build_html_epub_parser.add_argument(
        "-o", "--output",
        help="Path to output EPUB file (default: <book_title>_translated.epub)"
    )
    build_html_epub_parser.add_argument(
        "--allow-partial",
        action="store_true",
        help="Build despite missing/invalid units or metadata (unsafe; for previews only)",
    )
    build_html_epub_parser.set_defaults(func=build_html_epub_command)

    # Novel Translation subcommand (text-mode for light novels)
    translate_novel_parser = subparsers.add_parser(
        "translate-novel",
        help="Prepare light-novel EPUB for a translation Subagent",
        description="Extract light-novel text and create a local Subagent hand-off."
    )
    translate_novel_parser.add_argument(
        "-i", "--input",
        help="Input EPUB file"
    )
    translate_novel_parser.add_argument(
        "--source-language",
        help="Source language (default: Japanese)"
    )
    translate_novel_parser.add_argument(
        "--target-language",
        help="Target language (default: from config or Chinese)"
    )
    translate_novel_parser.add_argument(
        "--resume",
        action="store_true",
        help="Keep existing translated novel units and prepare only pending files",
    )
    translate_novel_parser.set_defaults(func=translate_novel_command)

    translate_novel_validate_parser = subparsers.add_parser(
        "translate-novel-validate",
        help="Validate Subagent light-novel output (no API calls)",
    )
    translate_novel_validate_parser.set_defaults(func=translate_novel_validate_command)

    # Build Novel EPUB subcommand (rebuild from translated text, no re-translation)
    build_novel_epub_parser = subparsers.add_parser(
        "build-novel-epub",
        help="Build EPUB from validated Subagent novel text",
        description="Rebuild EPUB only after translate-novel-validate succeeds."
    )
    build_novel_epub_parser.set_defaults(func=build_novel_epub_command)

__all__ = ["register_command_parsers"]
