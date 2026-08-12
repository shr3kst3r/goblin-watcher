"""Append-only JSONL journal of sync activity.

The single source of truth for what background passes did: every step outcome,
every notification, every error. `gw sync watch` follows it live and
`gw sync status` summarizes from it, so a scheduled pass is never invisible —
the gap that makes the existing `_describe` subprocess undebuggable.

Single-line appends to an `O_APPEND` file are atomic enough under POSIX for the
short records written here, the same posture as `command_log.py`. Write failures
are swallowed: losing an observability record must never fail a sync pass.
"""

from __future__ import annotations

import json
import os
import time
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from goblin_watcher import paths

Level = Literal["info", "action", "notify", "error"]


def _now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def append(
    *,
    pass_id: str,
    level: Level,
    event: str,
    project: str | None = None,
    task: str | None = None,
    detail: str | None = None,
) -> None:
    record: dict[str, Any] = {
        "ts": _now_iso(),
        "pass_id": pass_id,
        "level": level,
        "event": event,
    }
    if project is not None:
        record["project"] = project
    if task is not None:
        record["task"] = task
    if detail is not None:
        record["detail"] = detail
    line = json.dumps(record, default=str) + "\n"
    try:
        path = paths.sync_journal_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a") as f:
            f.write(line)
    except OSError:
        pass


def read_entries(limit: int | None = None) -> list[dict[str, Any]]:
    """Parse the journal, newest last. Unparseable lines are skipped."""
    path = paths.sync_journal_file()
    if not path.exists():
        return []
    try:
        lines = path.read_text().splitlines()
    except OSError:
        return []
    if limit is not None:
        lines = lines[-limit:]
    out: list[dict[str, Any]] = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            out.append(parsed)
    return out


def _split_by_age(older_than_days: int) -> tuple[list[str], list[str]]:
    """Partition raw journal lines into ``(to_remove, to_keep)``.

    Lines without a parseable `ts` are kept: a record we can't date is not one
    we should silently drop. Mirrors `command_log._split_by_age`.
    """
    path = paths.sync_journal_file()
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
                is_old = datetime.fromisoformat(ts_raw.replace("Z", "+00:00")) < cutoff
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
    """Drop records older than N days. Returns ``(removed, kept)``.

    A pass every few minutes forever is an unbounded file otherwise. The rewrite
    goes through a temp file plus rename, so a concurrent `follow()` sees a new
    inode and reopens rather than reading a half-written journal.
    """
    if older_than_days < 0:
        raise ValueError("older_than_days must be non-negative")
    to_remove, to_keep = _split_by_age(older_than_days)
    if not to_remove:
        return (0, len(to_keep))
    path = paths.sync_journal_file()
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text("\n".join(to_keep) + ("\n" if to_keep else ""))
    tmp.replace(path)
    return (len(to_remove), len(to_keep))


def follow(poll_seconds: float = 0.5) -> Iterator[dict[str, Any]]:
    """Yield journal records as they are appended, starting from the end.

    Handles the file not existing yet (sync never ran) and being replaced or
    truncated underneath us (journal pruning) by reopening from the start.
    """
    path = paths.sync_journal_file()
    handle = None
    inode: int | None = None
    buffer = ""
    try:
        while True:
            if handle is None:
                if not path.exists():
                    time.sleep(poll_seconds)
                    continue
                handle = path.open("r")
                handle.seek(0, os.SEEK_END)
                inode = os.fstat(handle.fileno()).st_ino
                buffer = ""
            chunk = handle.read()
            if chunk:
                buffer += chunk
                while "\n" in buffer:
                    line, buffer = buffer.split("\n", 1)
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        parsed = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(parsed, dict):
                        yield parsed
                continue
            # No new data: check whether the file was rotated or truncated.
            try:
                current = path.stat()
            except OSError:
                handle.close()
                handle = None
                continue
            if current.st_ino != inode or current.st_size < handle.tell():
                handle.close()
                handle = None
                continue
            time.sleep(poll_seconds)
    finally:
        if handle is not None:
            handle.close()
