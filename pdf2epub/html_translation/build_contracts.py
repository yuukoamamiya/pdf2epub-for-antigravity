"""Public HTML EPUB build data contracts and failures."""
from __future__ import annotations
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional

class NavigationUpdateError(RuntimeError):
    """Raised when translated EPUB navigation could not be updated safely."""

@dataclass
class BuildConfig:
    """Configuration for EPUB building."""
    original_epub: Path
    translated_dir: Path
    output_path: Path
    book_title: str
    translated_metadata: Optional[Dict] = None
    epubcheck_mode: str = "warn"
    epubcheck_path: Optional[str] = None
    navigation_report: Optional[Dict[str, Any]] = None
    allow_navigation_warnings: bool = False
