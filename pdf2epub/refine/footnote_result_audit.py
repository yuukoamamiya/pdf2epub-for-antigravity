"""Pure result audit for footnote preparation and normalization.

The audit reports suspicious residual markers and note-boundary anomalies without
choosing their semantics. Every review exception is bound to an exact file block
hash so a later source edit cannot silently reuse it.
"""
from __future__ import annotations

import hashlib
import json
import re
from collections import defaultdict
from typing import Any, Mapping, MutableMapping, Sequence

from ..footnote_markdown import collect_footnote_definition_blocks
from .endnote_review import _inside_inline_code_or_math
from .endnote_scopes import markdown_fence_mask
from .footnote_structure import FOOTNOTE_STRUCTURE_EXEMPT_TYPES

AUDIT_VERSION = 4
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


def _marker_occurrence_identity(
    lines: Sequence[str], line_index: int, match: re.Match[str], key: str, ordinal: int,
    occurrence_counts: MutableMapping[str, int],
) -> str:
    """Identify one visible marker independently of its chapter/key peers.

    The line number is intentionally absent so a finding follows an unchanged
    occurrence when unrelated lines are inserted above it. The exact marker,
    its line-local offset/ordinal, and local marker context identify its
    fingerprint; a file-local rank distinguishes identical repeated lines.
    """
    line = lines[line_index]
    before = line[max(0, match.start() - 48):match.start()]
    after = line[match.end():match.end() + 48]
    payload = {
        "key": key,
        "marker": match.group(0),
        "offset": match.start(),
        "ordinal": ordinal,
        "before": before,
        "after": after,
    }
    fingerprint = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    occurrence_rank = occurrence_counts.get(fingerprint, 0) + 1
    occurrence_counts[fingerprint] = occurrence_rank
    identity_payload = json.dumps(
        {
            **payload,
            "occurrence_rank": occurrence_rank,
        },
        ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    )
    return hashlib.sha256(identity_payload.encode("utf-8")).hexdigest()


def _toc_heading_nodes(toc_tree: Mapping[str, Any] | None) -> list[dict[str, Any]]:
    """Project heading identity plus reviewed source-boundary metadata."""
    if not isinstance(toc_tree, Mapping):
        return []
    result: list[dict[str, Any]] = []

    def visit(nodes: Any, path: tuple[int, ...] = ()) -> None:
        if not isinstance(nodes, list):
            return
        for index, node in enumerate(nodes):
            if not isinstance(node, Mapping):
                continue
            node_path = (*path, index)
            title = str(node.get("title") or "").strip()
            if title:
                boundary = node.get("boundary_info")
                result.append({
                    "title": title,
                    "node_id": str(node.get("node_id") or ""),
                    "unit_id": str(node.get("unit_id") or ""),
                    "index_path": list(node.get("index_path") or node_path),
                    "start_page": int(node.get("start_page") or 0),
                    "boundary_info": dict(boundary) if isinstance(boundary, Mapping) else {},
                })
            visit(node.get("children"), node_path)

    visit(toc_tree.get("chapters"))
    return result


def _canonical_heading_line(value: str) -> str:
    value = re.sub(r"^\s{0,3}#{1,6}\s+", "", value.strip())
    value = re.sub(r"\s+", " ", value)
    return value.strip()


def _heading_note_findings(
    *, file: str, target: str, target_defs: Mapping[int, tuple[str, str]],
    expected_by_target_key: Mapping[tuple[str, str], Mapping[str, Any]],
    source_texts: Mapping[str, str], resolved_blocks: Sequence[Mapping[str, Any]],
    toc_tree: Mapping[str, Any] | None, page_texts: Mapping[int | str, str] | None,
) -> list[dict[str, Any]]:
    """Find exact TOC title lines inside source spans materialized as notes.

    A title is called confirmed only if the reviewed TOC boundary resolves to
    the same physical page and the page Markdown confirms the bound heading
    range. Without that positional confirmation, the exact title is only a
    visual-review suspicion.
    """
    nodes = _toc_heading_nodes(toc_tree)
    if not nodes:
        return []
    by_output: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for row in resolved_blocks:
        if isinstance(row, Mapping) and row.get("output_key"):
            by_output[str(row["output_key"])].append(row)
    findings: list[dict[str, Any]] = []
    for target_index, (target_key, body) in target_defs.items():
        expected = expected_by_target_key.get((file, str(target_key)))
        if not isinstance(expected, Mapping):
            continue
        output_key = str(expected.get("output_key") or target_key)
        chapter_id = str(expected.get("chapter_id") or "")
        refs = {
            (int(ref.get("page", -1)), str(ref.get("source") or "primary"), int(ref.get("block", -1)))
            for ref in expected.get("candidate_refs", []) if isinstance(ref, Mapping)
            and str(ref.get("page", "")).lstrip("-").isdigit()
            and str(ref.get("block", "")).lstrip("-").isdigit()
        }
        target_lines = {
            _canonical_heading_line(line) for line in _visible(body).splitlines()
            if _canonical_heading_line(line)
        }
        for block in by_output.get(output_key, []):
            if str(block.get("chapter_id") or "") != chapter_id:
                continue
            try:
                address = (int(block["page"]), str(block.get("source") or "primary"), int(block["block"]))
                span_value = block["span"]
                start, end = int(span_value[0]), int(span_value[1])
            except (KeyError, TypeError, ValueError, IndexError):
                continue
            if address not in refs:
                continue
            source_file = str(block.get("file") or "")
            source = source_texts.get(source_file, "")
            if not source_file or start < 0 or end < start or end > len(source):
                continue
            source_slice = source[start:end]
            source_lines = {
                _canonical_heading_line(line) for line in _visible(source_slice).splitlines()
                if _canonical_heading_line(line)
            }
            for node in nodes:
                title = _canonical_heading_line(str(node.get("title") or ""))
                if not title or title not in target_lines or title not in source_lines:
                    continue
                if address[0] != int(node.get("start_page") or 0):
                    continue
                boundary = node.get("boundary_info") if isinstance(node.get("boundary_info"), Mapping) else {}
                start_line, end_line = boundary.get("start_line"), boundary.get("heading_end_line")
                page_text = None
                if isinstance(page_texts, Mapping):
                    page_text = page_texts.get(address[0], page_texts.get(str(address[0])))
                exact_page_boundary = False
                if (
                    boundary.get("heading_action") == "bind"
                    and type(start_line) is int and type(end_line) is int
                    and isinstance(page_text, str)
                ):
                    from .heading_ranges import source_range_matches_title
                    from .footnote_matching import find_exact_signature_spans

                    bound_heading = source_range_matches_title(
                        page_text.splitlines(), start_line, end_line, str(node.get("title") or ""),
                        anchor_line=start_line,
                    )
                    page_lines = page_text.splitlines(keepends=True)
                    heading_start = sum(len(line) for line in page_lines[:start_line - 1])
                    heading_end = sum(len(line) for line in page_lines[:end_line - 1])
                    source_matches = find_exact_signature_spans(page_text, source_slice)
                    exact_page_boundary = (
                        bound_heading
                        and len(source_matches) == 1
                        and max(source_matches[0][0], heading_start)
                        < min(source_matches[0][1], heading_end)
                    )
                code = "confirmed_toc_heading_in_note" if exact_page_boundary else "suspected_heading_in_note"
                finding = _finding(
                    code, file, target_index + 1, f"[^{target_key}]: {body}",
                    detail=(
                        "The edit plan moved a source span containing a reviewed TOC heading boundary into this note. Inspect both page image and note ownership."
                        if exact_page_boundary else
                        "A source span materialized as a note contains an exact TOC title on the same page, but its reviewed source boundary does not intersect this span. Inspect the page image."
                    ),
                    key=str(target_key), origin="normalized_target",
                    identity=(
                        f"node={node.get('node_id') or node.get('unit_id') or node.get('index_path')};"
                        f"page={address[0]};source={address[1]};block={address[2]};span={start}:{end}"
                    ), page=address[0],
                )
                finding.update({
                    "evidence_target_sha256": _sha(target),
                    "source_file": source_file,
                    "source_span": [start, end],
                    "candidate_refs": [{"page": address[0], "source": address[1], "block": address[2]}],
                    "pages": [address[0]],
                    "toc_heading_identity": {
                        "node_id": node.get("node_id"),
                        "unit_id": node.get("unit_id"),
                        "index_path": node.get("index_path"),
                        "title": node.get("title"),
                        "boundary_info": dict(boundary),
                    },
                })
                findings.append(finding)
    return findings


def audit_footnote_result(
    source_texts: Mapping[str, str],
    *,
    records: Sequence[Mapping[str, Any]] = (),
    target_texts: Mapping[str, str] | None = None,
    mode: str = "footnotes",
    decisions: Sequence[Mapping[str, Any]] = (),
    expected_notes: Sequence[Mapping[str, Any]] = (),
    current_exceptions: Sequence[Mapping[str, Any]] = (),
    resolved_blocks: Sequence[Mapping[str, Any]] = (),
    toc_tree: Mapping[str, Any] | None = None,
    page_texts: Mapping[int | str, str] | None = None,
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
    next_part_by_file: dict[str, Mapping[str, Any]] = {}
    ordered_records = [item for item in records if item.get("name")]
    for current, following in zip(ordered_records, ordered_records[1:]):
        if (
            str(current.get("chapter_id") or "") == str(following.get("chapter_id") or "")
            and int(following.get("part_index") or 1) == int(current.get("part_index") or 1) + 1
            and int(current.get("end_page") or 0) > 0
            and int(following.get("start_page") or 0) == int(current.get("end_page") or 0) + 1
        ):
            next_part_by_file[str(current["name"])] = following
    expected_by_target_key: dict[tuple[str, str], Mapping[str, Any]] = {}
    physical_note_refs: list[tuple[int, int, str, str, str]] = []
    for expected in expected_notes:
        if not isinstance(expected, Mapping):
            continue
        target_file = str(expected.get("target_file") or "")
        output_key = str(expected.get("output_key") or "")
        if target_file and output_key:
            expected_by_target_key[(target_file, output_key)] = expected
        for ref in expected.get("candidate_refs", []) or []:
            if not isinstance(ref, Mapping):
                continue
            try:
                physical_note_refs.append((int(ref["page"]), int(ref["block"]), str(ref.get("source_file") or ""), target_file, output_key))
            except (KeyError, TypeError, ValueError):
                continue
    last_physical_note_ref_by_page: dict[int, tuple[int, int, str, str, str]] = {}
    for ref in physical_note_refs:
        current_last = last_physical_note_ref_by_page.get(ref[0])
        if current_last is None or ref > current_last:
            last_physical_note_ref_by_page[ref[0]] = ref
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
    body_numeric_refs: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for file, raw in sorted(source_texts.items()):
        text = _visible(raw)
        lines = text.splitlines()
        occurrence_counts: dict[str, int] = {}
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
                if re.match(r"\s*\d{4}", after) and re.search(r"\b\d{4},?\s*$", before):
                    continue
                occurrence_id = _marker_occurrence_identity(lines, number - 1, match, key, occurrence, occurrence_counts)
                body_numeric_refs[scope].append({
                    "file": file, "line": number, "key": key,
                    "occurrence_sha256": occurrence_id,
                })
                finding = _finding("unexplained_numeric_superscript", file, number, line.strip(), detail=f"Raw superscript {key} remains in body text.", key=key, identity=f"occurrence={occurrence_id}")
                finding["occurrence_sha256"] = occurrence_id
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
                occurrence_id = _marker_occurrence_identity(lines, number - 1, match, key, occurrence, occurrence_counts)
                finding = _finding("unexplained_unicode_superscript", file, number, line.strip(), detail=f"Unicode superscript {key} remains in body text.", key=key, identity=f"occurrence={occurrence_id}")
                finding["occurrence_sha256"] = occurrence_id
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
                occurrence_id = _marker_occurrence_identity(lines, number - 1, match, key, occurrence, occurrence_counts)
                finding = _finding("unexplained_bracketed_numeric_reference", file, number, line.strip(), detail=f"Bracketed numeric reference [{key}] remains unclassified.", key=key, identity=f"occurrence={occurrence_id}")
                finding["occurrence_sha256"] = occurrence_id
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
            missing_as_text = {str(value) for value in missing}
            gap_references = [
                dict(ref) for ref in body_numeric_refs.get(scope, [])
                if str(ref.get("key") or "") in missing_as_text
            ]
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
                identity="references=" + _sha(json.dumps(gap_references, sort_keys=True, separators=(",", ":"))),
            )
            finding["neighbor_keys"] = [previous_key, current_key]
            finding["neighbor_files"] = [previous_file, current_file]
            finding["missing_keys"] = missing
            finding["body_reference_occurrences"] = gap_references
            finding["physical_definition_boundary"] = {
                "previous_page_range": [previous_start, previous_end],
                "current_page_range": [current_start, current_end],
                "previous_section_id": previous_section,
                "current_section_id": current_section,
            }
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
            target_occurrence_counts: dict[str, int] = {}
            target_lines = visible_target_lines
            target_defs, target_consumed = collect_footnote_definition_blocks(target_lines)
            resolved_by_output_key: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
            for resolved_item in resolved_blocks:
                if isinstance(resolved_item, Mapping) and resolved_item.get("output_key"):
                    resolved_by_output_key[str(resolved_item["output_key"])].append(resolved_item)
            definition_lines = {line + 1 for line in target_defs}
            definition_lines.update(line + 1 for line in target_consumed)
            target_record = record_by_file.get(file, {})
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
                    occurrence_id = _marker_occurrence_identity(visible_target_lines, line_number - 1, match, key, occurrence, target_occurrence_counts)
                    finding = _finding(
                        "unexplained_numeric_superscript", file, line_number, line.strip(),
                        detail=f"Raw superscript {key} remains in normalized body text.",
                        key=key, origin="normalized_target", identity=f"occurrence={occurrence_id}",
                    )
                    finding["occurrence_sha256"] = occurrence_id
                    finding["evidence_target_sha256"] = _sha(target)
                    record_finding(finding)
                for occurrence, match in enumerate(_UNICODE_SUP_RE.finditer(line) if target_marker_scan else ()):
                    if equation_like:
                        continue
                    if _is_in_inline_code(line, match.start()):
                        continue
                    key = match.group(0).translate(_DIGIT_MAP)
                    occurrence_id = _marker_occurrence_identity(visible_target_lines, line_number - 1, match, key, occurrence, target_occurrence_counts)
                    finding = _finding(
                        "unexplained_unicode_superscript", file, line_number, line.strip(),
                        detail=f"Unicode superscript {key} remains in normalized body text.",
                        key=key, origin="normalized_target", identity=f"occurrence={occurrence_id}",
                    )
                    finding["occurrence_sha256"] = occurrence_id
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
            for task in _heading_note_findings(
                file=file, target=target, target_defs=target_defs,
                expected_by_target_key=expected_by_target_key,
                source_texts=source_texts, resolved_blocks=resolved_blocks,
                toc_tree=toc_tree, page_texts=page_texts,
            ):
                record_finding(task)
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
                expected = expected_by_target_key.get((file, str(key)))
                candidate_refs = expected.get("candidate_refs", []) if isinstance(expected, Mapping) else []
                exact_ref = max(
                    (
                        (int(ref.get("page", 0)), int(ref.get("block", -1)), str(ref.get("source_file") or ""), str(expected.get("target_file") or ""), str(expected.get("output_key") or ""))
                        for ref in candidate_refs if isinstance(ref, Mapping)
                        and str(ref.get("page", "")).isdigit()
                        and str(ref.get("block", "")).isdigit()
                    ),
                    default=None,
                ) if isinstance(expected, Mapping) else None
                source_record = next_part_source = None
                if exact_ref is not None:
                    physical_page = exact_ref[0]
                    source_filename = exact_ref[2]
                    if source_filename:
                        source_record = record_by_file.get(source_filename)
                        if source_record is not None and not (
                            int(source_record.get("start_page") or 0)
                            <= physical_page
                            <= int(source_record.get("end_page") or 0)
                        ):
                            source_record = None
                    else:
                        source_record = next((
                            row for row in records
                            if int(row.get("start_page") or 0) <= physical_page <= int(row.get("end_page") or 0)
                            and str(row.get("chapter_id") or "") == str(expected.get("chapter_id") or "")
                        ), None)
                    if source_record is not None:
                        next_part_source = next_part_by_file.get(str(source_record.get("name") or ""))
                open_punctuation = value.endswith((",", ":", ";"))
                if (
                    mode != "endnotes_only"
                    and source_record is not None
                    and str(source_record.get("type") or "") not in FOOTNOTE_STRUCTURE_EXEMPT_TYPES
                    and open_punctuation
                    and next_part_source is not None
                    and exact_ref == last_physical_note_ref_by_page.get(exact_ref[0])
                    and int(source_record.get("end_page") or 0) == exact_ref[0]
                ):
                    finding = _finding(
                        "normalized_note_open_boundary_at_physical_page_break",
                        file, 0, f"[^{key}]: {body}",
                        detail=(
                            f"Normalized note {key} ends with open punctuation exactly at a mapped physical page/part boundary. "
                            "This is only a visual-review cue; confirm the neighboring page images before changing ownership."
                        ),
                        key=str(key), origin="normalized_target",
                    )
                    finding["pages"] = [exact_ref[0], int(next_part_source.get("start_page") or 0)]
                    finding["evidence_target_sha256"] = _sha(target)
                    record_finding(finding)
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
