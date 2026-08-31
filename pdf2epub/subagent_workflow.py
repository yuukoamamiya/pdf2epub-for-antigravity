"""Generic, local-only hand-offs for Antigravity Subagents.

The repository intentionally has no programmatic translation entry point.
These helpers create explicit source/target contracts and validate files that a
Subagent wrote in the workspace.
"""

from __future__ import annotations

import json
import hashlib
import re
import shutil
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional


DEFAULT_TRANSLATION_MODEL = "gemini-3.1-pro-preview"
DEFAULT_SUBAGENT_MODEL = "gemini-3.6-flash"
DEFAULT_BATCH_MAX_FILES = 5
DEFAULT_BATCH_MAX_SOURCE_TOKENS = 12_000
DEFAULT_BATCH_MAX_CONCURRENCY = 3

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
            r"(?:翻译|协助|帮助|处理|提供|继续|完成)"
        ),
    ),
    (
        "Chinese refusal",
        re.compile(
            r"(?:我|本人).{0,4}(?:无法|不能|不可以|拒绝).{0,15}"
            r"(?:翻译|协助|帮助|处理|提供|继续|完成)"
        ),
    ),
    (
        "Chinese policy disclaimer",
        re.compile(
            r"作为(?:一个)?(?:AI|人工智能|语言模型)|"
            r"(?:无法|不能|不可以|拒绝|抱歉).{0,30}"
            r"(?:安全|内容|使用)政策"
        ),
    ),
)

_TRANSLATION_TASKS = {
    "translate",
    "translate-novel",
    "translate-arxiv",
    "toc-translation",
    "metadata-translation",
}


def resolve_subagent_model(
    config: Optional[Mapping[str, Any]],
    task: str,
) -> str:
    """Resolve the model requested by a workspace Subagent task.

    The model is a task contract for Antigravity, not an API setting.  Exact
    task overrides are supported for exceptional cases; otherwise translation
    tasks use ``models.translation`` and every other task uses
    ``models.default``.
    """
    subagent = config.get("subagent", {}) if isinstance(config, Mapping) else {}
    if not isinstance(subagent, Mapping):
        subagent = {}
    models = subagent.get("models", {})
    if not isinstance(models, Mapping):
        models = {}
    task_models = subagent.get("task_models", {})
    if not isinstance(task_models, Mapping):
        task_models = {}

    for value in (task_models.get(task), models.get(task)):
        if isinstance(value, str) and value.strip():
            return value.strip()

    is_translation = task in _TRANSLATION_TASKS or task.startswith("translate-")
    fallback = DEFAULT_TRANSLATION_MODEL if is_translation else DEFAULT_SUBAGENT_MODEL
    configured = models.get("translation" if is_translation else "default")
    return configured.strip() if isinstance(configured, str) and configured.strip() else fallback


def _markdown_files(directory: Path) -> List[Path]:
    return sorted(path for path in directory.glob("*.md") if path.is_file())


@lru_cache(maxsize=1)
def _get_tokenizer():
    """Load the local tokenizer once for manifest estimates."""
    try:
        import tiktoken

        return tiktoken.get_encoding("cl100k_base")
    except Exception:
        # The estimate is advisory only.  Keep task preparation usable if a
        # downstream installation omits the optional tokenizer package.
        return None


def estimate_tokens(text: str) -> int:
    tokenizer = _get_tokenizer()
    if tokenizer is not None:
        return len(tokenizer.encode(text))
    return max(1, (len(text) + 3) // 4) if text else 0


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


def _positive_int(value: Any, default: int) -> int:
    try:
        value = int(value)
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


def _batching_config(config: Optional[Mapping[str, Any]]) -> Dict[str, int]:
    subagent = config.get("subagent", {}) if isinstance(config, Mapping) else {}
    batching = subagent.get("batching", {}) if isinstance(subagent, Mapping) else {}
    if not isinstance(batching, Mapping):
        batching = {}
    return {
        "max_files": _positive_int(batching.get("max_files"), DEFAULT_BATCH_MAX_FILES),
        "max_source_tokens": _positive_int(
            batching.get("max_source_tokens"), DEFAULT_BATCH_MAX_SOURCE_TOKENS
        ),
        "max_concurrency": _positive_int(
            batching.get("max_concurrency"), DEFAULT_BATCH_MAX_CONCURRENCY
        ),
    }


def _recommended_batches(
    file_stats: Mapping[str, Mapping[str, int]],
    max_files: int,
    max_source_tokens: int,
) -> List[List[str]]:
    """Create advisory, file-safe batches without splitting source lines."""
    batches: List[List[str]] = []
    current: List[str] = []
    current_tokens = 0
    for name, stats in file_stats.items():
        tokens = stats["estimated_tokens"]
        if current and (
            len(current) >= max_files or current_tokens + tokens > max_source_tokens
        ):
            batches.append(current)
            current = []
            current_tokens = 0
        current.append(name)
        current_tokens += tokens
        # An oversized file remains alone; the manifest explicitly flags it
        # so the operator can split it at logical line boundaries if needed.
        if tokens > max_source_tokens:
            batches.append(current)
            current = []
            current_tokens = 0
    if current:
        batches.append(current)
    return batches


def prepare_markdown_subagent(
    output_dir: Path,
    task: str,
    source_dir: Path,
    target_dir: Path,
    source_language: str,
    target_language: str,
    extra_rules: Iterable[str] = (),
    config: Optional[Mapping[str, Any]] = None,
    resume: bool = False,
) -> Dict[str, Path]:
    """Write a manifest and prompt for a markdown Subagent task."""
    source_dir = Path(source_dir)
    target_dir = Path(target_dir)
    sources = _markdown_files(source_dir)
    if not sources:
        raise ValueError(f"No Markdown source units found in {source_dir}")

    model = resolve_subagent_model(config, task)
    batching = _batching_config(config)
    file_stats: Dict[str, Dict[str, int]] = {}
    for source in sources:
        raw = source.read_bytes()
        text = raw.decode("utf-8")
        file_stats[source.name] = {
            "size_bytes": len(raw),
            "line_count": len(text.splitlines()),
            "nonempty_line_count": len([line for line in text.splitlines() if line.strip()]),
            "estimated_tokens": estimate_tokens(text),
        }
    recommended_batches = _recommended_batches(
        file_stats,
        batching["max_files"],
        batching["max_source_tokens"],
    )
    oversized_files = [
        name
        for name, stats in file_stats.items()
        if stats["estimated_tokens"] > batching["max_source_tokens"]
    ]
    validated_files = None
    validation: Dict[str, Any] = {}
    validation_path = output_dir / f"{task}_validation.json"
    if resume and validation_path.is_file():
        try:
            validation = json.loads(validation_path.read_text(encoding="utf-8"))
            if isinstance(validation, dict) and isinstance(validation.get("valid_files"), list):
                validated_files = set(validation["valid_files"])
        except (OSError, json.JSONDecodeError):
            validated_files = None
    completed_files = []
    pending_files = []
    for source in sources:
        target = target_dir / source.name
        source_hash = hashlib.sha256(source.read_bytes()).hexdigest()
        validation_hashes = validation.get("source_sha256", {}) if isinstance(validation, dict) else {}
        is_validated = (
            validated_files is not None
            and source.name in validated_files
            and validation_hashes.get(source.name) == source_hash
        )
        # A non-empty target is not proof of completion: an interrupted
        # Subagent can leave a truncated file behind.  Only a prior local
        # validation with the same source hash is a resumable checkpoint.
        if resume and target.is_file() and target.read_text(encoding="utf-8").strip() and is_validated:
            completed_files.append(source.name)
        else:
            pending_files.append(source.name)
    manifest = {
        "schema_version": 1,
        "workflow": "antigravity-subagent",
        "task": task,
        "source_language": source_language,
        "target_language": target_language,
        "model": model,
        "resume": resume,
        "source_dir": str(source_dir.relative_to(output_dir)),
        "target_dir": str(target_dir.relative_to(output_dir)),
        "files": [path.name for path in sources],
        "file_stats": file_stats,
        "batching": batching,
        "recommended_batches": recommended_batches,
        "oversized_files": oversized_files,
        "completed_files": completed_files,
        "pending_files": pending_files,
    }
    manifest_path = output_dir / f"{task}_subagent_manifest.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

    rules = [
        "Read each source file and write a same-named target file; do not skip files.",
        "Write files directly in the target directory, with no Markdown code fences around the file contents.",
        "Do not rename files, alter the source directory, or create extra output files.",
        "If the model refuses a unit or inserts a safety disclaimer, do not write that refusal as the translation; leave the target absent and report the blocked unit.",
        *extra_rules,
    ]
    prompt_path = output_dir / f"{task}_subagent_prompt.md"
    prompt_path.write_text(
        f"""# {task} Subagent task

Source language: `{source_language}`
Target language: `{target_language}`
Recommended Antigravity model: `{model}`
Source directory: `{manifest['source_dir']}`
Target directory: `{manifest['target_dir']}`

Process only the files listed in `pending_files` in `{manifest_path.name}`.
Files listed in `completed_files` are existing checkpoints. Do not overwrite
them unless a later local validation explicitly reports that file as invalid.
If a target file is incomplete or invalid, replace it completely rather than
appending to it.

Batching guidance:

- Prefer the `recommended_batches` in the manifest.
- Keep each batch at or below {batching['max_files']} files and approximately
  {batching['max_source_tokens']} source tokens; keep oversized files in their
  own Subagent task and split them only at complete source-line boundaries.
- Keep at most {batching['max_concurrency']} Subagent tasks active at once.
- Files with no prior validation report are pending, even when a non-empty
  target file already exists.

Rules:

{chr(10).join(f"- {rule}" for rule in rules)}
""",
        encoding="utf-8",
    )
    return {"manifest": manifest_path, "prompt": prompt_path}


def validate_markdown_subagent(
    output_dir: Path,
    task: str,
    source_dir: Path,
    target_dir: Path,
    structural_patterns: Iterable[str] = (),
    create_validated_copy: bool = True,
) -> Dict:
    """Validate a Subagent markdown hand-off and optionally stage it."""
    source_dir = Path(source_dir)
    target_dir = Path(target_dir)
    sources = _markdown_files(source_dir)
    missing: List[str] = []
    invalid: List[Dict[str, str]] = []
    safety_blocked: List[str] = []
    valid_files: List[str] = []
    source_sha256: Dict[str, str] = {}
    validated_dir = target_dir / "validated"
    # Never leave a previous successful hand-off usable after a later failed
    # validation.  The validated directory is a generated staging area.
    if validated_dir.exists():
        shutil.rmtree(validated_dir)

    for source in sources:
        target = target_dir / source.name
        if not target.exists():
            missing.append(source.name)
            continue
        source_text = source.read_text(encoding="utf-8")
        source_sha256[source.name] = hashlib.sha256(source_text.encode("utf-8")).hexdigest()
        target_text = target.read_text(encoding="utf-8")
        if not target_text.strip():
            invalid.append({"file": source.name, "reason": "target is empty"})
            continue
        refusal = detect_refusal(source_text, target_text)
        if refusal:
            invalid.append(
                {"file": source.name, "reason": f"refusal/disclaimer detected: {refusal}"}
            )
            safety_blocked.append(source.name)
            continue
        for pattern in structural_patterns:
            if len(re.findall(pattern, source_text, flags=re.MULTILINE)) != len(
                re.findall(pattern, target_text, flags=re.MULTILINE)
            ):
                invalid.append({"file": source.name, "reason": f"structural marker mismatch: {pattern}"})
                break
        else:
            valid_files.append(source.name)

        if target_text and "```" in target_text:
            invalid.append(
                {"file": source.name, "reason": "Markdown code fence is not allowed"}
            )
            if source.name in valid_files:
                valid_files.remove(source.name)

    extras = sorted(path.name for path in _markdown_files(target_dir) if path.name not in {p.name for p in sources})
    if extras:
        invalid.extend(
            {"file": name, "reason": "unexpected extra target file"}
            for name in extras
        )
    if create_validated_copy and not missing and not invalid:
        validated_dir.mkdir(parents=True, exist_ok=True)
        for source in sources:
            shutil.copy2(target_dir / source.name, validated_dir / source.name)

    report = {
        "task": task,
        "total": len(sources),
        "completed": len(sources) - len(missing),
        "missing": missing,
        "invalid": invalid,
        "safety_blocked": safety_blocked,
        "extra": extras,
        "valid_files": valid_files,
        "source_sha256": source_sha256,
        "validated_dir": str(validated_dir),
        "all_passed": bool(sources) and not missing and not invalid and not extras,
    }
    (output_dir / f"{task}_validation.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return report


def prepare_toc_translation_subagent(
    output_dir: Path,
    source_language: str,
    target_language: str,
    config: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Path]:
    """Create a file contract for translating the PDF TOC in the workspace."""
    output_dir = Path(output_dir)
    source_path = output_dir / "toc_tree.json"
    if not source_path.exists():
        raise ValueError(f"TOC source not found: {source_path}")
    try:
        source = json.loads(source_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Invalid TOC source: {exc}") from exc
    if not isinstance(source, dict) or not isinstance(source.get("chapters"), list):
        raise ValueError("toc_tree.json must contain a chapters array")

    source_contract = output_dir / "toc_translation_source.json"
    source_contract.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "workflow": "antigravity-subagent",
                "source_language": source_language,
                "target_language": target_language,
                "model": resolve_subagent_model(config, "toc-translation"),
                "source_file": "toc_tree.json",
                "output_file": "toc_tree_translated.json",
                "toc": source,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    prompt_path = output_dir / "toc_translation_prompt.md"
    prompt_path.write_text(
        f"""# PDF TOC translation

Recommended Antigravity model: `{resolve_subagent_model(config, "toc-translation")}`

Read `toc_translation_source.json` and write `toc_tree_translated.json` in the
same directory. Translate the book and chapter titles from {source_language}
to {target_language}. Preserve every field except `book_title` and node
`title`; preserve the complete tree, order, page ranges, levels,
`boundary_info`, types, and all other metadata.

Return valid JSON only. Do not add Markdown fences or commentary.
""",
        encoding="utf-8",
    )
    return {"source": source_contract, "prompt": prompt_path}


def validate_toc_translation_subagent(output_dir: Path) -> Dict:
    """Validate translated TOC structure without contacting a model."""
    output_dir = Path(output_dir)
    source_path = output_dir / "toc_translation_source.json"
    target_path = output_dir / "toc_tree_translated.json"
    errors: List[str] = []
    if not source_path.exists():
        errors.append("toc_translation_source.json is missing")
    if not target_path.exists():
        errors.append("toc_tree_translated.json is missing")
    if errors:
        return {"valid": False, "errors": errors}
    try:
        source_contract = json.loads(source_path.read_text(encoding="utf-8"))
        target = json.loads(target_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return {"valid": False, "errors": [f"invalid TOC JSON: {exc}"]}

    source = source_contract.get("toc")
    if not isinstance(source, dict) or not isinstance(target, dict):
        return {"valid": False, "errors": ["TOC documents must be JSON objects"]}
    if target.get("schema_version") != source.get("schema_version"):
        errors.append("schema_version changed")
    if "book_title" in source:
        if not isinstance(target.get("book_title"), str) or not target["book_title"].strip():
            errors.append("book_title is missing or empty")
    for key, value in source.items():
        if key in {"book_title", "chapters"}:
            continue
        if target.get(key) != value:
            errors.append(f"top-level field {key!r} changed")

    def compare_nodes(source_nodes: Any, target_nodes: Any, path: str) -> None:
        if not isinstance(source_nodes, list) or not isinstance(target_nodes, list):
            errors.append(f"{path} must remain an array")
            return
        if len(source_nodes) != len(target_nodes):
            errors.append(f"{path} entry count changed")
            return
        for index, (source_node, target_node) in enumerate(zip(source_nodes, target_nodes)):
            node_path = f"{path}[{index}]"
            if not isinstance(source_node, dict) or not isinstance(target_node, dict):
                errors.append(f"{node_path} must remain an object")
                continue
            if not isinstance(target_node.get("title"), str) or not target_node["title"].strip():
                errors.append(f"{node_path}.title is missing or empty")
            for key, value in source_node.items():
                if key in {"title", "children"}:
                    continue
                if target_node.get(key) != value:
                    errors.append(f"{node_path}.{key} changed")
            compare_nodes(source_node.get("children", []), target_node.get("children", []), f"{node_path}.children")

    compare_nodes(source.get("chapters", []), target.get("chapters", []), "chapters")
    return {
        "valid": not errors,
        "errors": errors,
        "source": str(source_path),
        "translated": str(target_path),
    }
