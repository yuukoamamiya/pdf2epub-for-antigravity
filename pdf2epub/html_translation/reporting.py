"""HTML translation report aggregation over validation and unit workspace data."""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
from loguru import logger

from pdf2epub.workflow_contracts import atomic_write_json
from .unit_files import HTMLUnitStore, HTMLUnitWorkspace
from .validation import nonempty_lines, tag_mismatch_count


@dataclass(frozen=True)
class HTMLReportContext:
    output_dir: Path
    source_epub: Path
    book_title: str
    navigation_report: Dict[str, Any]
    units: HTMLUnitWorkspace
    metadata_validation: Dict[str, Any]


class HTMLTranslationReportAggregator:
    """Aggregate existing receipts and per-unit facts into the stable report."""
    def __init__(self, context: HTMLReportContext):
        self.context = context
        self.output_dir = Path(context.output_dir)
        self.epub_path = Path(context.source_epub)
        self.book_title = context.book_title
        self.navigation_report = context.navigation_report
        self.units = HTMLUnitStore(context.units)

    @staticmethod
    def _read_jsonl(path: Path) -> List[Dict[str, Any]]:
        """Read a JSONL file, skipping malformed lines."""
        if not path.exists():
            return []
        rows = []
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return rows
    def _trace_stats_for_file(self, trace_rows: List[Dict[str, Any]], file_stem: str) -> Dict[str, Any]:
        """Summarize LLM trace entries for one compressed HTML unit."""
        operation = f"Translate {file_stem}"
        rows = [row for row in trace_rows if row.get("operation") == operation]
        if not rows:
            return {
                "requests": 0,
                "errors": 0,
                "input_tokens": 0,
                "output_tokens": 0,
                "cache_read_tokens": 0,
                "cache_write_tokens": 0,
                "usage_statuses": {},
            }

        usage_statuses: Dict[str, int] = {}
        for row in rows:
            status = row.get("usage_status", "legacy")
            usage_statuses[status] = usage_statuses.get(status, 0) + 1

        return {
            "requests": len(rows),
            "errors": sum(1 for row in rows if row.get("error")),
            "input_tokens": sum(int(row.get("input_tokens") or 0) for row in rows),
            "output_tokens": sum(int(row.get("output_tokens") or 0) for row in rows),
            "cache_read_tokens": sum(int(row.get("cache_read_tokens") or 0) for row in rows),
            "cache_write_tokens": sum(int(row.get("cache_write_tokens") or 0) for row in rows),
            "usage_statuses": usage_statuses,
            "last_duration_ms": rows[-1].get("duration_ms"),
            "last_error": rows[-1].get("error"),
        }
    def _agent_stats_for_file(self, file_stem: str) -> Dict[str, Any]:
        """Summarize agent-loop artifacts for one translated unit."""
        artifact_dir = self.output_dir / "logs" / "agent_artifacts" / file_stem
        originals_dir = artifact_dir / "originals"
        workspace_dir = artifact_dir / "workspace"
        metrics_path = workspace_dir / "_agent_round_metrics.jsonl"
        metrics = self._read_jsonl(metrics_path)

        return {
            "artifact_dir": str(artifact_dir) if artifact_dir.exists() else None,
            "raw_output_exists": (originals_dir / "raw_output.txt").exists(),
            "continuation_count": len(list(originals_dir.glob("continuation_*.txt"))),
            "rounds": len(metrics),
            "actions": [row.get("decision_action") for row in metrics if row.get("decision_action")],
            "final_status": metrics[-1].get("status") if metrics else None,
            "final_action": metrics[-1].get("decision_action") if metrics else None,
        }
    def write(
        self,
        output_epub: Optional[Path] = None,
        phase: str = "translation",
    ) -> Path:
        """Write a machine-readable report for compressed HTML translation state."""
        metadata_path = self.output_dir / "translated_metadata.json"
        translated_metadata = {}
        if metadata_path.exists():
            try:
                translated_metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                translated_metadata = {}

        trace_path = self.output_dir / "logs" / "llm_trace.jsonl"
        trace_rows = self._read_jsonl(trace_path)

        files = []
        summary = {
            "total_files": 0,
            "translated_files": 0,
            "missing_translations": 0,
            "line_mismatch_files": 0,
            "tag_mismatch_files": 0,
            "total_continuations": 0,
        }

        merged_parts = self.units.merge_part_files_in_dir(self.units.workspace.translated_dir)
        for record in self.units.logical_unit_records(merged_parts):
            file_stem = record["base_stem"]
            source_text = record["source_content"]
            translated_text = record["translated_content"]
            source_lines = nonempty_lines(source_text)
            translated_lines = nonempty_lines(translated_text)

            compared = min(len(source_lines), len(translated_lines))
            tag_mismatches = tag_mismatch_count(
                source_lines[:compared],
                translated_lines[:compared],
            )

            agent_stats = self._agent_stats_for_file(file_stem)
            trace_stats = self._trace_stats_for_file(trace_rows, file_stem)
            continuation_count = agent_stats.get("continuation_count", 0)

            file_report = {
                "file": file_stem,
                "source_lines": len(source_lines),
                "translated_lines": len(translated_lines),
                "line_count_match": len(source_lines) == len(translated_lines),
                "tag_mismatches": tag_mismatches,
                "translated_exists": record["translated_exists"],
                "final_xhtml_exists": record["final_path"].exists(),
                "agent": agent_stats,
                "llm_trace": trace_stats,
            }
            files.append(file_report)

            summary["total_files"] += 1
            if record["translated_exists"]:
                summary["translated_files"] += 1
            else:
                summary["missing_translations"] += 1
            if record["translated_exists"] and len(source_lines) != len(translated_lines):
                summary["line_mismatch_files"] += 1
            if tag_mismatches:
                summary["tag_mismatch_files"] += 1
            summary["total_continuations"] += continuation_count

        report = {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "phase": phase,
            "book_title": self.book_title,
            "source_epub": str(self.epub_path),
            "output_epub": str(output_epub) if output_epub else None,
            "translated_title": translated_metadata.get("translated_title"),
            "target_language": translated_metadata.get("target_language"),
            "target_language_code": translated_metadata.get("target_language_code"),
            "metadata_validation": self.context.metadata_validation,
            "navigation": self.navigation_report,
            "navigation_warnings": [
                kind
                for kind, result in self.navigation_report.items()
                if isinstance(result, dict) and result.get("status") == "warning"
            ],
            "summary": summary,
            "files": files,
            "logs": {
                "llm_trace": str(trace_path),
                "prepare_log": str(self.output_dir / "logs" / "html-prepare.log"),
                "build_log": str(self.output_dir / "logs" / "build-html-epub.log"),
            },
        }

        report_path = self.output_dir / "translation_report.json"
        atomic_write_json(report_path, report)
        logger.info(f"Wrote translation report: {report_path}")
        return report_path
