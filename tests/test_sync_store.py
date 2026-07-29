"""Sync state + indicator cache persistence."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from goblin_watcher import paths
from goblin_watcher.sync import store
from goblin_watcher.sync.models import (
    DescriptionBackoff,
    IndicatorCache,
    PassReport,
    SyncState,
    TaskIndicators,
)


def test_load_state_defaults_when_absent(isolated_xdg: Path) -> None:
    st = store.load_state()
    assert st.last_pass is None
    assert st.last_seen == {}


def test_state_round_trip(isolated_xdg: Path) -> None:
    st = SyncState(
        last_pass=PassReport(pass_id="abc123", tasks=3),
        last_seen={"demo/demo-1:pr-state": "MERGED"},
        description_backoff={"sess-1": DescriptionBackoff(failures=2)},
    )
    store.save_state(st)

    loaded = store.load_state()
    assert loaded.last_pass is not None
    assert loaded.last_pass.pass_id == "abc123"
    assert loaded.last_pass.tasks == 3
    assert loaded.last_seen["demo/demo-1:pr-state"] == "MERGED"
    assert loaded.description_backoff["sess-1"].failures == 2


def test_corrupt_state_falls_back_to_empty(isolated_xdg: Path) -> None:
    """A bad state file must not wedge sync — worst case is a duplicate notify."""
    f = paths.sync_state_file()
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text("{not json")
    assert store.load_state().last_seen == {}


def test_cache_round_trip_and_key(isolated_xdg: Path) -> None:
    cache = IndicatorCache()
    cache.entries[store.cache_key("demo", "demo-1")] = TaskIndicators(
        uncommitted=True, ahead=2, ahead_vs_remote=True, pr_state="OPEN", checks="failing"
    )
    store.save_cache(cache)

    got = store.get_indicators("demo", "demo-1", max_age_seconds=3600)
    assert got is not None
    assert got.uncommitted is True
    assert got.ahead == 2
    assert got.ahead_vs_remote is True
    assert got.pr_state == "OPEN"
    assert got.checks == "failing"


def test_get_indicators_returns_none_when_stale(isolated_xdg: Path) -> None:
    cache = IndicatorCache()
    cache.entries[store.cache_key("demo", "demo-1")] = TaskIndicators(
        computed_at=datetime.now(UTC) - timedelta(hours=2)
    )
    store.save_cache(cache)

    assert store.get_indicators("demo", "demo-1", max_age_seconds=60) is None
    assert store.get_indicators("demo", "demo-1", max_age_seconds=3 * 3600) is not None


def test_get_indicators_returns_none_when_absent(isolated_xdg: Path) -> None:
    assert store.get_indicators("demo", "nope", max_age_seconds=3600) is None
