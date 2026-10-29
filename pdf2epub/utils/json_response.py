"""Lenient JSON parsing for model responses."""
import json
import time
from pathlib import Path
from typing import Any, Optional
from loguru import logger

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
