import json
import sys
from pathlib import Path
from types import SimpleNamespace

from pdf2epub.commands import dispatch_recovery as command
from pdf2epub.cli import main
from pdf2epub.worker_prompt import render_dispatch_operator_prompt


def test_status_is_read_only_and_calculates_effective_cap(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(command, "_context", lambda args: SimpleNamespace(output_dir=tmp_path, config={"subagent": {"batching": {"max_concurrency": 4}}}))
    args = SimpleNamespace(task="translate", json=True, wave_cap=None, wave_reason="large_units", available_cap=3, active_workers=2)
    assert command.dispatch_status_command(args) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["effective_cap"] == 2
    assert out["concurrency_cap"] == 2
    assert out["new_worker_slots"] == 0
    assert list(tmp_path.iterdir()) == []


def test_real_cli_record_status_resume_and_bad_retry_after(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config.yaml").write_text("title: Dispatch Book\nsubagent:\n  batching:\n    max_concurrency: 4\n", encoding="utf-8")
    out = tmp_path / "output" / "Dispatch Book"
    out.mkdir(parents=True)
    digest = "a" * 64
    (out / "translate_subagent_manifest.json").write_text(json.dumps({"task": "translate", "worker_handoffs": [{"worker_id": "worker_001", "assignment_sha256": digest, "assigned_files": ["a.md"], "status": "pending"}]}), encoding="utf-8")
    common = ["pdf2epub", "-c", "config.yaml"]
    monkeypatch.setattr(sys, "argv", common + ["dispatch-record-failure", "--task", "translate", "--worker-id", "worker_001", "--assignment-sha256", digest, "--kind", "rate_limit", "--retry-after", "0", "--actual-peak-concurrency", "2", "--temporary-cap", "2"])
    assert main() == 0
    recorded = json.loads(capsys.readouterr().out)
    assert recorded["next_retry_at"] == recorded["task_next_retry_at"]
    assert recorded["temporary_cap_effective"] == 2
    monkeypatch.setattr(sys, "argv", common + ["dispatch-status", "--task", "translate", "--available-cap", "0", "--active-workers", "2", "--json"])
    assert main() == 0
    status = json.loads(capsys.readouterr().out)
    assert status["effective_cap"] == 0
    assert status["new_worker_slots"] == 0
    monkeypatch.setattr(sys, "argv", common + ["dispatch-resume", "--task", "translate", "--reason", "sessions reconciled", "--confirm-sessions-reconciled"])
    assert main() == 0
    assert json.loads(capsys.readouterr().out)["temporary_cap_before"] == 2
    monkeypatch.setattr(sys, "argv", common + ["dispatch-record-failure", "--task", "translate", "--worker-id", "worker_001", "--assignment-sha256", digest, "--kind", "rate_limit", "--retry-after", "999999999999999999999999999999999999999", "--actual-peak-concurrency", "1"])
    assert main() == 2
    assert "Retry-After" in capsys.readouterr().err


def test_guide_shell_quotes_config_path_with_spaces():
    guide = render_dispatch_operator_prompt(task="translate-html", manifest_path="manifest.json", handoffs=[], dispatch_plan=[], config_path="configs/my book.yaml")
    assert "-c 'configs/my book.yaml'" in guide
