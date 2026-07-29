"""Persistence for sync state and the indicator cache.

Both files live in the global data tier under `sync/`. Writes reuse
`state._atomic_write_text` semantics via `state` helpers so a crash mid-write
never leaves a torn file, and both are only ever written by the process holding
the sync pass lock — so no per-file locking is needed here.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from goblin_watcher import paths, state
from goblin_watcher.sync.models import IndicatorCache, SyncState, TaskIndicators


def cache_key(project_name: str, task_id: str) -> str:
    return f"{project_name}/{task_id}"


def load_state() -> SyncState:
    f = paths.sync_state_file()
    if not f.exists():
        return SyncState()
    try:
        return SyncState.model_validate_json(f.read_text())
    except Exception:
        # A corrupt or older-schema state file must not wedge sync: the only
        # cost of starting over is one duplicate notification per event.
        return SyncState()


def save_state(value: SyncState) -> None:
    _write(paths.sync_state_file(), value.model_dump(mode="json", exclude_none=True))


def load_cache() -> IndicatorCache:
    f = paths.sync_cache_file()
    if not f.exists():
        return IndicatorCache()
    try:
        return IndicatorCache.model_validate_json(f.read_text())
    except Exception:
        return IndicatorCache()


def save_cache(value: IndicatorCache) -> None:
    _write(paths.sync_cache_file(), value.model_dump(mode="json", exclude_none=True))


def get_indicators(
    project_name: str, task_id: str, max_age_seconds: float
) -> TaskIndicators | None:
    """Cached indicators for a task, or None when absent or staler than allowed."""
    entry = load_cache().entries.get(cache_key(project_name, task_id))
    if entry is None:
        return None
    if entry.age_seconds() > max_age_seconds:
        return None
    return entry


def _write(target: Path, payload: dict[str, Any]) -> None:
    state.write_json_atomic(target, payload)
