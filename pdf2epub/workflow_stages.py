"""Canonical PDF workflow stage table shared by the CLI and diagnostics.

The stage order used to be written out by hand in ``AGENTS.md``,
``docs/architecture.md``, ``cli.py``'s ``--help`` epilog and
``commands/diagnostics.py``.  Those copies drifted (the epilog still ended with
``build-epub --translated`` and omitted ``status``/``check-ready`` and the
dispatch commands).  This module owns the ordered table; the CLI epilog is
rendered from it, and ``tests/workflow_stages_test.py`` fails if diagnostics or
the registered subcommands stop matching it.

Operator prose still lives in ``AGENTS.md``; this table is the machine-readable
projection so the executable help cannot silently disagree with it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class WorkflowStage:
    """One ordered stage of the PDF pipeline.

    ``key`` is the name reported by ``pdf2epub status``.  ``command`` is the
    CLI subcommand that drives the stage.  ``invocation`` is the canonical
    command line suffix (what follows ``-c <config>``).  ``note`` is an
    optional operator comment rendered in ``--help`` around the command.
    """

    key: str
    command: str
    invocation: str
    summary: str
    note: Optional[str] = None
    requires_translation: bool = False
    when_secondary_ocr: bool = False


PDF_WORKFLOW_STAGES: tuple[WorkflowStage, ...] = (
    WorkflowStage(
        key="status",
        command="status",
        invocation="status",
        summary="read-only stage, blocker, and next-command report",
        note="开始/恢复前先看只读诊断；doctor 检查配置、依赖与 OCR 后端",
    ),
    WorkflowStage(
        key="ocr-pages",
        command="ocr-pages",
        invocation="ocr-pages --resume",
        summary="visual OCR of every page (dual OCR when configured)",
    ),
    WorkflowStage(
        key="ocr-correct",
        command="ocr-correct",
        invocation="ocr-correct",
        summary="visual correction of OCR pages flagged by consensus or anomalies",
        note="仅双 OCR，或单 OCR 检出疑似异常页时需要；Subagent 写 ocr_corrected_pages/*.md",
        when_secondary_ocr=True,
    ),
    WorkflowStage(
        key="ocr-correct-validate",
        command="ocr-correct-validate",
        invocation="ocr-correct-validate",
        summary="validate reviewed OCR pages",
        note="跳过本阶段当且仅当没有需要复核的页",
        when_secondary_ocr=True,
    ),
    WorkflowStage(
        key="refine-prepare",
        command="refine-prepare",
        invocation="refine-prepare",
        summary="prepare TOC structure review",
        note="Subagent 写 toc_tree.json 与 toc_structure_decisions.json",
    ),
    WorkflowStage(
        key="illustration",
        command="illustration-prepare",
        invocation="illustration-prepare",
        summary="detect and bind full-page illustration candidates",
        note="有候选时 Subagent 写 illustration_decisions.json；随后 validate → apply",
    ),
    WorkflowStage(
        key="illustration-validate",
        command="illustration-validate",
        invocation="illustration-validate",
        summary="validate illustration decisions",
    ),
    WorkflowStage(
        key="illustration-apply",
        command="illustration-apply",
        invocation="illustration-apply",
        summary="apply validated illustration bindings",
    ),
    WorkflowStage(
        key="refine-local",
        command="refine-local",
        invocation="refine-local --resume",
        summary="merge OCR pages into refined Markdown units",
    ),
    WorkflowStage(
        key="footnote",
        command="footnote-prepare",
        invocation="footnote-prepare",
        summary="detect page-footnote and endnote candidates",
        note="需要复核时 Subagent 写 footnote_decisions.json；随后 validate → apply",
    ),
    WorkflowStage(
        key="footnote-validate",
        command="footnote-validate",
        invocation="footnote-validate",
        summary="validate footnote decisions against current evidence",
    ),
    WorkflowStage(
        key="footnote-apply",
        command="footnote-apply",
        invocation="footnote-apply",
        summary="materialize normalized footnote Markdown",
    ),
    WorkflowStage(
        key="polish",
        command="polish",
        invocation="polish",
        summary="prepare structural polish hand-off (required for every PDF run)",
        note="Subagent 写 polished_markdown/*.md；每个文件先跑 polish-validate --file <f> --check-only",
    ),
    WorkflowStage(
        key="polish-validate",
        command="polish-validate",
        invocation="polish-validate",
        summary="validate polished Markdown and rebuild the receipt",
    ),
    WorkflowStage(
        key="extract-entities",
        command="extract-entities",
        invocation="extract-entities",
        summary="prepare the book entity/terminology hand-off",
        note="纯转换流程（pipeline: epub_conversion）跳过；Subagent 写 translation_entities.json",
        requires_translation=True,
    ),
    WorkflowStage(
        key="translate-toc",
        command="translate-toc",
        invocation="translate-toc",
        summary="prepare the independent TOC translation hand-off",
        note="Subagent 写 toc_translation_result.json；本地合并为 toc_tree_translated.json",
        requires_translation=True,
    ),
    WorkflowStage(
        key="check-ready",
        command="check-ready",
        invocation="check-ready --stage translate",
        summary="full pre-flight gate before body translation",
        requires_translation=True,
    ),
    WorkflowStage(
        key="translate",
        command="translate",
        invocation="translate --resume",
        summary="prepare body-translation worker hand-offs",
        note="按 manifest 的 dispatch_plan 分波派发；每个 worker 只写自己 assigned_files 的译文",
        requires_translation=True,
    ),
    WorkflowStage(
        key="translate-validate",
        command="translate-validate",
        invocation="translate-validate",
        summary="validate translated Markdown and write the receipt",
        requires_translation=True,
    ),
    WorkflowStage(
        key="package",
        command="build-epub",
        invocation="build-epub --only-translated",
        summary="build the delivered EPUB (translated name for translation runs)",
    ),
)

PDF_STAGE_BY_KEY = {stage.key: stage for stage in PDF_WORKFLOW_STAGES}


def stage_invocation(key: str) -> str:
    """Return the canonical invocation for one stage key."""
    try:
        return PDF_STAGE_BY_KEY[key].invocation
    except KeyError as exc:  # pragma: no cover - guarded by tests
        raise KeyError(f"unknown workflow stage: {key}") from exc


def _render_stage(stage: WorkflowStage) -> list[str]:
    lines: list[str] = []
    if stage.note:
        lines.append(f"  # {stage.note}")
    lines.append(f"  pdf2epub -c config.yaml {stage.invocation}")
    return lines


def render_pdf_workflow(*, include_translation: bool = True) -> str:
    """Render the PDF workflow block used by ``--help``."""
    lines = ["  # PDF（翻译）:" if include_translation else "  # PDF（纯转换）:"]
    for stage in PDF_WORKFLOW_STAGES:
        if stage.requires_translation and not include_translation:
            continue
        if stage.key in {"extract-entities", "translate-toc", "translate", "translate-validate"}:
            continue
        lines.extend(_render_stage(stage))
    return "\n".join(lines)


def render_cli_epilog() -> str:
    """Render the full ``--help`` epilog from the canonical stage table."""
    translation_lines = ["  # PDF（翻译）:"]
    for stage in PDF_WORKFLOW_STAGES:
        if stage.key == "status":
            continue
        translation_lines.extend(_render_stage(stage))

    conversion_lines = ["", "  # PDF（纯转换，pipeline: epub_conversion）:"]
    for stage in PDF_WORKFLOW_STAGES:
        if stage.requires_translation or stage.key == "status":
            continue
        if stage.command == "build-epub":
            conversion_lines.append("  pdf2epub -c config.yaml check-ready --stage package")
        conversion_lines.extend(_render_stage(stage))

    return "\n".join(
        [
            "",
            "===============================================================================",
            "RECOMMENDED WORKFLOW / 推荐工作流（本段由 pdf2epub/workflow_stages.py 渲染）",
            "===============================================================================",
            "  先运行 status 获取当前阶段与下一步；doctor 检查配置、依赖和 OCR 后端。",
            "",
            *translation_lines,
            *conversion_lines,
            "",
            "  # 已有译文的页眉页脚修复:",
            "  pdf2epub repair-page-furniture",
            "  # Subagent 定点修复 translated/*.md",
            "  pdf2epub repair-page-furniture-validate",
            "  pdf2epub build-epub --only-translated",
            "",
            "  # 派发额度与限流（429）恢复:",
            "  pdf2epub dispatch-status --task translate",
            "  pdf2epub dispatch-record-failure --task translate --worker-id <id> --assignment-sha256 <sha> --kind rate_limit --actual-peak-concurrency <n>",
            "  pdf2epub dispatch-resume --task translate --reason <why> --confirm-sessions-reconciled --confirm-quota-available",
            "",
            "  # EPUB 高保真翻译:",
            "  pdf2epub html-prepare -i mybook.epub",
            "  # Subagent 写 translated_compressed/* 与 translated_metadata.json",
            "  pdf2epub html-validate",
            "  pdf2epub build-html-epub",
            "",
            "  # 轻小说:",
            "  pdf2epub translate-novel -i mybook.epub",
            "  pdf2epub translate-novel-validate",
            "  pdf2epub build-novel-epub",
            "",
            "  # arXiv / LaTeX:",
            "  pdf2epub translate-arxiv 2503.01800",
            "  pdf2epub translate-arxiv ./latex-source --main-tex paper.tex",
            "===============================================================================",
            "",
        ]
    )


__all__ = [
    "PDF_STAGE_BY_KEY",
    "PDF_WORKFLOW_STAGES",
    "WorkflowStage",
    "render_cli_epilog",
    "render_pdf_workflow",
    "stage_invocation",
]
