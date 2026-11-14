"""Pure result audit for footnote preparation and normalization.

The audit reports suspicious residual markers and note-boundary anomalies without
choosing their semantics. Every review exception is bound to an exact file block
hash so a later source edit cannot silently reuse it.
"""
from __future__ import annotations

import hashlib
import re
from collections import defaultdict
from typing import Any, Mapping, Sequence

from ..footnote_markdown import collect_footnote_definition_blocks
from .endnote_review import _inside_inline_code_or_math
from .endnote_scopes import markdown_fence_mask
from .footnote_structure import FOOTNOTE_STRUCTURE_EXEMPT_TYPES

AUDIT_VERSION = 1
AUDIT_FILENAME = "footnote_result_audit.json"

_HTML_COMMENT_RE = re.compile(r"(?s)<!--.*?-->")
_HTML_CODE_BLOCK_RE = re.compile(r"(?is)<(?:pre|code)\b[^>]*>.*?</(?:pre|code)\s*>")
_RAW_SUP_RE = re.compile(r"<sup\b[^>]*>\s*(?P<key>\d+)\s*</sup>", re.I)
_UNICODE_SUP_RE = re.compile(r"[⁰¹²³⁴⁵⁶⁷⁸⁹]+")
_BRACKET_REF_RE = re.compile(r"(?<!\!)\[(?P<key>\d{1,3})\](?!\s*\()")
_RAW_DEF_RE = re.compile(r"^\s{0,3}(?:[-*+]\s+)?(?:\[(?P<bracket>\d{1,3})\]|(?P<num>\d{1,3})[.)])\s+(?P<body>\S.*)$")
_DIGIT_MAP = str.maketrans("⁰¹²³⁴⁵⁶⁷⁸⁹", "0123456789")


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _visible(text: str) -> str:
    lines = text.splitlines()
    fenced = markdown_fence_mask(lines)
    text = "\n".join("" if fenced[index] else line for index, line in enumerate(lines))
    text = _HTML_CODE_BLOCK_RE.sub(lambda match: "\n" * match.group(0).count("\n"), text)
    text = _HTML_COMMENT_RE.sub(lambda m: "\n" * m.group(0).count("\n"), text)
    visible_lines = []
    for line in text.splitlines():
        chars = list(line)
        spans: list[tuple[int, int]] = []
        for delimiter_re in (
            re.compile(r"(?<!\\)(`+)(?!`)"),
            re.compile(r"(?<!\\)(\$\$|(?<!\$)\$(?!\$))"),
        ):
            opened: tuple[str, int] | None = None
            for match in delimiter_re.finditer(line):
                delimiter = match.group(1)
                if opened is None:
                    opened = (delimiter, match.end())
                elif opened[0] == delimiter:
                    spans.append((opened[1], match.start()))
                    opened = None
        for start, end in spans:
            chars[start:end] = [" "] * (end - start)
        visible_lines.append("".join(chars))
    return "\n".join(visible_lines)


def _task_id(code: str, file: str, line: int, evidence_hash: str, identity: str = "") -> str:
    # Physical line numbers are presentation metadata only; inserting text
    # earlier in a part must not invalidate a still-current source-block task.
    payload = f"{AUDIT_VERSION}\0{code}\0{file}\0{evidence_hash}\0{identity}"
    return "fra-" + hashlib.sha256(payload.encode("utf-8")).hexdigest()[:20]


def _finding(code: str, file: str, line: int, block: str, *, detail: str, page: int | None = None, key: str | None = None, origin: str | None = None, identity: str = "") -> dict[str, Any]:
    evidence_hash = _sha(block)
    return {
        "task_id": _task_id(code, file, line, evidence_hash, identity),
        "code": code,
        "file": file,
        "line": line,
        "page": page,
        "key": key,
        "evidence_origin": origin,
        "evidence_sha256": evidence_hash,
        "evidence_excerpt": block[:280],
        "detail": detail,
        "requires_visual_review": True,
    }


def _line_is_citation(line: str) -> bool:
    low = line.lower()
    return bool(re.search(r"\b(?:doi:|isbn|issn|pp?\.|vol\.|no\.|edition|auf(?:lage)?|cited in|citations?:)\b", low))


def _in_markdown_definition(line_number: int, definition_lines: set[int]) -> bool:
    return line_number in definition_lines


def _alnum(text: str) -> str:
    return "".join(char.casefold() for char in text if char.isalnum())


def _is_in_inline_code(line: str, position: int) -> bool:
    return _inside_inline_code_or_math(line, position)


def audit_footnote_result(
    source_texts: Mapping[str, str],
    *,
    records: Sequence[Mapping[str, Any]] = (),
    target_texts: Mapping[str, str] | None = None,
    mode: str = "footnotes",
    decisions: Sequence[Mapping[str, Any]] = (),
    expected_notes: Sequence[Mapping[str, Any]] = (),
    current_exceptions: Sequence[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    """Audit source and optional normalized output; never makes semantic choices.

    ``current_exceptions`` contains only exact task-id/evidence-hash review
    decisions. Changed source blocks therefore reappear as new tasks.
    """
    mode = str(mode or "footnotes").strip().lower()
    exceptions = {
        (str(item.get("task_id")), str(item.get("evidence_sha256")))
        for item in current_exceptions if isinstance(item, Mapping)
        and item.get("outcome") in {"legitimate_non_footnote", "truncated_source_exception"}
    }
    exceptions.update(
        (str(item.get("source_task_id")), str(item.get("source_evidence_sha256")))
        for item in current_exceptions if isinstance(item, Mapping)
        and item.get("outcome") in {"legitimate_non_footnote", "truncated_source_exception"}
        and item.get("source_task_id") and item.get("source_evidence_sha256")
    )
    record_by_file = {str(item.get("name")): item for item in records if item.get("name")}
    record_order = {str(item.get("name")): index for index, item in enumerate(records) if item.get("name")}
    reviewed_body_keys: dict[str, set[str]] = defaultdict(set)
    for item in decisions:
        if isinstance(item, Mapping) and item.get("role") == "body" and item.get("key"):
            record = record_by_file.get(str(item.get("file") or ""), {})
            scope = str(record.get("chapter_id") or item.get("chapter_id") or "")
            if scope:
                reviewed_body_keys[scope].add(str(item.get("key")))
    findings: list[dict[str, Any]] = []
    suppressed: list[dict[str, str]] = []
    resolved: list[dict[str, str]] = []
    target_definition_rows: dict[str, list[tuple[str, str]]] = defaultdict(list)
    if target_texts is not None:
        for target_file, target in target_texts.items():
            target_lines = _visible(target).splitlines()
            target_defs, _ = collect_footnote_definition_blocks(target_lines)
            for _, (key, body) in target_defs.items():
                target_definition_rows[str(target_file)].append((str(key), body))

    def record_finding(finding: dict[str, Any]) -> None:
        identity = (finding["task_id"], finding["evidence_sha256"])
        if identity in exceptions:
            suppressed.append({"task_id": finding["task_id"], "evidence_sha256": finding["evidence_sha256"]})
        else:
            findings.append(finding)

    if mode == "disabled":
        return {
            "schema_version": AUDIT_VERSION,
            "status": "not_applicable",
            "valid": True,
            "mode": mode, "findings": [], "suppressed": [], "source_sha256": {k: _sha(v) for k, v in sorted(source_texts.items())},
        }

    # Raw markers in body text are suspicious only when not in code/comments,
    # markdown definitions, obvious math, or explicit bibliographic contexts.
    ordered_definitions: dict[str, list[tuple[int, str, int, int, int, int, int, str]]] = defaultdict(list)
    open_definitions: dict[str, list[tuple[str, str, str, int, int]]] = defaultdict(list)
    lowercase_body_starts: dict[str, list[tuple[str, int, str, int]]] = defaultdict(list)
    source_by_scope: dict[str, str] = defaultdict(str)
    for file, raw in sorted(source_texts.items()):
        text = _visible(raw)
        lines = text.splitlines()
        definitions, consumed = collect_footnote_definition_blocks(lines)
        definition_lines = {line + 1 for line in definitions}
        definition_lines.update(line + 1 for line in consumed)
        record = record_by_file.get(file, {})
        scope = str(record.get("chapter_id") or file)
        scan_source_markers = (
            target_texts is None
            and mode != "endnotes_only"
            and str(record.get("type") or "") not in FOOTNOTE_STRUCTURE_EXEMPT_TYPES
        )
        part_index = int(record.get("part_index") or 1)
        source_by_scope[scope] += "\n" + text
        for line_index, (key, body) in definitions.items():
            if str(key).isdigit():
                ordered_definitions[scope].append((
                    int(key), file, line_index + 1, part_index,
                    int(record.get("start_page") or 0), int(record.get("end_page") or 0),
                    record_order.get(file, 10**9), str(record.get("section_id") or ""),
                ))
            if re.search(r"[A-Za-zÀ-ÖØ-öø-ÿ]{2,}[-‐‑]$", body.strip()):
                prefix = re.search(r"([A-Za-zÀ-ÖØ-öø-ÿ]{2,})[-‐‑]$", body.strip())
                if prefix:
                    open_definitions[scope].append((str(key), file, prefix.group(1), part_index, line_index + 1))
        for number, line in enumerate(lines, 1):
            if _in_markdown_definition(number, definition_lines):
                continue
            equation_text = re.sub(r"<[^>]+>", " ", line)
            equation_like = (
                len(list(_RAW_SUP_RE.finditer(line))) + len(list(_UNICODE_SUP_RE.finditer(line))) >= 2
                and bool(re.search(r"(?:=|[+*/])", equation_text))
            )
            # Skip obvious math/formula and edition superscripts.
            scope = str(record_by_file.get(file, {}).get("chapter_id") or file)
            # At apply time the raw source is expected to still contain the
            # OCR markers that the edit plan just consumed. Residual-marker
            # checks must inspect the materialized target instead; scanning
            # the input here falsely reports successfully applied notes.
            for occurrence, match in enumerate((_RAW_SUP_RE.finditer(line) if scan_source_markers else ())):
                if equation_like:
                    continue
                before, after = line[:match.start()], line[match.end():]
                key = match.group("key")
                scope = str(record_by_file.get(file, {}).get("chapter_id") or file)
                if _is_in_inline_code(line, match.start()):
                    continue
                # A leading variable-like token alone is not enough to call
                # this an exponent. Only explicit equation/operator notation
                # is automatically exempt; ambiguous cases go to visual review.
                if re.search(r"(?:=|\+|\-|\*|/|\^)\s*[A-Za-z]$", before):
                    continue
                if key in reviewed_body_keys.get(scope, set()):
                    resolved.append({"task_id": _task_id("unexplained_numeric_superscript", file, number, _sha(line.strip())), "evidence_sha256": _sha(line.strip()), "resolution": "image_confirmed_body_candidate"})
                    continue
                if re.match(r"\s*\d{4}", after) and re.search(r"\b\d{4},?\s*$", before):
                    continue
                finding = _finding("unexplained_numeric_superscript", file, number, line.strip(), detail=f"Raw superscript {key} remains in body text.", key=key, identity=f"key={key};occurrence={occurrence}")
                record_finding(finding)
            for occurrence, match in enumerate((_UNICODE_SUP_RE.finditer(line) if scan_source_markers else ())):
                if equation_like:
                    continue
                before, after = line[:match.start()], line[match.end():]
                key = match.group(0).translate(_DIGIT_MAP)
                if _is_in_inline_code(line, match.start()):
                    continue
                if re.match(r"\s*\d{4}", after):
                    continue
                finding = _finding("unexplained_unicode_superscript", file, number, line.strip(), detail=f"Unicode superscript {key} remains in body text.", key=key, identity=f"key={key};occurrence={occurrence}")
                record_finding(finding)
            for occurrence, match in enumerate((_BRACKET_REF_RE.finditer(line) if scan_source_markers else ())):
                if _is_in_inline_code(line, match.start()):
                    continue
                # [n] is a common scholarly citation form. Only route it to
                # visual review when the same line explicitly looks like a
                # note cue; standalone bracket citations remain untouched.
                if not re.search(r"\b(?:footnote|note|see note|cf\. note)\b", line, re.I):
                    continue
                if _line_is_citation(line) or re.search(r"\b(?:table|figure|fig\.|chapter|section)\s*$", line[:match.start()], re.I):
                    continue
                key = match.group("key")
                finding = _finding("unexplained_bracketed_numeric_reference", file, number, line.strip(), detail=f"Bracketed numeric reference [{key}] remains unclassified.", key=key, identity=f"key={key};occurrence={occurrence}")
                record_finding(finding)
            raw_def = _RAW_DEF_RE.match(line) if scan_source_markers else None
            if (
                raw_def
                and str(record_by_file.get(file, {}).get("type") or "") not in FOOTNOTE_STRUCTURE_EXEMPT_TYPES
                and not _is_in_inline_code(line, raw_def.start())
                and re.search(r"\b(?:note|notes|see note|cf\.|ibid\.|trans\.|ed\.)\b", raw_def.group("body"), re.I)
            ):
                key = raw_def.group("bracket") or raw_def.group("num")
                finding = _finding("suspected_numbered_note_definition", file, number, line.strip(), detail=f"Numbered block {key} resembles a note definition but is not a Markdown footnote.")
                record_finding(finding)
            stripped_line = line.strip()
            if stripped_line and stripped_line[0].islower() and re.match(r"[a-zà-öø-ÿ]{3,}\b", stripped_line):
                lowercase_body_starts[scope].append((file, number, stripped_line, part_index))

        # Exact broken word suffixes and likely truncated note definitions need
        # visual review; punctuation/wording alone never auto-repairs them.
        for line_index, (key, body) in definitions.items():
            stripped = body.strip()
            open_paren = stripped.count("(") - stripped.count(")")
            open_bracket = stripped.count("[") - stripped.count("]")
            if stripped.endswith(("-", "‐", "‑", "—", "…")) or open_paren > 0 or open_bracket > 0:
                finding = _finding("normalized_note_truncated", file, 0, f"[^{key}]: {body}", detail=f"Footnote definition {key} may be truncated, unclosed, or require a continuation.", key=key, origin="source_markdown")
                record_finding(finding)

    # A note definition ending with a split word plus a lower-case body start
    # elsewhere in the same established chapter-part scope is a possible
    # cross-page body/note seam. Require the rejoined token to occur in source
    # text; this is structural corroboration, not a language dictionary.
    for scope, definitions in open_definitions.items():
        scope_text = _alnum(source_by_scope.get(scope, ""))
        for key, _definition_file, prefix, definition_part, _definition_line in definitions:
            for body_file, body_line, body_text, body_part in lowercase_body_starts.get(scope, []):
                first = re.match(r"([a-zà-öø-ÿ]{3,})\b", body_text)
                if not first or abs(body_part - definition_part) > 1:
                    continue
                joined = _alnum(prefix + first.group(1))
                if len(joined) < 6 or joined not in scope_text:
                    continue
                record_finding(_finding(
                    "cross_page_body_note_boundary", body_file, body_line,
                    body_text,
                    detail=f"Lowercase fragment may continue note {key} ending with {prefix}- across adjacent parts; inspect both page images.",
                    key=key,
                ))

    # Detect only interior gaps in a forward-running numeric sequence. A
    # decrease is a numbering reset (common at section boundaries), so start
    # a new segment and never infer missing notes across it. This applies to
    # single-file and multipart chapters alike.
    for scope, sequence in (ordered_definitions.items() if mode != "endnotes_only" else ()):
        sequence.sort(key=lambda item: (int(item[3]), item[6], item[2]))
        for previous, current in zip(sequence, sequence[1:]):
            previous_key, previous_file, previous_line, previous_part, previous_start, previous_end, previous_order, previous_section = previous
            current_key, current_file, current_line, current_part, current_start, current_end, current_order, current_section = current
            if current_key == previous_key:  # duplicate key is not a sequence gap
                continue
            if current_key < previous_key:
                page_bounded_reset = (
                    previous_order != current_order
                    and previous_end > 0 and current_start > 0
                    and current_start > previous_end
                )
                section_bounded_reset = bool(previous_section and current_section and previous_section != current_section)
                if page_bounded_reset or section_bounded_reset:
                    continue
                finding = _finding(
                    "scoped_numbering_reset_uncertain", current_file, current_line,
                    f"scope={scope}; previous={previous_key}@{previous_file}:{previous_line}; next={current_key}@{current_file}:{current_line}",
                    detail=(
                        f"Numeric note sequence decreases from {previous_key} to {current_key}, "
                        "but no exact page or section boundary confirms a numbering reset. "
                        "Review the adjacent note images; no missing key is inferred."
                    ),
                    origin="source_markdown",
                    identity=f"previous={previous_key};current={current_key};reset",
                )
                finding["neighbor_keys"] = [previous_key, current_key]
                finding["neighbor_files"] = [previous_file, current_file]
                if (finding["task_id"], finding["evidence_sha256"]) not in exceptions:
                    findings.append(finding)
                else:
                    suppressed.append({"task_id": finding["task_id"], "evidence_sha256": finding["evidence_sha256"]})
                continue
            if current_key - previous_key <= 1:
                continue
            missing = list(range(previous_key + 1, current_key))
            evidence = (
                f"scope={scope}; previous={previous_key}@{previous_file}:{previous_line}; "
                f"next={current_key}@{current_file}:{current_line}; missing={missing}"
            )
            finding = _finding(
                "scoped_numbering_gap", current_file, current_line, evidence,
                detail=(
                    f"Possible note-number gap {missing} between definitions "
                    f"{previous_key} and {current_key}; numbering resets and OCR omissions "
                    "are uncertain, so inspect only adjacent note evidence."
                ),
                key=",".join(map(str, missing)),
            )
            finding["neighbor_keys"] = [previous_key, current_key]
            finding["neighbor_files"] = [previous_file, current_file]
            # Page mapping is resolved later through the exact sidecar
            # candidates for these neighboring keys. `start_page` is only a
            # unit bound and must never be invented as evidence for a gap.
            if (finding["task_id"], finding["evidence_sha256"]) not in exceptions:
                findings.append(finding)
            else:
                suppressed.append({"task_id": finding["task_id"], "evidence_sha256": finding["evidence_sha256"]})

    # Validate materialized marker/definition counts and continuation seams.
    if target_texts is not None:
        for file, source in sorted(source_texts.items()):
            target = target_texts.get(file)
            if target is None:
                findings.append(_finding("normalized_unit_missing", file, 1, source[:280], detail="Normalized unit is missing."))
                continue
            # Note definitions should not be followed by unindented body text;
            # continuation paragraphs have explicit Markdown indentation.
            visible_target_lines = _visible(target).splitlines()
            target_lines = visible_target_lines
            target_defs, target_consumed = collect_footnote_definition_blocks(target_lines)
            definition_lines = {line + 1 for line in target_defs}
            definition_lines.update(line + 1 for line in target_consumed)
            target_record = record_by_file.get(file, {})
            target_scope = str(target_record.get("chapter_id") or file)
            target_marker_scan = (
                mode != "endnotes_only"
                and str(target_record.get("type") or "") not in FOOTNOTE_STRUCTURE_EXEMPT_TYPES
            )
            for line_number, line in enumerate(visible_target_lines, 1):
                if line_number in definition_lines:
                    continue
                equation_text = re.sub(r"<[^>]+>", " ", line)
                equation_like = (
                    len(list(_RAW_SUP_RE.finditer(line))) + len(list(_UNICODE_SUP_RE.finditer(line))) >= 2
                    and bool(re.search(r"(?:=|[+*/])", equation_text))
                )
                for occurrence, match in enumerate(_RAW_SUP_RE.finditer(line) if target_marker_scan else ()):
                    if equation_like:
                        continue
                    if _is_in_inline_code(line, match.start()):
                        continue
                    key = match.group("key")
                    if key in reviewed_body_keys.get(target_scope, set()):
                        continue
                    finding = _finding(
                        "unexplained_numeric_superscript", file, line_number, line.strip(),
                        detail=f"Raw superscript {key} remains in normalized body text.",
                        key=key, origin="normalized_target", identity=f"key={key};occurrence={occurrence}",
                    )
                    finding["evidence_target_sha256"] = _sha(target)
                    record_finding(finding)
                for occurrence, match in enumerate(_UNICODE_SUP_RE.finditer(line) if target_marker_scan else ()):
                    if equation_like:
                        continue
                    if _is_in_inline_code(line, match.start()):
                        continue
                    key = match.group(0).translate(_DIGIT_MAP)
                    finding = _finding(
                        "unexplained_unicode_superscript", file, line_number, line.strip(),
                        detail=f"Unicode superscript {key} remains in normalized body text.",
                        key=key, origin="normalized_target", identity=f"key={key};occurrence={occurrence}",
                    )
                    finding["evidence_target_sha256"] = _sha(target)
                    record_finding(finding)
                raw_def = _RAW_DEF_RE.match(line) if target_marker_scan else None
                if raw_def and re.search(r"\b(?:note|notes|see note|cf\.|ibid\.|trans\.|ed\.)\b", raw_def.group("body"), re.I):
                    key = raw_def.group("bracket") or raw_def.group("num")
                    finding = _finding(
                        "suspected_numbered_note_definition", file, line_number, line.strip(),
                        detail=f"Numbered block {key} remains a possible unnormalized note definition.",
                        key=key, origin="normalized_target",
                    )
                    finding["evidence_target_sha256"] = _sha(target)
                    record_finding(finding)
            for index, (key, body) in target_defs.items():
                value = body.strip()
                if (
                    value.endswith(("-", "‐", "‑", "—", "…"))
                    or value.count("(") > value.count(")")
                    or value.count("[") > value.count("]")
                ):
                    record_finding(_finding(
                        "normalized_note_truncated", file, 0,
                        f"[^{key}]: {body}",
                        detail=f"Normalized note definition {key} remains truncated or unclosed.",
                        key=key,
                        origin="normalized_target",
                    ))
            covered = set(target_consumed)
            covered.update(target_defs)
            for index in target_defs:
                cursor = index + 1
                while cursor < len(target_lines) and not target_lines[cursor].strip():
                    cursor += 1
                if cursor < len(target_lines) and cursor not in covered and not target_lines[cursor].lstrip().startswith("#"):
                    record_finding(_finding("possible_body_note_boundary_contamination", file, 0, target_lines[cursor].strip(), detail="Unindented body text follows a note definition; verify the page/body boundary and paragraph ownership."))

        # Verify the complete expected note parts against the actual
        # materialized definitions, including ordering within the chapter-end
        # file. A resolved sidecar address alone does not prove its text made
        # it into the EPUB source.
        expected_by_file: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
        for expected in expected_notes:
            if isinstance(expected, Mapping) and expected.get("target_file") and expected.get("output_key"):
                expected_by_file[str(expected["target_file"])].append(expected)
        for target_file, expected_rows in expected_by_file.items():
            actual_rows = target_definition_rows.get(target_file, [])
            actual_keys = [key for key, _ in actual_rows]
            key_positions = {key: index for index, key in enumerate(actual_keys)}
            expected_keys = [str(row["output_key"]) for row in expected_rows]
            for expected in expected_rows:
                key = str(expected["output_key"])
                bodies = [body for actual_key, body in actual_rows if actual_key == key]
                parts = expected.get("parts") if isinstance(expected.get("parts"), list) else []
                signature = _alnum(" ".join(str(part) for part in parts))
                body_signature = _alnum(bodies[0]) if len(bodies) == 1 else ""
                if len(bodies) != 1 or not signature or signature not in body_signature:
                    evidence = f"key={key}; expected={signature}; actual_count={len(bodies)}"
                    finding = _finding(
                        "applied_note_content_integrity", target_file, 0, evidence,
                        detail=f"Expected complete note {key} is missing, duplicated, truncated, or has its parts out of order in the materialized chapter-end definition.",
                        key=key, origin="normalized_target",
                    )
                    finding["evidence_target_sha256"] = _sha(target_texts.get(target_file, ""))
                    refs = expected.get("candidate_refs") if isinstance(expected.get("candidate_refs"), list) else []
                    finding["candidate_refs"] = refs
                    if refs:
                        finding["page"] = refs[0].get("page")
                    record_finding(finding)
            if all(key in key_positions for key in expected_keys):
                positions = [key_positions[key] for key in expected_keys]
                if positions != sorted(positions) or len(set(positions)) != len(positions):
                    evidence = f"expected_order={expected_keys}; actual_order={actual_keys}"
                    finding = _finding(
                        "applied_note_order_integrity", target_file, 0, evidence,
                        detail="Materialized chapter-end note definitions do not preserve accepted source order.",
                        origin="normalized_target",
                    )
                    finding["evidence_target_sha256"] = _sha(target_texts.get(target_file, ""))
                    refs = [
                        ref for expected in expected_rows
                        for ref in (expected.get("candidate_refs") if isinstance(expected.get("candidate_refs"), list) else [])
                    ]
                    finding["candidate_refs"] = refs
                    if refs:
                        finding["page"] = refs[0].get("page")
                    record_finding(finding)

    dedup: dict[str, dict[str, Any]] = {}
    for item in findings:
        dedup[item["task_id"]] = item
    ordered = [dedup[key] for key in sorted(dedup)]
    return {
        "schema_version": AUDIT_VERSION,
        "status": "review_required" if ordered else "validated",
        "valid": not ordered,
        "mode": mode,
        "source_sha256": {k: _sha(v) for k, v in sorted(source_texts.items())},
        "target_sha256": {k: _sha(v) for k, v in sorted((target_texts or {}).items())} if target_texts is not None else None,
        "candidate_report_sha256": None,
        "decision_validation_sha256": None,
        "findings": ordered,
        "resolved": sorted(resolved, key=lambda row: row["task_id"]),
        "suppressed": sorted(suppressed, key=lambda row: row["task_id"]),
    }



__all__ = ["AUDIT_FILENAME", "AUDIT_VERSION", "audit_footnote_result"]
