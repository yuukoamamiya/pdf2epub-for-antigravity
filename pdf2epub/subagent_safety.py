"""Safety signal detection for Subagent-produced translation output."""

from __future__ import annotations

import re
from typing import Optional

_REFUSAL_PATTERNS = (
    (
        "English refusal",
        re.compile(
            r"\b(?:i|we)\s+(?:cannot|can't|can not|won't|will not|must not|mustn't)"
            r"\s+(?:translate|assist|help|provide|comply|fulfill|process|continue|generate|rewrite)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "English refusal",
        re.compile(
            r"\b(?:i am|i'm)\s+unable\s+to\s+"
            r"(?:translate|assist|help|provide|comply|process|continue|generate|rewrite)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "English refusal",
        re.compile(r"\b(?:i\s+must|i\s+have\s+to)\s+refuse\b", re.IGNORECASE),
    ),
    (
        "English refusal",
        re.compile(
            r"\b(?:i|we)\s+(?:refuse|decline)\s+to\s+"
            r"(?:translate|assist|help|provide|process|continue|generate|rewrite)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "English policy disclaimer",
        re.compile(
            r"\b(?:as\s+an?\s+ai|as\s+a\s+language\s+model)\b|"
            r"\b(?:cannot|can't|unable|refuse).{0,50}\b(?:safety|content)\s+"
            r"(?:policy|policies|guidelines?)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "Chinese refusal",
        re.compile(
            r"(?:抱歉|很抱歉).{0,25}(?:无法|不能|不可以|拒绝).{0,20}"
            r"(?:翻译|协助|帮助|处理|提供|改写|重写|生成|回答|"
            r"完成(?:这(?:项|个)|该)?(?:请求|任务))"
        ),
    ),
    (
        "Chinese refusal",
        re.compile(
            r"(?<![\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff])"
            r"(?:我|本人)(?=[，。、！？；：,.!?;:\s]|无法|不能|不可以|拒绝)"
            r"(?:无法|不能|不可以|拒绝).{0,20}"
            r"(?:翻译|协助|帮助|处理|提供|改写|重写|生成|回答|"
            r"完成(?:这(?:项|个)|该)?(?:请求|任务))"
        ),
    ),
    (
        "Chinese policy disclaimer",
        re.compile(
            r"作为(?:一个)?(?:AI|人工智能|语言模型)"
            r"(?:[，,]\s*(?:我|本人)\s*(?:无法|不能|不可以|拒绝)|"
            r"[，,]?\s*(?:无法|不能|不可以|拒绝)\s*"
            r"(?:翻译|协助|帮助|处理|提供|改写|重写|生成|回答|完成)|"
            r"[，,]\s*(?:会|旨在)\s*(?:遵循|确保|保护|提供|协助))|"
            r"(?:无法|不能|不可以|拒绝|抱歉).{0,30}"
            r"(?:安全|内容|使用)政策"
        ),
    ),
)





def _normalize_detection_text(text: str) -> str:
    return (
        text.replace("\u2018", "'")
        .replace("\u2019", "'")
        .replace("\u201c", '"')
        .replace("\u201d", '"')
    )


def detect_refusal(source_text: str, translated_text: str) -> Optional[str]:
    """Detect high-confidence model refusal text in a candidate translation.

    This is intentionally conservative: a match is reported only when the
    corresponding source line does not contain the same refusal/disclaimer
    signal.  Thus a book character saying "I cannot help" can still be
    translated normally, while a model-generated "I cannot translate this"
    replacing ordinary source text is rejected.
    """
    source_lines = [line for line in source_text.splitlines() if line.strip()]
    target_lines = [line for line in translated_text.splitlines() if line.strip()]
    for index, target_line in enumerate(target_lines):
        target_line = _normalize_detection_text(target_line)
        target_match = next(
            ((label, pattern) for label, pattern in _REFUSAL_PATTERNS if pattern.search(target_line)),
            None,
        )
        if target_match is None:
            continue
        source_line = source_lines[index] if index < len(source_lines) else ""
        source_line = _normalize_detection_text(source_line)
        if any(pattern.search(source_line) for _label, pattern in _REFUSAL_PATTERNS):
            continue
        return f"{target_match[0]} detected at non-empty line {index + 1}"
    return None

__all__ = ["detect_refusal"]
