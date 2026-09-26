"""Detect safe footnote bindings introduced by physical unit boundaries.

Refinement is allowed to split a logical unit at a page or Markdown block
boundary.  A reference can therefore end one generated file while its
definition starts the next one.  This module records only unambiguous,
adjacent bindings; it never edits the source Markdown and deliberately leaves
ambiguous cases for the normal EPUB footnote safety handling.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Iterable

from ..workflow_contracts import atomic_write_text


_DEFINITION_RE = re.compile(r"^\s*\[\^([A-Za-z0-9_-]+)\]:")
_REFERENCE_RE = re.compile(r"\[\^([A-Za-z0-9_-]+)\](?!:)")
_PAGE_NOTE_RE = re.compile(r"^\d+n(\d+)$")
_BOUNDARY_WINDOW_LINES = 20


def _canonical_key(key: str) -> str:
    match = _PAGE_NOTE_RE.match(key)
    return match.group(1) if match else key


def _scan_file(path: Path) -> dict[str, Any]:
    lines = path.read_text(encoding="utf-8").splitlines()
    definitions: list[dict[str, Any]] = []
    references: list[dict[str, Any]] = []
    definition_counts: dict[str, int] = {}

    for line_num, line in enumerate(lines, 1):
        definition = _DEFINITION_RE.match(line)
        if definition:
            key = _canonical_key(definition.group(1))
            definition_counts[key] = definition_counts.get(key, 0) + 1
            definitions.append(
                {
                    "key": key,
                    "line": line_num,
                    "occurrence_in_file": definition_counts[key],
                }
            )
            continue

        for reference in _REFERENCE_RE.finditer(line):
            key = _canonical_key(reference.group(1))
            # Reference and definition occurrences have separate namespaces;
            # recompute below so a definition earlier in a file does not shift
            # the reference occurrence count.
            references.append({"key": key, "line": line_num})

    reference_counts: dict[str, int] = {}
    for reference in references:
        key = reference["key"]
        reference_counts[key] = reference_counts.get(key, 0) + 1
        reference["occurrence_in_file"] = reference_counts[key]

    return {
        "name": path.name,
        "lines": len(lines),
        "definitions": definitions,
        "references": references,
    }


def scan_boundary_footnotes(
    markdown_dir: Path,
    ordered_files: Iterable[str],
    output_path: Path | None = None,
) -> dict[str, Any]:
    """Write and return a conservative boundary-footnote binding report.

    A binding is emitted only when exactly one reference for a key appears in
    the final 20 lines of a file, the key has no definition in that file, and
    exactly one matching definition appears in the first 20 lines of the
    immediately following file.  This makes the operation deterministic and
    safe to carry through the later polish/translation stages, where line
    numbers may change but occurrence order is preserved.
    """

    markdown_dir = Path(markdown_dir)
    files = [markdown_dir / str(name) for name in ordered_files]
    scans = [_scan_file(path) for path in files if path.is_file()]
    bindings: list[dict[str, Any]] = []

    for current, following in zip(scans, scans[1:]):
        current_refs = current["references"]
        current_defs = {item["key"] for item in current["definitions"]}
        tail_start = max(1, current["lines"] - _BOUNDARY_WINDOW_LINES + 1)
        tail_refs = [item for item in current_refs if item["line"] >= tail_start]
        following_defs = following["definitions"]
        head_limit = min(_BOUNDARY_WINDOW_LINES, following["lines"])
        head_defs = [item for item in following_defs if item["line"] <= head_limit]

        candidate_keys = {
            item["key"] for item in tail_refs if item["key"] not in current_defs
        }
        for key in sorted(candidate_keys):
            refs = [item for item in tail_refs if item["key"] == key]
            defs = [item for item in head_defs if item["key"] == key]
            all_following_defs = [item for item in following_defs if item["key"] == key]
            if len(refs) != 1 or len(defs) != 1 or len(all_following_defs) != 1:
                continue
            reference = refs[0]
            definition = defs[0]
            bindings.append(
                {
                    "key": key,
                    "reference_file": current["name"],
                    "reference_line": reference["line"],
                    "reference_occurrence_in_file": reference["occurrence_in_file"],
                    "definition_file": following["name"],
                    "definition_line": definition["line"],
                    "definition_occurrence_in_file": definition["occurrence_in_file"],
                    "confidence": "safe_adjacent_boundary",
                }
            )

    report = {
        "schema_version": 1,
        "source_dir": markdown_dir.name,
        "scanned_files": [path.name for path in files if path.is_file()],
        "bindings": bindings,
    }
    destination = Path(output_path) if output_path else markdown_dir / "footnote_boundary_bindings.json"
    atomic_write_text(destination, json.dumps(report, ensure_ascii=False, indent=2))
    return report


__all__ = ["scan_boundary_footnotes"]
