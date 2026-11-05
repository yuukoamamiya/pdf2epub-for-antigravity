"""Operator-only dispatch recovery commands."""
import json
import sys
from pathlib import Path
from typing import Any
from pdf2epub.batching_service import batching_config

from pdf2epub.commands.runtime import load_book_context
from pdf2epub.subagent_dispatch_recovery import recovery_state_path, snapshot, record_failure, resume


def _run(action):
    try:
        return action()
    except (ValueError, OSError) as exc:
        print(f"dispatch recovery error: {exc}", file=sys.stderr)
        return 2


def _context(args: Any):
    return load_book_context(args, "dispatch-recovery", init_logging=False)


def _inside_output(output_dir: Path, candidate: Path) -> Path:
    root = output_dir.resolve()
    resolved = candidate.resolve()
    if not resolved.is_relative_to(root):
        raise ValueError("dispatch recovery paths must stay inside the book output directory")
    return resolved


def dispatch_status_command(args: Any) -> int:
    return _run(lambda: _dispatch_status(args))


def _dispatch_status(args: Any) -> int:
    context = _context(args)
    if context is None:
        return 2
    path = _inside_output(context.output_dir, recovery_state_path(context.output_dir, args.task))
    configured = batching_config(context.config)["max_concurrency"]
    wave_cap = args.wave_cap
    if wave_cap is not None and not 1 <= wave_cap <= 6:
        raise ValueError("wave-cap must be between 1 and 6")
    if args.wave_reason == "large_units":
        wave_cap = min(wave_cap if wave_cap is not None else 2, 2)
    elif args.wave_reason == "extreme_unit":
        wave_cap = 1
    value = snapshot(path, args.task, configured_cap=configured, wave_cap=wave_cap, available_cap=args.available_cap, active_workers=args.active_workers)
    if args.json:
        print(json.dumps(value, ensure_ascii=False, indent=2))
    else:
        print(f"task={args.task} next_retry_at={value.get('next_retry_at')} cooldown_active={value.get('cooldown_active')} operator_blocked={bool((value.get('task_block') or {}).get('operator_blocked'))} concurrency_cap={value.get('concurrency_cap')} new_worker_slots={value.get('new_worker_slots')} effective_cap={value.get('effective_cap')} (report actual running workers and free slots)")
    return 0


def dispatch_record_failure_command(args: Any) -> int:
    return _run(lambda: _dispatch_record_failure(args))


def _dispatch_record_failure(args: Any) -> int:
    context = _context(args)
    if context is None:
        return 2
    manifest = _inside_output(context.output_dir, context.output_dir / (args.manifest or f"{args.task}_subagent_manifest.json"))
    path = _inside_output(context.output_dir, recovery_state_path(context.output_dir, args.task))
    value = record_failure(path, manifest, args.worker_id, args.kind, expected_assignment_sha256=args.assignment_sha256, retry_after=args.retry_after, reset_at=args.reset_at, actual_peak_concurrency=args.actual_peak_concurrency, temporary_cap=args.temporary_cap)
    print(json.dumps(value, ensure_ascii=False, indent=2))
    return 0


def dispatch_resume_command(args: Any) -> int:
    return _run(lambda: _dispatch_resume(args))


def _dispatch_resume(args: Any) -> int:
    context = _context(args)
    if context is None:
        return 2
    value = resume(_inside_output(context.output_dir, recovery_state_path(context.output_dir, args.task)), args.task, reason=args.reason, confirm_sessions_reconciled=args.confirm_sessions_reconciled, confirm_quota_available=args.confirm_quota_available)
    print(json.dumps(value, ensure_ascii=False, indent=2))
    return 0
