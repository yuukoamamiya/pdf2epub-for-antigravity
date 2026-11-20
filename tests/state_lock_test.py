"""Tests for the cross-platform state lock helper."""

from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

from pdf2epub.state_lock import state_lock


def test_state_lock_serializes_repeated_acquisitions(tmp_path: Path):
    state_path = tmp_path / "registry.json"
    events: list[str] = []
    with state_lock(state_path, timeout=5.0):
        events.append("first")
    with state_lock(state_path, timeout=5.0):
        events.append("second")

    assert events == ["first", "second"]
    assert Path(f"{state_path}.lock").is_file()


def test_state_lock_times_out_while_another_thread_holds_it(tmp_path: Path):
    state_path = tmp_path / "registry.json"
    holding = threading.Event()
    release = threading.Event()

    def holder() -> None:
        with state_lock(state_path, timeout=5.0):
            holding.set()
            release.wait(10.0)

    thread = threading.Thread(target=holder, daemon=True)
    thread.start()
    try:
        assert holding.wait(5.0), "holder thread never acquired the state lock"
        started = time.monotonic()
        with pytest.raises(TimeoutError, match="waiting for state lock"):
            with state_lock(state_path, timeout=0.05):
                pytest.fail("state lock was acquired while another thread held it")
        elapsed = time.monotonic() - started
        assert elapsed < 2.0
    finally:
        release.set()
        thread.join(10.0)
    assert not thread.is_alive()


def test_retry_until_deadline_covers_posix_contention_semantics():
    """The shared retry loop (used by the POSIX flock branch) honors the deadline."""
    import errno

    from pdf2epub.state_lock import _retry_until_deadline

    def contended() -> None:
        raise BlockingIOError(errno.EAGAIN, "lock is held")

    started = time.monotonic()
    with pytest.raises(TimeoutError, match="waiting for state lock"):
        _retry_until_deadline(0.05, contended)
    assert time.monotonic() - started < 2.0

    def fatal() -> None:
        raise OSError(errno.ENOENT, "missing lock file")

    with pytest.raises(OSError) as excinfo:
        _retry_until_deadline(0.05, fatal)
    assert not isinstance(excinfo.value, TimeoutError)
    assert excinfo.value.errno == errno.ENOENT
