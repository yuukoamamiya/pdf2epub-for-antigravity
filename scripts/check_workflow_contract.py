#!/usr/bin/env python3
"""Fail fast when the public CLI and maintenance contracts drift."""

from __future__ import annotations

import argparse
from pathlib import Path

from pdf2epub.commands.registry import register_command_parsers
from pdf2epub.workflow_contract import CLI_COMMANDS


ROOT = Path(__file__).resolve().parents[1]
REQUIRED_PATHS = (
    "AGENTS.md",
    "README.md",
    "docs/architecture.md",
    "docs/antigravity-workflow.md",
    "pdf2epub/commands/refine.py",
    "pdf2epub/refine/main.py",
    "pdf2epub/commands/diagnostics.py",
)
ACTIVE_SCAN_PATHS = (
    ROOT / "pdf2epub/ocr/backends",
    ROOT / "pdf2epub/ocr_backends.py",
    ROOT / "pdf2epub/ocr_pages.py",
    ROOT / "pdf2epub/commands/ocr.py",
    ROOT / "pdf2epub/local_credentials.py",
    ROOT / "config.yaml.example",
    ROOT / "config_epub.yaml.example",
    ROOT / "web",
    ROOT / "pyproject.toml",
)


def _registered_commands() -> set[str]:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command")
    register_command_parsers(subparsers)
    return set(subparsers.choices)


def _active_retired_backend_refs() -> list[str]:
    findings: list[str] = []
    for root in ACTIVE_SCAN_PATHS:
        paths = [root] if root.is_file() else root.rglob("*")
        for path in paths:
            if not path.is_file() or path.suffix.lower() not in {".py", ".toml", ".yaml", ".yml", ".html", ".js"}:
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except (OSError, UnicodeError):
                continue
            if "mistral" in text.lower() or "mistralai" in text.lower():
                findings.append(path.relative_to(ROOT).as_posix())
    return sorted(set(findings))


def main() -> int:
    errors: list[str] = []
    actual = _registered_commands()
    if actual != set(CLI_COMMANDS):
        errors.append(
            "CLI command contract mismatch: "
            f"missing={sorted(set(CLI_COMMANDS) - actual)}, "
            f"unexpected={sorted(actual - set(CLI_COMMANDS))}"
        )
    for relative in REQUIRED_PATHS:
        if not (ROOT / relative).is_file():
            errors.append(f"required maintenance path is missing: {relative}")
    retired_refs = _active_retired_backend_refs()
    if retired_refs:
        errors.append(
            "retired Mistral backend reference found in active project files: "
            + ", ".join(retired_refs)
        )
    if errors:
        for error in errors:
            print(f"ERROR: {error}")
        return 1
    print("Workflow contract check passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
