"""External glossary candidate command."""

import json
from pathlib import Path

from loguru import logger

from pdf2epub.commands.runtime import load_book_context


def glossary_candidates_command(args):
    """Create a deterministic report of local external glossary candidates."""
    from pdf2epub.glossary import discover_glossary_candidates

    context = load_book_context(args, "glossary-candidates")
    if context is None:
        return 1
    config = context.config
    translation = config.get("translation", {}) or {}
    source_language = args.source_language or translation.get(
        "source_language", "English"
    )
    target_language = args.target_language or translation.get(
        "target_language", "Chinese"
    )
    config_root = context.config_path.resolve().parent
    glossary_dir = (
        Path(args.glossary_dir)
        if args.glossary_dir
        else config_root / "glossaries"
    )
    if not glossary_dir.is_absolute():
        glossary_dir = (config_root / glossary_dir).resolve()
    candidates = discover_glossary_candidates(
        glossary_dir, source_language, target_language
    )
    report = {
        "schema_version": 1,
        "glossary_dir": str(glossary_dir),
        "source_language": source_language,
        "target_language": target_language,
        "selection_required": bool(candidates),
        "candidates": candidates,
    }
    output_dir = context.output_dir
    output_dir.mkdir(parents=True, exist_ok=True)
    report_path = output_dir / "glossary_candidates.json"
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    eligible = [item for item in candidates if item.get("eligible")]
    logger.info(
        f"发现 {len(candidates)} 个术语表候选，其中 {len(eligible)} 个通过语言和格式检查。"
    )
    logger.success(f"术语表候选报告已写入: {report_path}")
    return 0


__all__ = ["glossary_candidates_command"]
