"""CLI wrapper for the read-only footnote review cost summary."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import yaml

from pdf2epub.refine.footnote_review_diagnostics import ArtifactError, build_footnote_review_summary
from pdf2epub.utils.paths import book_output_dir


def footnote_review_summary_command(args: Any) -> int:
    config_path = Path(getattr(args, "config", "config.yaml"))
    try:
        config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
        if not isinstance(config, dict):
            raise ValueError("configuration root must be a YAML mapping")
        title = str(config.get("title") or "").strip()
        if not title:
            raise ValueError("configuration title is missing")
        summary = build_footnote_review_summary(book_output_dir(title))
    except (OSError, UnicodeError, yaml.YAMLError, ValueError, ArtifactError) as exc:
        print(json.dumps({"schema_version": 1, "diagnostic_only": True, "available": False, "error": str(exc)}, ensure_ascii=False, indent=2))
        return 1
    if getattr(args, "json", False):
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    else:
        counts = summary["counts"]
        print(f"Footnote review diagnostic (not production-validated): {summary['output_dir']}")
        for key, value in counts.items():
            print(f"{key}: {'unknown' if value is None else value}")
        print(f"Missing artifacts: {', '.join(summary['missing_artifacts']) or 'none'}")
        print(f"Validation freshness: {summary['validation_freshness']}")
    return 0
