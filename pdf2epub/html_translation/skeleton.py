"""Protected-token masking for targeted HTML translation retries.

The normal HTML workflow keeps tags in the translation unit.  This module is
an opt-in fallback for a unit whose translated tag sequence failed validation:
tags/entities are replaced with exact placeholders, the Subagent translates
the surrounding sentence, and the local process restores the original tokens.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Dict, List, Tuple

from .validation import _PROTECTED_TOKEN_RE


_PLACEHOLDER_RE = re.compile(r"⟦HTML_(\d{4})⟧")


def mask_text(text: str) -> Tuple[str, Dict[str, Any]]:
    """Mask protected tokens while retaining line boundaries and context."""
    counter = 0
    lines: List[Dict[str, Any]] = []
    masked_lines: List[str] = []

    for line_number, line in enumerate(text.splitlines(keepends=True), 1):
        tokens: List[str] = []

        def replace(match: re.Match[str]) -> str:
            nonlocal counter
            counter += 1
            tokens.append(match.group(0))
            return f"⟦HTML_{counter:04d}⟧"

        masked_line = _PROTECTED_TOKEN_RE.sub(replace, line)
        masked_lines.append(masked_line)
        lines.append({"line": line_number, "tokens": tokens})

    return "".join(masked_lines), {
        "schema_version": 1,
        "placeholder_prefix": "HTML_",
        "lines": lines,
    }


def _line_placeholders(line: str) -> List[str]:
    return [f"⟦HTML_{number}⟧" for number in _PLACEHOLDER_RE.findall(line)]


def restore_text(masked_translation: str, contract: Dict[str, Any]) -> str:
    """Restore tokens after verifying every placeholder exactly once."""
    contract_lines = contract.get("lines")
    if not isinstance(contract_lines, list):
        raise ValueError("invalid skeleton contract: missing lines")

    translation_lines = masked_translation.splitlines(keepends=True)
    if len(translation_lines) != len(contract_lines):
        raise ValueError(
            "skeleton line count mismatch: "
            f"expected {len(contract_lines)}, got {len(translation_lines)}"
        )

    replacements: Dict[str, str] = {}
    expected_placeholders: List[str] = []
    expected_by_line: List[List[str]] = []
    for line_record in contract_lines:
        line_placeholders: List[str] = []
        for index, token in enumerate(line_record.get("tokens", [])):
            placeholder = f"⟦HTML_{len(expected_placeholders) + 1:04d}⟧"
            expected_placeholders.append(placeholder)
            line_placeholders.append(placeholder)
            replacements[placeholder] = token
        expected_by_line.append(line_placeholders)

    actual_by_line = [_line_placeholders(line) for line in translation_lines]
    if actual_by_line != expected_by_line:
        raise ValueError(
            "skeleton placeholder placement mismatch: "
            f"expected {expected_by_line}, got {actual_by_line}"
        )

    actual_placeholders = _PLACEHOLDER_RE.findall(masked_translation)
    actual_normalized = [f"⟦HTML_{number}⟧" for number in actual_placeholders]
    if actual_normalized != expected_placeholders:
        raise ValueError(
            "skeleton placeholder sequence mismatch: "
            f"expected {expected_placeholders}, got {actual_normalized}"
        )

    restored = masked_translation
    for placeholder in expected_placeholders:
        restored = restored.replace(placeholder, replacements[placeholder], 1)
    return restored


def write_masked_unit(source_path: Path, masked_path: Path, contract_path: Path) -> None:
    """Create a masked source unit and its immutable restore contract."""
    masked, contract = mask_text(Path(source_path).read_text(encoding="utf-8"))
    masked_path.parent.mkdir(parents=True, exist_ok=True)
    contract_path.parent.mkdir(parents=True, exist_ok=True)
    masked_path.write_text(masked, encoding="utf-8")
    contract_path.write_text(
        json.dumps(contract, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def restore_unit(
    translated_path: Path, contract_path: Path, output_path: Path
) -> None:
    """Restore one translated masked unit into the normal HTML target dir."""
    contract = json.loads(Path(contract_path).read_text(encoding="utf-8"))
    restored = restore_text(
        Path(translated_path).read_text(encoding="utf-8"), contract
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(restored, encoding="utf-8")
