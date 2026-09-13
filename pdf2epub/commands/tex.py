"""TeX/arXiv translation command handlers.

These handlers materialize and validate TeX translation units locally;
translation remains delegated to the workspace Subagent.
"""

import json
from pathlib import Path

from loguru import logger

from pdf2epub.commands.runtime import load_command_config


def translate_arxiv_command(args):
    """Materialize a TeX project and prepare a Subagent translation task."""
    from pdf2epub.subagent_runtime import estimate_tokens, resolve_subagent_model
    from pdf2epub.workflow_contracts import (
        is_reusable_checkpoint,
        load_json_object,
    )

    import shutil
    from pdf2epub.tex_translation.arxiv import ArxivSourceResolver, slugify_source_id
    from pdf2epub.tex_translation.document import discover_main_tex, scan_project

    config, _ = load_command_config(args)
    source_language = args.source_language or config.get("tex_translation", {}).get(
        "source_language", "English"
    )
    target_language = args.target_language or config.get("tex_translation", {}).get(
        "target_language", "Simplified Chinese"
    )
    model = resolve_subagent_model(config, "translate-arxiv")
    resolver = ArxivSourceResolver()
    source_id = resolver.source_id(args.source)
    run_dir = Path(args.output_dir) if args.output_dir else Path("output") / "arxiv" / slugify_source_id(source_id)
    run_dir = run_dir.resolve()
    source_dir = run_dir / "source"
    project_dir = run_dir / "project"
    control_dir = run_dir / ".pdf2epub"
    previous_manifest = {}
    previous_manifest_path = control_dir / "tex_subagent_manifest.json"
    if getattr(args, "resume", False) and previous_manifest_path.is_file():
        previous_manifest = load_json_object(previous_manifest_path)
    if not isinstance(previous_manifest, dict):
        previous_manifest = {}
    previous_manifest_units = previous_manifest.get("units", [])
    previous_units = {
        entry.get("id"): entry
        for entry in previous_manifest_units
        if isinstance(entry, dict) and entry.get("id")
    } if isinstance(previous_manifest_units, list) else {}
    previous_validated_ids = previous_manifest.get("validated_units", [])
    previously_validated = (
        set(previous_validated_ids)
        if isinstance(previous_validated_ids, list)
        else set()
    )
    try:
        resolved = resolver.materialize(args.source, source_dir)
        main_tex = discover_main_tex(source_dir, args.main_tex or resolved.suggested_main_tex)
        document = scan_project(
            source_dir,
            main_tex,
            unit_chars=args.unit_chars or config.get("tex_translation", {}).get("unit_chars", 12_000),
            target_language=target_language,
        )
        unit_chars = args.unit_chars or config.get("tex_translation", {}).get("unit_chars", 12_000)
        shutil.copytree(source_dir, project_dir, dirs_exist_ok=True)
        # Materialize the normalized source snapshot (including CJK support
        # injected by scan_project) into the editable project.  Copying the
        # raw archive alone would make a Chinese hand-off fail at XeLaTeX.
        for relative_path, source_text in document.sources.items():
            target_path = (project_dir / relative_path).resolve()
            target_path.relative_to(project_dir.resolve())
            target_path.parent.mkdir(parents=True, exist_ok=True)
            target_path.write_text(source_text, encoding="utf-8")
        source_units_dir = run_dir / "tex_units"
        translated_units_dir = run_dir / "translated_tex_units"
        source_units_dir.mkdir(parents=True, exist_ok=True)
        translated_units_dir.mkdir(parents=True, exist_ok=True)
        for unit in document.units:
            source_unit_path = source_units_dir / f"{unit.id}.md"
            source_unit_path.write_text(unit.source_text, encoding="utf-8")

        unit_entries = []
        for unit in document.units:
            target_name = f"{unit.id}.md"
            entry = unit.manifest_entry()
            entry.update({
                "source_file": f"tex_units/{unit.id}.md",
                "target_file": f"translated_tex_units/{target_name}",
                "size_bytes": len(unit.source_text.encode("utf-8")),
                "line_count": len(unit.source_text.splitlines()),
                "estimated_tokens": estimate_tokens(unit.source_text),
            })
            unit_entries.append(entry)
        completed_units = []
        for entry in unit_entries:
            target_path = translated_units_dir / Path(entry["target_file"]).name
            previous_entry = previous_units.get(entry["id"], {})
            if is_reusable_checkpoint(
                target_path,
                entry["id"],
                entry.get("source_sha256", ""),
                previously_validated,
                {entry["id"]: previous_entry.get("source_sha256", "")},
            ):
                completed_units.append(entry["id"])
        manifest = {
            "schema_version": 1,
            "workflow": "antigravity-subagent",
            "task": "translate-arxiv",
            "source_language": source_language,
            "target_language": target_language,
            "model": model,
            "source_dir": "source",
            "target_dir": "translated_tex_units",
            "project_dir": "project",
            "main_tex": document.main_tex,
            "unit_chars": unit_chars,
            "units": unit_entries,
            "resume": getattr(args, "resume", False),
            "completed_units": completed_units,
            "pending_units": [
                entry["id"] for entry in unit_entries if entry["id"] not in completed_units
            ],
        }
        control_dir.mkdir(parents=True, exist_ok=True)
        (control_dir / "tex_subagent_manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        (control_dir / "tex_subagent_prompt.md").write_text(
            f"""# TeX translation Subagent task

Recommended Antigravity model: `{model}`

Translate only the units listed in `pending_units` in
`tex_subagent_manifest.json` from {source_language} to {target_language}.
Read each `source_file` and write the complete translation to its corresponding
`target_file`. Preserve LaTeX commands, labels, references, formulas, and
document structure. Do not add Markdown fences or commentary. Files listed in
`completed_units` are checkpoints and must not be overwritten unless validation
reports them as invalid. If the model refuses a unit or inserts a safety
disclaimer, do not write that refusal as its translation; report the blocked
unit instead. Do not call an API, modify `../source/`, or edit
`../project/` directly; the local validator reconstructs it from the unit files.
""",
            encoding="utf-8",
        )
        logger.success(f"Prepared TeX Subagent task: {control_dir / 'tex_subagent_prompt.md'}")
        logger.info("完成后运行 pdf2epub translate-arxiv-validate --output-dir <run_dir>")
        return 0
    except Exception as exc:
        logger.error(f"Could not prepare TeX Subagent task: {exc}")
        return 1


def translate_arxiv_validate_command(args):
    """Rebuild and compile TeX from validated Subagent unit files locally."""

    from pdf2epub.subagent_safety import detect_refusal
    from pdf2epub.tex_translation.compiler import TexCompiler
    from pdf2epub.tex_translation.document import scan_project
    from pdf2epub.workflow_contracts import sha256_file

    if not args.output_dir:
        logger.error("--output-dir is required for translate-arxiv-validate")
        return 1
    run_dir = Path(args.output_dir).resolve()
    manifest_path = run_dir / ".pdf2epub" / "tex_subagent_manifest.json"
    if not manifest_path.exists():
        logger.error(f"TeX Subagent manifest not found: {manifest_path}")
        return 1
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if not all("source_file" in unit and "target_file" in unit for unit in manifest.get("units", [])):
            logger.error(
                "This TeX manifest uses the old project-editing format; "
                "rerun translate-arxiv to prepare resumable unit files."
            )
            return 1
        source_dir = run_dir / "source"
        document = scan_project(
            source_dir,
            manifest["main_tex"],
            unit_chars=manifest.get("unit_chars", 12_000),
            target_language=manifest.get("target_language", "Simplified Chinese"),
        )
        document_units = {unit.id: unit for unit in document.units}
        translated = {}
        missing_units = []
        invalid_units = []
        safety_blocked_units = []
        completed_units = []
        run_root = run_dir.resolve()

        def safe_run_path(relative_name: str) -> Path:
            target = (run_root / relative_name).resolve()
            target.relative_to(run_root)
            return target

        for entry in manifest.get("units", []):
            unit_id = entry.get("id", "unknown")
            if unit_id not in document_units:
                invalid_units.append(f"{unit_id}: no matching source unit")
                continue
            source_path = safe_run_path(entry["source_file"])
            target_path = safe_run_path(entry["target_file"])
            if not source_path.is_file() or not target_path.is_file():
                missing_units.append(unit_id)
                continue
            source_text = source_path.read_text(encoding="utf-8")
            target_text = target_path.read_text(encoding="utf-8")
            if sha256_file(source_path) != entry.get("source_sha256"):
                invalid_units.append(f"{unit_id}: source unit changed")
                continue
            if not target_text.strip():
                invalid_units.append(f"{unit_id}: target is empty")
                continue
            refusal = detect_refusal(source_text, target_text)
            if refusal:
                invalid_units.append(f"{unit_id}: refusal/disclaimer detected ({refusal})")
                safety_blocked_units.append(unit_id)
                continue
            if "```" in target_text:
                invalid_units.append(f"{unit_id}: Markdown fence is not allowed")
                continue
            translated[unit_id] = target_text
            completed_units.append(unit_id)

        manifest["completed_units"] = completed_units
        manifest["pending_units"] = [
            entry.get("id") for entry in manifest.get("units", [])
            if entry.get("id") not in completed_units
        ]
        # A non-empty TeX unit is only a candidate until the reconstructed
        # project compiles successfully.  Keep a separate durable checkpoint
        # so --resume never trusts a truncated or compile-breaking file.
        manifest["validated_units"] = []
        manifest["safety_blocked_units"] = safety_blocked_units
        manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        if missing_units or invalid_units:
            if missing_units:
                logger.error(f"Missing translated TeX units: {missing_units[:10]}")
            if invalid_units:
                logger.error(f"Invalid translated TeX units: {invalid_units[:10]}")
            return 1

        project_dir = run_dir / "project"
        project_dir.mkdir(parents=True, exist_ok=True)
        for relative_path, source_text in document.render(translated).items():
            target_path = (project_dir / relative_path).resolve()
            target_path.relative_to(project_dir.resolve())
            target_path.parent.mkdir(parents=True, exist_ok=True)
            target_path.write_text(source_text, encoding="utf-8")
        result = TexCompiler(timeout_seconds=args.compile_timeout or 180).compile(
            project_dir,
            manifest["main_tex"],
            run_dir / ".pdf2epub" / "logs" / "subagent_compile.log",
        )
        if not result.success:
            logger.error(f"TeX validation failed:\n{result.tail()}")
            return 1
        manifest["validated_units"] = completed_units
        manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        logger.success(f"TeX Subagent output compiled successfully: {result.pdf_path}")
        return 0
    except Exception as exc:
        logger.error(f"TeX validation failed: {exc}")
        return 1
