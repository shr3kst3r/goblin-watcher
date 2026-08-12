"""JSONL audit log of `gw` command invocations.

`cli.main` wraps each invocation in `record_invocation`, which writes one line
to `paths.logs_dir() / commands.jsonl` with: timestamp, argv, cwd, exit code,
duration, gw version. The `gw history` subcommand reads and prunes this file.
"""

from __future__ import annotations

import json
import os
import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from goblin_watcher import __version__, paths

LOG_FILENAME = "commands.jsonl"

# Set by `description.schedule_if_stale` on the background `_describe`
# subprocess. Those are internal heartbeats, not user-issued commands.
_SUBPROCESS_ENV = "GW_DESCRIBE_SUBPROCESS"


def log_file() -> Path:
    return paths.logs_dir() / LOG_FILENAME


def _now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _write_entry(entry: dict[str, Any]) -> None:
    path = log_file()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a") as f:
            f.write(json.dumps(entry, separators=(",", ":"), ensure_ascii=False) + "\n")
    except OSError:
        # Logging is best-effort; never block the actual command.
        pass


@contextmanager
def record_invocation(argv: list[str]) -> Iterator[dict[str, Any]]:
    """Capture a single `gw` invocation as a JSONL line.

    The yielded dict's ``exit_code`` field should be set by the caller before
    the context exits; leaving it untouched records 0 (success).
    """
    if os.environ.get(_SUBPROCESS_ENV):
        # Internal subprocess — don't pollute the user-facing log.
        yield {"exit_code": 0}
        return
    start = time.monotonic()
    entry: dict[str, Any] = {
        "ts": _now_iso(),
        "argv": list(argv),
        "cwd": str(Path.cwd()),
        "exit_code": 0,
        "duration_ms": 0,
        "version": __version__,
    }
    try:
        yield entry
    finally:
        entry["duration_ms"] = int((time.monotonic() - start) * 1000)
        _write_entry(entry)


def read_entries() -> list[dict[str, Any]]:
    """Every parseable entry from the log file. Malformed lines are skipped."""
    path = log_file()
    if not path.exists():
        return []
    out: list[dict[str, Any]] = []
    for line in path.read_text().splitlines():
        if not line.strip():
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            out.append(obj)
    return out


def _split_by_age(older_than_days: int) -> tuple[list[str], list[str]]:
    """Partition raw log lines into ``(to_remove, to_keep)``.

    Lines without a parseable `ts` are conservatively kept — we'd rather hold
    onto a few stray rows than drop data we can't reason about.
    """
    path = log_file()
    if not path.exists():
        return ([], [])
    cutoff = datetime.now(UTC) - timedelta(days=older_than_days)
    to_remove: list[str] = []
    to_keep: list[str] = []
    for line in path.read_text().splitlines():
        if not line.strip():
            continue
        is_old = False
        try:
            obj = json.loads(line)
            ts_raw = obj.get("ts") if isinstance(obj, dict) else None
            if isinstance(ts_raw, str):
                ts = datetime.fromisoformat(ts_raw.replace("Z", "+00:00"))
                is_old = ts < cutoff
        except json.JSONDecodeError, ValueError, TypeError:
            pass
        (to_remove if is_old else to_keep).append(line)
    return (to_remove, to_keep)


def count_old(older_than_days: int) -> tuple[int, int]:
    """``(would_remove, would_keep)`` without touching the file."""
    if older_than_days < 0:
        raise ValueError("older_than_days must be non-negative")
    to_remove, to_keep = _split_by_age(older_than_days)
    return (len(to_remove), len(to_keep))


def prune(older_than_days: int) -> tuple[int, int]:
    """Drop entries older than `older_than_days` days. Returns ``(removed, kept)``."""
    if older_than_days < 0:
        raise ValueError("older_than_days must be non-negative")
    to_remove, to_keep = _split_by_age(older_than_days)
    if not to_remove:
        return (0, len(to_keep))
    path = log_file()
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text("\n".join(to_keep) + ("\n" if to_keep else ""))
    tmp.replace(path)
    return (len(to_remove), len(to_keep))
