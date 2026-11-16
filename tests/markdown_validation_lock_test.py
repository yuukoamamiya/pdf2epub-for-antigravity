import json
import multiprocessing
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

from pdf2epub.commands import markdown as markdown_commands
from pdf2epub.state_lock import state_lock
from pdf2epub.workflow_contracts import atomic_write_json


def _process_locked_increment(args) -> None:
    report_path, barrier, updates = args
    barrier.wait(timeout=20)
    for _ in range(updates):
        with state_lock(report_path):
            state = json.loads(Path(report_path).read_text(encoding="utf-8"))
            state["count"] += 1
            atomic_write_json(Path(report_path), state)


def test_formal_pdf_markdown_validation_takes_task_lock_but_check_only_does_not(
    tmp_path: Path, monkeypatch,
):
    context = SimpleNamespace(output_dir=tmp_path)
    monkeypatch.setattr(markdown_commands, "load_book_context", lambda *args, **kwargs: context)
    monkeypatch.setattr(
        markdown_commands, "_validate_pdf_markdown_task_impl",
        lambda args, task, *, context=None: 0,
    )
    acquired = []

    @contextmanager
    def recording_lock(path):
        acquired.append(Path(path))
        yield

    monkeypatch.setattr("pdf2epub.state_lock.state_lock", recording_lock)

    assert markdown_commands._validate_pdf_markdown_task(
        SimpleNamespace(check_only=False, file="unit.md"), "polish"
    ) == 0
    assert acquired == [tmp_path / "polish_validation.json"]

    assert markdown_commands._validate_pdf_markdown_task(
        SimpleNamespace(check_only=True, file="unit.md"), "polish"
    ) == 0
    assert acquired == [tmp_path / "polish_validation.json"]


def test_state_lock_preserves_concurrent_read_modify_write_updates(tmp_path: Path):
    report_path = tmp_path / "polish_file_validation.json"
    atomic_write_json(report_path, {"count": 0})
    workers = 6
    updates_per_worker = 40
    start_barrier = threading.Barrier(workers)

    def update() -> None:
        # Release all workers together to create real lock contention, but do
        # not rendezvous on every iteration. Repeated barriers can deadlock the
        # test if an OS-level lock acquisition fails or times out on Windows.
        start_barrier.wait()
        for _ in range(updates_per_worker):
            with state_lock(report_path):
                state = json.loads(report_path.read_text(encoding="utf-8"))
                state["count"] += 1
                atomic_write_json(report_path, state)

    with ThreadPoolExecutor(max_workers=workers) as executor:
        list(executor.map(lambda _: update(), range(workers)))

    final = json.loads(report_path.read_text(encoding="utf-8"))
    assert final["count"] == workers * updates_per_worker


def test_state_lock_serializes_processes_during_first_lockfile_creation(tmp_path: Path):
    report_path = tmp_path / "first-process-validation.json"
    atomic_write_json(report_path, {"count": 0})
    workers = 3
    updates_per_worker = 8
    context = multiprocessing.get_context("spawn")
    with context.Manager() as manager:
        barrier = manager.Barrier(workers)
        with context.Pool(workers) as pool:
            pool.map(
                _process_locked_increment,
                [(str(report_path), barrier, updates_per_worker)] * workers,
            )

    final = json.loads(report_path.read_text(encoding="utf-8"))
    assert final["count"] == workers * updates_per_worker
