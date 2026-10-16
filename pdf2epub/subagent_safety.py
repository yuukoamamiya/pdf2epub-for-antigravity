"""Safety signal detection for Subagent-produced translation output."""

from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Any, Mapping
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
    (
        "Chinese translation placeholder",
        re.compile(
            r"(?:这是|以下是|以下为|这里是)"
            r"(?:一份|本次|对原文的|对该段(?:文字|内容)?的|"
            r"纯正(?:的)?学术)?"
            r"(?:中文)?(?:翻译结果|翻译内容|翻译文本)"
            r"(?:。|！|!|：|:|$)"
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
        if any(target_match[1].search(_normalize_detection_text(s)) for s in source_lines):
            continue
        start_win = max(0, index - 30)
        end_win = min(len(source_lines), index + 31)
        if any(pattern.search(_normalize_detection_text(source_lines[w])) for w in range(start_win, end_win) for _label, pattern in _REFUSAL_PATTERNS):
            continue
        return f"{target_match[0]} detected at non-empty line {index + 1}"
    return None


def _repository_root(output_dir: Path) -> Optional[Path]:
    """Find the project root without assuming a particular output location."""
    candidate = Path(output_dir).resolve()
    for path in (candidate, *candidate.parents):
        if (path / "pyproject.toml").is_file() and (path / "pdf2epub").is_dir():
            return path
    return None


def protected_workspace_snapshot(output_dir: Path) -> dict[str, Any]:
    """Hash files a content worker is never authorized to modify.

    The snapshot is intentionally limited to source/configuration artifacts;
    generated output, caches, and runtime secrets are outside this guard.
    """
    root = _repository_root(Path(output_dir))
    if root is None:
        return {"available": False, "files": {}}

    paths: set[Path] = set()
    for directory, suffixes in (
        (root / "pdf2epub", {".py"}),
        (root / "tests", {".py"}),
        (root / "docs", {".md"}),
        (root / "glossaries", {".yaml", ".yml", ".json"}),
    ):
        if not directory.is_dir():
            continue
        paths.update(
            path
            for path in directory.rglob("*")
            if path.is_file() and path.suffix.lower() in suffixes
        )
    paths.update(
        path
        for path in (
            root / "AGENTS.md",
            root / "pyproject.toml",
            root / "uv.lock",
        )
        if path.is_file()
    )
    paths.update(
        path
        for path in root.glob("config*.yaml")
        if path.is_file()
    )

    files: dict[str, str] = {}
    for path in sorted(paths):
        try:
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError:
            continue
        files[path.relative_to(root).as_posix()] = digest
    return {"available": True, "files": files}


def protected_workspace_changes(
    output_dir: Path, snapshot: Mapping[str, Any]
) -> list[str]:
    """Return protected paths changed since a hand-off was prepared."""
    if not isinstance(snapshot, Mapping) or snapshot.get("available") is not True:
        return []
    expected = snapshot.get("files")
    if not isinstance(expected, Mapping):
        return []
    current = protected_workspace_snapshot(output_dir).get("files", {})
    if not isinstance(current, Mapping):
        return ["<protected workspace snapshot unavailable>"]
    names = set(str(name) for name in expected) | set(str(name) for name in current)
    return sorted(
        name
        for name in names
        if str(expected.get(name, "")) != str(current.get(name, ""))
    )


__all__ = [
    "detect_refusal",
    "protected_workspace_changes",
    "protected_workspace_snapshot",
]
