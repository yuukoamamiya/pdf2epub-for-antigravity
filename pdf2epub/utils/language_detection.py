"""Language detection for Markdown collections."""
from pathlib import Path
from loguru import logger

def guess_language(markdown_dir: Path) -> str:
    """
    Detect the primary language of the book content.

    Args:
        markdown_dir: Path to directory containing markdown files

    Returns:
        Language code (e.g., 'en', 'ja', 'zh-cn', 'zh-tw')
    """
    from langdetect import detect, LangDetectException
    from loguru import logger

    # Collect sample text from markdown files
    sample_text = ""
    sample_size = 0
    max_sample_size = 5000  # Characters to sample

    # Get all markdown files, sorted to ensure consistency
    markdown_files = sorted(markdown_dir.glob("*.md"))

    if not markdown_files:
        logger.warning(f"No markdown files found in {markdown_dir}")
        return "en"  # Default to English

    # Sample from multiple files to get better representation
    for md_file in markdown_files[:5]:  # Sample from first 5 files
        try:
            with open(md_file, "r", encoding="utf-8") as f:
                content = f.read()
                # Remove markdown syntax for cleaner detection
                import re
                # Remove headers, links, images, code blocks
                content = re.sub(r'^#{1,6}\s+', '', content, flags=re.MULTILINE)
                content = re.sub(r'\[([^\]]+)\]\([^\)]+\)', r'\1', content)
                content = re.sub(r'!\[([^\]]*)\]\([^\)]+\)', '', content)
                content = re.sub(r'```[^`]*```', '', content, flags=re.DOTALL)
                content = re.sub(r'`[^`]+`', '', content)
                # Remove footnotes
                content = re.sub(r'\[\^[^\]]+\]', '', content)
                content = re.sub(r'^\[\^[^\]]+\]:\s+.*$', '', content, flags=re.MULTILINE)

                sample_text += content[:1000] + " "
                sample_size += len(content)

                if sample_size >= max_sample_size:
                    break
        except Exception as e:
            logger.debug(f"Error reading {md_file}: {e}")
            continue

    if not sample_text.strip():
        logger.warning("No readable content found for language detection")
        return "en"

    try:
        # Detect language
        detected_lang = detect(sample_text)
        logger.info(f"Detected language: {detected_lang}")

        # Map common language codes to standard EPUB codes
        lang_mapping = {
            'en': 'en',
            'ja': 'ja',
            'zh-cn': 'zh-CN',  # Simplified Chinese
            'zh-tw': 'zh-TW',  # Traditional Chinese
            'ko': 'ko',
            'fr': 'fr',
            'de': 'de',
            'es': 'es',
            'it': 'it',
            'ru': 'ru',
            'pt': 'pt',
            'ar': 'ar',
            'hi': 'hi',
        }

        # Return mapped language or the detected one if not in mapping
        return lang_mapping.get(detected_lang, detected_lang)

    except LangDetectException as e:
        logger.warning(f"Language detection failed: {e}")
        return "en"  # Default to English on failure
