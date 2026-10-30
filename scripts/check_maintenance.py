#!/usr/bin/env python3
"""Run the repository's local maintenance acceptance checks in order."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CHECKS = (
    ("pytest", ("uv", "run", "pytest", "-q")),
    (
        "workflow contract",
        ("uv", "run", "python", "scripts/check_workflow_contract.py"),
    ),
    (
        "pinned Ruff undefined-name checks",
        (
            "uvx",
            "--from",
            "ruff==0.14.1",
            "ruff",
            "check",
            "pdf2epub",
            "--select",
            "F821,F822,F823",
        ),
    ),
    ("git diff whitespace check", ("git", "diff", "--check")),
)


def main() -> int:
    for name, command in CHECKS:
        print(f"\n[maintenance] {name}: {' '.join(command)}", flush=True)
        try:
            result = subprocess.run(command, cwd=ROOT, check=False)
        except FileNotFoundError as exc:
            print(
                f"[maintenance] ERROR: required command {command[0]!r} is unavailable: {exc}",
                file=sys.stderr,
                flush=True,
            )
            return 127
        if result.returncode:
            print(
                f"[maintenance] FAILED: {name} exited with status {result.returncode}; later checks were not run.",
                file=sys.stderr,
                flush=True,
            )
            return result.returncode
        print(f"[maintenance] passed: {name}", flush=True)

    print("\n[maintenance] all local acceptance checks passed.", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
