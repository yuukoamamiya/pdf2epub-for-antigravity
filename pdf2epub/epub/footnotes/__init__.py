"""
Footnote management system for EPUB generation.

This module handles both local (per-chapter) and global (cross-chapter) footnote styles.
It automatically detects which style is appropriate based on the book's structure.
"""

from .keys import display_footnote_key
from .models import FootnoteStyle, FootnoteDefinition, FootnoteReference
from .manager import FootnoteManager
from .validator import FootnoteGraphError, inspect_footnote_graph, validate_footnote_graph

__all__ = [
    'FootnoteStyle',
    'FootnoteDefinition',
    'FootnoteReference',
    'FootnoteManager',
    'FootnoteGraphError',
    'display_footnote_key',
    'inspect_footnote_graph',
    'validate_footnote_graph',
]
