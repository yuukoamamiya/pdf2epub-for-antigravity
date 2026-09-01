"""
Data models for the footnote management system.
"""

from dataclasses import dataclass
from enum import Enum


class FootnoteStyle(Enum):
    """Footnote organization style."""
    LOCAL = "local"    # Each chapter has its own footnotes (default)
    GLOBAL = "global"  # Footnotes are centralized in specific chapters


@dataclass
class FootnoteDefinition:
    """Represents a footnote definition."""
    key: str           # The footnote key (e.g., "1", "note")
    content: str       # The footnote text
    chapter: str       # The chapter file where defined
    line_num: int      # Line number in the file


@dataclass
class FootnoteReference:
    """Represents a footnote reference."""
    key: str           # The footnote key
    chapter: str       # The chapter file where referenced
    line_num: int      # Line number in the file
