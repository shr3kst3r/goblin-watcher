"""Advisory cross-process file locks (ADR 0004).

Every state write in `gw` is a whole-document read-modify-write, and gw
routinely runs several processes at once (parallel agents, detached
`_describe` subprocesses, scheduled `gw sync` passes). Without
serialization those overlap as last-writer-wins lost updates.

Locks are taken on **stable sidecar files**, never on the data file itself:
`state._atomic_write_text` renames a temp file over the target, so a lock
held on the data file's inode would not survive a write by anyone else.
Sidecars are empty, created on demand, and never deleted.

`fcntl.flock` is POSIX-only, which matches gw's supported platforms (macOS,
Linux). Locks release automatically when the holding process dies, so a
crash never leaves a stuck lock.
"""

from __future__ import annotations

import fcntl
import os
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from goblin_watcher.errors import GoblinError

# Locks wrap read + mutate + write of a small JSON document, so they are held
# for milliseconds. Waiting longer than this means another process is wedged;
# failing loudly beats deadlocking an interactive command.
DEFAULT_TIMEOUT_SECONDS = 5.0

_POLL_SECONDS = 0.02


class LockTimeoutError(GoblinError):
    """Raised when a lock could not be acquired within its timeout."""


@contextmanager
def exclusive(lock_path: Path, timeout: float = DEFAULT_TIMEOUT_SECONDS) -> Iterator[None]:
    """Hold an exclusive advisory lock on `lock_path` for the duration of the block.

    `timeout=0` polls once and raises `LockTimeoutError` immediately if the lock is
    already held — the single-instance-guard idiom used by `gw sync`.
    """
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(lock_path), os.O_RDWR | os.O_CREAT, 0o644)
    deadline = time.monotonic() + timeout
    try:
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise LockTimeoutError(
                        f"Timed out after {timeout:g}s waiting for lock {lock_path.name}.",
                        hint="Another gw process is writing this record. Retry in a moment.",
                    ) from None
                time.sleep(_POLL_SECONDS)
        try:
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)
