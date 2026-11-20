"""Patterns shared by endnote validation and endnote normalization.

Only the definitions that are *identical* on both sides live here.  The
definition regexes stay in their modules on purpose: validation matches a
prefix while apply requires content to the end of the line, so merging them
would change behavior.
"""
from __future__ import annotations

import re


UNICODE_SUP_RE = re.compile(r"(?P<value>[⁰¹²³⁴⁵⁶⁷⁸⁹]+)")
UNICODE_DIGITS = str.maketrans("⁰¹²³⁴⁵⁶⁷⁸⁹", "0123456789")
MARKDOWN_REF_RE = re.compile(r"\[\^(?P<key>[A-Za-z0-9_-]+)\](?!:)")


__all__ = ["MARKDOWN_REF_RE", "UNICODE_DIGITS", "UNICODE_SUP_RE"]
