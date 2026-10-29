"""Compatibility imports for legacy callers. Production code imports owners directly."""
from pdf2epub.pipeline_policy import PipelinePolicy
from pdf2epub.utils.paths import book_output_dir, sanitize_filename
from pdf2epub.utils.input_paths import resolve_book_input_path, resolve_input_path
from pdf2epub.utils.json_response import parse_llm_json
from pdf2epub.utils.configuration import load_config
from pdf2epub.utils.filesystem import ensure_directory
from pdf2epub.utils.language_detection import guess_language

def is_epub_conversion_pipeline(config):
    """Compatibility wrapper; prefer PipelinePolicy at production call sites."""
    return PipelinePolicy.from_config(config).is_conversion

__all__ = [
    "book_output_dir", "ensure_directory", "guess_language",
    "is_epub_conversion_pipeline", "load_config", "parse_llm_json",
    "resolve_book_input_path", "resolve_input_path", "sanitize_filename",
]
