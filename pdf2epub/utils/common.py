"""
Common utility functions used across multiple modules.
"""

import json
import time
import yaml
from pathlib import Path
from typing import Dict, Optional, Any, Union, Iterable, Sequence

from loguru import logger

from .config_manager import ConfigManager, load_config as _load_config_from_manager


def resolve_input_path(path: Optional[Union[str, Path]]) -> Optional[Path]:
    """
    Resolve an input file path, searching in input/ directory if not found directly.

    Args:
        path: Path string or Path object

    Returns:
        Resolved Path object or original Path if not found
    """
    if path is None:
        return None
    p = Path(path)
    if p.exists():
        return p
    # Check under input/ directory
    input_p = Path("input") / p
    if input_p.exists():
        return input_p
    input_name = Path("input") / p.name
    if input_name.exists():
        return input_name
    return p


def resolve_book_input_path(
    explicit_path: Optional[Union[str, Path]] = None,
    *,
    config_value: Optional[Union[str, Path]] = None,
    config_path: Optional[Union[str, Path]] = None,
    output_dir: Optional[Union[str, Path]] = None,
    input_dir: Optional[Union[str, Path]] = None,
    extensions: Iterable[str] = (),
    output_names: Sequence[str] = (),
) -> Path:
    """Resolve book input files using one consistent CLI/config priority.

    Priority is explicit CLI path, configured path, a unique file in ``input/``
    and finally a standard file in the output directory. Relative configured
    paths are resolved relative to the config file first, while explicit CLI
    paths retain the current-working-directory behavior for compatibility.
    """
    cwd = Path.cwd()
    config_parent = Path(config_path).expanduser().resolve().parent if config_path else cwd
    normalized_extensions = {
        ext.lower() if str(ext).startswith(".") else f".{str(ext).lower()}"
        for ext in extensions
    }

    def candidates_for(value, roots):
        if value is None:
            return []
        path = Path(value).expanduser()
        if path.is_absolute():
            return [path]
        candidates = [root / path for root in roots]
        candidates.append(cwd / "input" / path)
        candidates.append(cwd / "input" / path.name)
        return candidates

    def first_existing(candidates):
        seen = set()
        for candidate in candidates:
            candidate = candidate.resolve() if not candidate.is_absolute() else candidate
            key = str(candidate).lower()
            if key in seen:
                continue
            seen.add(key)
            if candidate.is_file():
                return candidate
        return None

    explicit_candidates = candidates_for(explicit_path, [cwd, config_parent])
    resolved = first_existing(explicit_candidates)
    if explicit_path is not None:
        return resolved or Path(explicit_candidates[0] if explicit_candidates else explicit_path)

    configured_candidates = candidates_for(config_value, [config_parent, cwd])
    resolved = first_existing(configured_candidates)
    if config_value is not None:
        return resolved or Path(configured_candidates[0] if configured_candidates else config_value)

    search_dirs = []
    if input_dir is not None:
        search_dirs.append(Path(input_dir))
    else:
        search_dirs.extend([config_parent / "input", cwd / "input"])
    unique_files = {}
    for directory in search_dirs:
        if not directory.is_dir():
            continue
        for candidate in directory.iterdir():
            if not candidate.is_file():
                continue
            if normalized_extensions and candidate.suffix.lower() not in normalized_extensions:
                continue
            unique_files[str(candidate.resolve()).lower()] = candidate
    if len(unique_files) == 1:
        return next(iter(unique_files.values()))
    if len(unique_files) > 1:
        names = ", ".join(sorted(path.name for path in unique_files.values()))
        raise ValueError(f"Multiple input files found; specify one explicitly: {names}")

    if output_dir:
        output_root = Path(output_dir)
        for name in output_names:
            candidate = output_root / name
            if candidate.is_file():
                return candidate
        if output_names:
            return output_root / output_names[0]
    return Path(output_names[0]) if output_names else Path()


def parse_llm_json(
    text: str,
    save_dir: Optional[Path] = None,
    operation_name: str = "LLM response"
) -> Any:
    """
    Parse JSON from LLM response with lenient settings.

    Uses strict=False to allow control characters that LLMs sometimes include.
    Saves raw response to file on parse failure for debugging.

    Args:
        text: JSON text to parse
        save_dir: Optional directory to save raw response on failure
        operation_name: Name for error logging

    Returns:
        Parsed JSON object

    Raises:
        json.JSONDecodeError: If parsing fails even with lenient settings
    """
    # Extract JSON from markdown code blocks (handles text before/after fences)
    text = text.strip()
    # Find opening fence (may not be at the start if LLM added explanation)
    json_fence = text.find("```json")
    plain_fence = text.find("```") if json_fence < 0 else json_fence
    if json_fence >= 0:
        text = text[json_fence + 7:]  # After ```json
    elif plain_fence >= 0:
        text = text[plain_fence + 3:]  # After ```
    # Find closing fence
    closing_fence = text.find("```")
    if closing_fence >= 0:
        text = text[:closing_fence]
    text = text.strip()

    try:
        return json.loads(text, strict=False)
    except json.JSONDecodeError as original_error:
        # Try heuristic repair before giving up
        try:
            from json_repair import repair_json
            repaired = repair_json(text, return_objects=True)
            if repaired is not None:
                logger.warning(
                    f"json_repair fixed invalid JSON from {operation_name} "
                    f"(original error: {original_error})"
                )
                # Save raw response for debugging
                if save_dir:
                    save_dir = Path(save_dir)
                    save_dir.mkdir(parents=True, exist_ok=True)
                    repair_file = save_dir / f"json_repaired_{int(time.time())}.txt"
                    repair_file.write_text(text, encoding='utf-8')
                    logger.debug(f"Pre-repair response saved to: {repair_file}")
                return repaired
        except Exception:
            pass  # repair also failed, fall through to original error

        logger.error(f"Failed to parse JSON from {operation_name}: {original_error}")
        # Log the actual response for debugging
        logger.error(f"Response length: {len(text)} chars")
        logger.error(f"Response preview: {text[:500]}...")

        if save_dir:
            save_dir = Path(save_dir)
            save_dir.mkdir(parents=True, exist_ok=True)
            error_file = save_dir / f"json_error_{int(time.time())}.txt"
            error_file.write_text(text, encoding='utf-8')
            logger.error(f"Raw response saved to: {error_file}")

        raise original_error

# Re-export for backward compatibility
def load_config(config_path: str = "config.yaml") -> Dict:
    """
    Load configuration from config file.

    Uses ConfigManager internally for migration and backward compatibility.

    Args:
        config_path: Path to the YAML config file

    Returns:
        Configuration dictionary (in legacy format for backward compatibility)
    """
    return _load_config_from_manager(config_path)


def get_config_manager(config_path: str = "config.yaml") -> ConfigManager:
    """
    Get a ConfigManager instance for advanced config access.

    Args:
        config_path: Path to the YAML config file

    Returns:
        ConfigManager instance
    """
    return ConfigManager(config_path)


def load_book_structure(book_title: str) -> Optional[Dict]:
    """
    Load the book structure JSON file.
    
    Args:
        book_title: Title of the book
        
    Returns:
        Book structure dictionary or None if not found
    """
    structure_path = Path("output") / book_title / "book_structure.json"
    if structure_path.exists():
        with open(structure_path, "r", encoding="utf-8") as file:
            structure = json.load(file)
        return structure
    return None


def ensure_directory(directory_path: Path) -> None:
    """
    Ensure a directory exists, create it if it doesn't.
    
    Args:
        directory_path: Path to the directory
    """
    Path(directory_path).mkdir(parents=True, exist_ok=True)


def sanitize_filename(filename: str) -> str:
    """
    Sanitize a filename for filesystem compatibility.
    
    Args:
        filename: The filename to sanitize
    
    Returns:
        Sanitized filename safe for filesystem use
    """
    return "".join(c for c in filename if c not in '<>:"/\\|?*')


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
