"""Shared parser for Markdown footnote definition blocks."""

from __future__ import annotations

import re

FOOTNOTE_KEY_PATTERN = r"[\w-]+"


def collect_footnote_definition_blocks(lines, *, allow_no_colon=False):
    """Return definitions keyed by zero-based line and lines consumed by them.

    Continuations require four spaces or a tab. Soft wraps are joined with a
    space; only an intervening blank line creates a new note paragraph. A
    blank line followed by ordinary unindented text is left untouched.
    """
    definitions = {}
    consumed = set()
    index = 0
    while index < len(lines):
        line = lines[index]
        match = re.match(rf'^\[\^({FOOTNOTE_KEY_PATTERN})\]:\s*(.*)', line)
        if not match and allow_no_colon:
            match = re.match(rf'^\[\^({FOOTNOTE_KEY_PATTERN})\]\s+(.+)', line)
        if not match:
            index += 1
            continue

        key, first = match.groups()
        start = index
        paragraphs = [[first]]
        index += 1
        while index < len(lines):
            if not lines[index].strip():
                blank_start = index
                while index < len(lines) and not lines[index].strip():
                    index += 1
                if index < len(lines) and re.match(r'^(?: {4}|\t)', lines[index]):
                    paragraphs.append([])
                    consumed.update(range(blank_start, index))
                    continue
                index = blank_start
                break
            continuation = re.match(r'^(?: {4}|\t)(.*)$', lines[index])
            if not continuation:
                break
            # Remove exactly the structural continuation indent. Remaining
            # indentation and line boundaries carry Markdown block meaning.
            paragraphs[-1].append(continuation.group(1))
            consumed.add(index)
            index += 1
        definitions[start] = (key, "\n\n".join("\n".join(p) for p in paragraphs))
    return definitions, consumed
