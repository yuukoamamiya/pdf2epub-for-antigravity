"""Conservative source Markdown references for rendered graph expectations."""

from __future__ import annotations

import re
from collections import defaultdict
from html.parser import HTMLParser
from pathlib import Path

from ...footnote_markdown import collect_footnote_definition_blocks


def scan_source_reference_occurrences(
    files: list[Path], no_colon_definition_chapters: set[str] | None = None
) -> list[dict[str, str | int]]:
    """Return actual footnote syntax outside code, comments, and definitions."""
    expected: list[dict[str, str | int]] = []
    counts: dict[tuple[str, str], int] = defaultdict(int)
    for path in files:
        lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
        visible_lines = _mask_code_and_comments(lines)
        definitions, consumed = collect_footnote_definition_blocks(
            visible_lines,
            allow_no_colon=path.stem in (no_colon_definition_chapters or set()),
        )
        definition_lines = set(definitions) | consumed
        for line_index, line in enumerate(visible_lines):
            if line_index in definition_lines:
                continue
            for match in re.finditer(r"\[\^([\w-]+)\](?!:)", line):
                source_key = match.group(1)
                page_note = re.match(r"^\d+n(\d+)$", source_key)
                key = page_note.group(1) if page_note else source_key
                identity = (path.stem, key)
                counts[identity] += 1
                expected.append(
                    {
                        "source_chapter": path.stem,
                        "key": key,
                        "occurrence": counts[identity],
                    }
                )
    return expected


def scan_legacy_hr_note_candidates(files: list[Path]) -> list[dict[str, str | int]]:
    """Find raw superscripts paired with numbered notes across an HR divider.

    This is a review signal only. It deliberately does not convert or link the
    source marker; a divider plus exact matching note number is the minimum
    legacy evidence used by the conservative fallback.
    """
    candidates: list[dict[str, str | int]] = []
    sup_re = re.compile(r"<sup\b[^>]*>(.*?)</sup\s*>", re.IGNORECASE)
    note_re = re.compile(r"^[ \t]{0,3}(\d+)[.)][ \t]+\S")
    hr_re = re.compile(r"^[ \t]{0,3}(?:-{3,}|\*{3,}|_{3,})[ \t]*$")
    for path in files:
        lines = _mask_code_and_comments(path.read_text(encoding="utf-8").splitlines(keepends=True))
        linked_sup_offsets = _linked_superscript_offsets("".join(lines))
        divider_indices = [i for i, line in enumerate(lines) if hr_re.match(line.rstrip("\r\n"))]
        for divider_index in divider_indices:
            references: list[tuple[int, re.Match[str]]] = []
            definitions: dict[str, int] = {}
            offset = 0
            for line_index, line in enumerate(lines):
                for match in sup_re.finditer(line):
                    key_match = re.fullmatch(r"\s*(\d+)\s*", match.group(1))
                    if (
                        not key_match
                        or offset + match.start() in linked_sup_offsets
                    ):
                        continue
                    if _looks_like_variable_superscript(line, match.start()):
                        continue
                    references.append((line_index, key_match))
                note = note_re.match(line)
                if note and line_index > divider_index:
                    definitions.setdefault(note.group(1), line_index)
                offset += len(line)
            for line_index, match in references:
                key = match.group(1)
                if line_index < divider_index < definitions.get(key, -1):
                    candidates.append(
                        {
                            "source_file": path.name,
                            "key": key,
                            "reference_line": line_index + 1,
                            "definition_line": definitions[key] + 1,
                            "status": "review_required",
                        }
                    )
    return candidates


def _looks_like_variable_superscript(line: str, start: int) -> bool:
    before = line[:start]
    after = line[start:]
    return bool(
        re.search(r"(?:^|[\s=+*/(])([A-Za-z])$", before)
        or re.search(r"</sup>\s*\d{4}\b", after, re.IGNORECASE)
    )


def _linked_superscript_offsets(text: str) -> set[int]:
    """Find superscripts nested in HTML anchors or Markdown links."""
    class AnchorSupParser(HTMLParser):
        def __init__(self, source: str) -> None:
            super().__init__(convert_charrefs=False)
            self.source = source
            self.line_offsets = [0]
            for line in source.splitlines(keepends=True):
                self.line_offsets.append(self.line_offsets[-1] + len(line))
            self.stack: list[str] = []
            self.offsets: set[int] = set()

        def handle_starttag(self, tag: str, attrs) -> None:
            if tag.lower() == "sup" and "a" in self.stack:
                line, column = self.getpos()
                self.offsets.add(self.line_offsets[line - 1] + column)
            if tag.lower() not in {
                "area", "base", "br", "col", "embed", "hr", "img", "input",
                "link", "meta", "param", "source", "track", "wbr",
            }:
                self.stack.append(tag.lower())

        def handle_startendtag(self, tag: str, attrs) -> None:
            if tag.lower() == "sup" and "a" in self.stack:
                line, column = self.getpos()
                self.offsets.add(self.line_offsets[line - 1] + column)

        def handle_endtag(self, tag: str) -> None:
            tag = tag.lower()
            for index in range(len(self.stack) - 1, -1, -1):
                if self.stack[index] == tag:
                    del self.stack[index:]
                    break

    parser = AnchorSupParser(text)
    parser.feed(text)
    offsets = parser.offsets
    # Markdown links are not HTML tokens, so also recognize a linked raw sup.
    linked_markdown = re.compile(
        r"\[[^\]\n]*?<sup\b[^>]*>.*?</sup\s*>[^\]\n]*\]\([^\n)]*\)",
        re.IGNORECASE,
    )
    for link in linked_markdown.finditer(text):
        sup = re.search(r"<sup\b[^>]*>.*?</sup\s*>", link.group(0), re.IGNORECASE)
        if sup:
            offsets.add(link.start() + sup.start())
    return offsets


def mask_markdown_code_and_comments(lines: list[str]) -> list[str]:
    text = "".join(lines)
    chars = list(text)
    offsets = []
    offset = 0
    fence_char = None
    fence_size = 0
    for line in lines:
        offsets.append(offset)
        match = re.match(r"^\s{0,3}(`{3,}|~{3,})", line)
        if fence_char is not None:
            _blank(chars, offset, offset + len(line))
            if match and match.group(1)[0] == fence_char and len(match.group(1)) >= fence_size:
                fence_char, fence_size = None, 0
        elif match:
            fence_char, fence_size = match.group(1)[0], len(match.group(1))
            _blank(chars, offset, offset + len(line))
        else:
            for code in re.finditer(r"(`+)(?!`)(.+?)\1(?!`)", line):
                _blank(chars, offset + code.start(), offset + code.end())
        offset += len(line)
    masked = "".join(chars)
    chars = list(masked)
    for pattern in (r"<!--.*?-->", r"<(?:pre|code)\b[^>]*>.*?</(?:pre|code)\s*>"):
        for match in re.finditer(pattern, masked, re.IGNORECASE | re.DOTALL):
            _blank(chars, match.start(), match.end())
        masked = "".join(chars)
        chars = list(masked)
    return [masked[start : start + len(line)] for start, line in zip(offsets, lines)]


_mask_code_and_comments = mask_markdown_code_and_comments


def _blank(chars: list[str], start: int, end: int) -> None:
    for index in range(start, min(end, len(chars))):
        if chars[index] not in "\r\n":
            chars[index] = " "
