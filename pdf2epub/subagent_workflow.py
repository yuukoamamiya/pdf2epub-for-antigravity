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
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional


DEFAULT_TRANSLATION_MODEL = "gemini-2.5-pro"
DEFAULT_SUBAGENT_MODEL = "gemini-2.5-flash"

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
        if resume and target.is_file() and target.read_text(encoding="utf-8").strip() and (
            is_validated or validated_files is None
        ):
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
        "completed_files": completed_files,
        "pending_files": pending_files,
    }
    manifest_path = output_dir / f"{task}_subagent_manifest.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

    rules = [
        "Read each source file and write a same-named target file; do not skip files.",
        "Write files directly in the target directory, with no Markdown code fences around the file contents.",
        "Do not rename files, alter the source directory, or create extra output files.",
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
        for pattern in structural_patterns:
            if len(re.findall(pattern, source_text, flags=re.MULTILINE)) != len(
                re.findall(pattern, target_text, flags=re.MULTILINE)
            ):
                invalid.append({"file": source.name, "reason": f"structural marker mismatch: {pattern}"})
                break
        else:
            valid_files.append(source.name)

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
