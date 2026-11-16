"""Cross-platform advisory file lock shared by small state registries."""
from __future__ import annotations

import os
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


_THREAD_LOCKS: dict[str, threading.Lock] = {}
_THREAD_LOCKS_GUARD = threading.Lock()


def _thread_lock_for(path: Path) -> threading.Lock:
    key = os.path.normcase(str(path.resolve()))
    with _THREAD_LOCKS_GUARD:
        return _THREAD_LOCKS.setdefault(key, threading.Lock())


@contextmanager
def state_lock(state_path: Path) -> Iterator[None]:
    """Serialize JSON state mutations on Windows and POSIX hosts."""
    lock_path = Path(f"{state_path}.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    # Windows byte-range locks do not serialize threads in the same process
    # consistently, so pair them with a per-path in-process mutex. The file
    # lock still coordinates independent processes.
    with _thread_lock_for(lock_path):
        try:
            # Exclusive creation ensures only one contender initializes the
            # lock byte; existing lock files are never written without a lock.
            handle = lock_path.open("x+b")
        except FileExistsError:
            handle = lock_path.open("r+b")
        with handle:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                if os.fstat(handle.fileno()).st_size == 0:
                    handle.seek(0)
                    handle.write(b"0")
                    handle.flush()
                yield
            finally:
                if os.name == "nt":
                    import msvcrt
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
