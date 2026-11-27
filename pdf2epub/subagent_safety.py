"""Safety signal detection for Subagent-produced translation output."""

from __future__ import annotations

import hashlib
from pdf2epub.workflow_contracts import sha256_file
import re
from pathlib import Path
from typing import Any, Mapping
from typing import Optional

_REFUSAL_PATTERNS = (
    (
        "English refusal",
        re.compile(
            r"\b(?:i|we)\s+(?:cannot|can't|can not|could not|couldn't|won't|will not|must not|mustn't)"
            r"\s+(?:translate|assist|help|provide|answer|comply|fulfill|process|continue|generate|rewrite)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "English refusal",
        re.compile(
            r"\b(?:i am|i'm|i was|we are|we're|we were)\s+unable\s+to\s+"
            r"(?:translate|assist|help|provide|answer|comply|process|continue|generate|rewrite)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "English refusal",
        re.compile(r"\b(?:i\s+must|i\s+have\s+to|i\s+had\s+to)\s+refuse\b", re.IGNORECASE),
    ),
    (
        "English refusal",
        re.compile(
            r"\b(?:i|we)\s+(?:refuse|refused|decline|declined)\s+to\s+"
            r"(?:translate|assist|help|provide|answer|process|continue|generate|rewrite)\b",
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
            r"(?:抱歉|很抱歉)[^，。！？；：,.!?;:\r\n]{0,20}(?:无法|不能|不可以|拒绝)[^，。！？；：,.!?;:\r\n]{0,16}"
            r"(?:翻译|协助翻译|改写|重写|完成(?:这(?:项|个)|该)?(?:请求|任务)|"
            r"(?:提供|生成)[^，。！？；：,.!?;:\r\n]{0,6}(?:译文|帮助|内容|支持)|"
            r"(?:处理|回答)[^，。！？；：,.!?;:\r\n]{0,6}(?:请求|任务|问题|内容|文本)|"
            r"帮助[^，。！？；：,.!?;:\r\n]{0,6}(?:翻译|完成|处理))"
        ),
    ),
    (
        "Chinese refusal",
        re.compile(
            r"(?<![\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff])"
            r"(?:我|本人)(?=[，。、！？；：,.!?;:\s]|无法|不能|不可以|拒绝)"
            r"[^。！？；.!?;:\r\n]{0,6}(?:无法|不能|不可以|拒绝)"
            r"[^，。！？；：,.!?;:\r\n]{0,12}"
            r"(?:翻译|协助翻译|改写|重写|完成(?:这(?:项|个)|该)?(?:请求|任务)|"
            r"(?:提供|生成)[^，。！？；：,.!?;:\r\n]{0,6}(?:译文|帮助|内容|支持)|"
            r"(?:处理|回答)[^，。！？；：,.!?;:\r\n]{0,6}(?:请求|任务|问题|内容|文本)|"
            r"帮助[^，。！？；：,.!?;:\r\n]{0,6}(?:翻译|完成|处理))"
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


def _is_meta_disclaimer_label(label: str) -> bool:
    return "disclaimer" in label or "placeholder" in label


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
    source_lines = [_normalize_detection_text(line) for line in source_text.splitlines() if line.strip()]
    target_lines = [_normalize_detection_text(line) for line in translated_text.splitlines() if line.strip()]
    joined_source_pairs = [
        f"{source_lines[pos]} {source_lines[pos + 1]}"
        for pos in range(max(0, len(source_lines) - 1))
    ]
    for index, target_line in enumerate(target_lines):
        # Always prioritize meta-disclaimers / translation placeholders over
        # generic refusal phrases so character dialogue never exempts AI policy text.
        meta_match = next(
            (
                (label, pattern)
                for label, pattern in _REFUSAL_PATTERNS
                if _is_meta_disclaimer_label(label) and pattern.search(target_line)
            ),
            None,
        )
        target_match = meta_match or next(
            ((label, pattern) for label, pattern in _REFUSAL_PATTERNS if pattern.search(target_line)),
            None,
        )
        if target_match is None:
            continue
        target_label, target_pattern = target_match
        source_line = source_lines[index] if index < len(source_lines) else ""
        if _is_meta_disclaimer_label(target_label):
            # Never let character dialogue or nearby refusal lines exempt an AI/policy meta-disclaimer
            # or translation placeholder unless the aligned source itself carries that meta label.
            matching_patterns = [
                pattern for label, pattern in _REFUSAL_PATTERNS if _is_meta_disclaimer_label(label)
            ]
            if any(pattern.search(source_line) for pattern in matching_patterns):
                continue
            return f"{target_label} detected at non-empty line {index + 1}"

        refusal_patterns = [
            pattern for label, pattern in _REFUSAL_PATTERNS if not _is_meta_disclaimer_label(label)
        ]
        if any(pattern.search(source_line) for pattern in refusal_patterns):
            continue
        if any(target_pattern.search(s) for s in source_lines) or any(
            target_pattern.search(pair) for pair in joined_source_pairs
        ):
            continue
        start_win = max(0, index - 30)
        end_win = min(len(source_lines), index + 31)
        win_pairs = [
            f"{source_lines[w]} {source_lines[w + 1]}"
            for w in range(start_win, max(start_win, end_win - 1))
        ]
        if any(
            pattern.search(candidate)
            for candidate in [*source_lines[start_win:end_win], *win_pairs]
            for pattern in refusal_patterns
        ):
            continue
        return f"{target_label} detected at non-empty line {index + 1}"
    return None


_UNTRUSTED_OPEN = "<untrusted>"
_UNTRUSTED_CLOSE = "</untrusted>"
_UNTRUSTED_REGION_RE = re.compile(
    re.escape(_UNTRUSTED_OPEN) + r".*?" + re.escape(_UNTRUSTED_CLOSE), re.DOTALL
)


def fence_untrusted(text: str, *, label: str = "") -> str:
    """Wrap foreign TOC/glossary text in an explicit untrusted fence.

    Any fence marker embedded in the payload is escaped so the wrapped text
    cannot close the fence early and smuggle instructions into the prompt.
    """
    cleaned = (
        str(text)
        .replace(_UNTRUSTED_OPEN, "&lt;untrusted&gt;")
        .replace(_UNTRUSTED_CLOSE, "&lt;/untrusted&gt;")
    )
    header = _UNTRUSTED_OPEN + "\n" + (f"[{label}]\n" if label else "")
    return f"{header}{cleaned}\n{_UNTRUSTED_CLOSE}"


def _normalize_snippet(text: str) -> str:
    return " ".join(str(text).split())


def untrusted_fence_warnings(
    prompt: str, foreign_texts: Any, *, min_length: int = 3
) -> list[str]:
    """Warn when foreign TOC/glossary text appears outside an untrusted fence.

    This is a diagnostic guard, not a content classifier: it only proves that
    a known foreign snippet leaked into unfenced instruction text. Short
    snippets are skipped because they collide with ordinary prose.
    """
    if isinstance(foreign_texts, str):
        foreign_texts = [foreign_texts]
    unfenced = _UNTRUSTED_REGION_RE.sub(" ", str(prompt))
    normalized_prompt = _normalize_snippet(unfenced)
    if not normalized_prompt:
        return []
    warnings: list[str] = []
    seen: set[str] = set()
    for snippet in foreign_texts or ():
        normalized = _normalize_snippet(snippet)
        if len(normalized) < min_length or normalized in seen:
            continue
        seen.add(normalized)
        if normalized in normalized_prompt:
            warnings.append(
                "foreign text appears outside an <untrusted> fence: "
                + repr(normalized[:120])
            )
    return warnings


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
            digest = sha256_file(path)
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
    "fence_untrusted",
    "protected_workspace_changes",
    "protected_workspace_snapshot",
    "untrusted_fence_warnings",
]
