"""The sync journal: append, read, and the live `follow` used by `gw sync watch`."""

from __future__ import annotations

import queue
import threading
import time
from collections.abc import Callable
from pathlib import Path

from goblin_watcher import paths
from goblin_watcher.sync import journal


def test_append_and_read_round_trip(isolated_xdg: Path) -> None:
    journal.append(pass_id="p1", level="info", event="pass-start")
    journal.append(pass_id="p1", level="action", event="task-pruned", project="demo", task="demo-1")

    entries = journal.read_entries()
    assert [e["event"] for e in entries] == ["pass-start", "task-pruned"]
    assert entries[1]["project"] == "demo"
    assert entries[1]["task"] == "demo-1"
    # Absent optional fields are omitted rather than written as null.
    assert "project" not in entries[0]


def test_read_entries_respects_limit(isolated_xdg: Path) -> None:
    for i in range(5):
        journal.append(pass_id="p", level="info", event=f"e{i}")
    assert [e["event"] for e in journal.read_entries(limit=2)] == ["e3", "e4"]


def test_read_entries_skips_unparseable_lines(isolated_xdg: Path) -> None:
    journal.append(pass_id="p", level="info", event="good")
    with paths.sync_journal_file().open("a") as f:
        f.write("{ this is not json\n\n")
    journal.append(pass_id="p", level="info", event="also-good")

    assert [e["event"] for e in journal.read_entries()] == ["good", "also-good"]


def test_read_entries_on_missing_file(isolated_xdg: Path) -> None:
    assert journal.read_entries() == []


def _collect(out: queue.Queue, stop: threading.Event) -> None:
    for entry in journal.follow(poll_seconds=0.01):
        out.put(entry)
        if stop.is_set():
            return


def _await_entry(
    received: queue.Queue,
    write: Callable[[], None],
    timeout: float = 5.0,
) -> dict:
    """Keep writing until the follower reports something, or give up.

    `follow()` seeks to EOF when it opens the file and exposes no readiness
    signal, so a single write racing the thread's first `open()` is silently
    missed — the source of a real flake. Retrying the write keeps the assertion
    (a *concurrent* writer's record reaches the follower) while removing the
    race; records written before `follow()` started still must not appear.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        write()
        try:
            return received.get(timeout=0.1)
        except queue.Empty:
            continue
    raise AssertionError("follow() never yielded the appended record")


def test_follow_yields_newly_appended_records(isolated_xdg: Path) -> None:
    """`gw sync watch` must surface events written by a separate sync process."""
    journal.append(pass_id="p0", level="info", event="before-follow")

    received: queue.Queue = queue.Queue()
    stop = threading.Event()
    watcher = threading.Thread(target=_collect, args=(received, stop), daemon=True)
    watcher.start()

    # follow() starts at EOF, so the pre-existing line must NOT arrive; only
    # what a concurrent writer appends after we started watching.
    entry = _await_entry(
        received,
        lambda: journal.append(pass_id="p1", level="notify", event="pr-merged", task="demo-1"),
    )
    stop.set()

    assert entry["event"] == "pr-merged"
    assert entry["task"] == "demo-1"


def test_follow_recovers_after_the_journal_is_truncated(isolated_xdg: Path) -> None:
    """Journal pruning replaces the file; follow must reopen rather than wedge."""
    journal.append(pass_id="p0", level="info", event="first")

    received: queue.Queue = queue.Queue()
    stop = threading.Event()
    watcher = threading.Thread(target=_collect, args=(received, stop), daemon=True)
    watcher.start()

    # Simulate a prune: rewrite the file from scratch.
    paths.sync_journal_file().write_text("")
    entry = _await_entry(
        received,
        lambda: journal.append(pass_id="p2", level="info", event="after-truncate"),
    )
    stop.set()

    assert entry["event"] == "after-truncate"


def _append_dated(event: str, days_ago: int) -> None:
    """Write a journal line with a backdated `ts` (append() always stamps now)."""
    import json
    from datetime import UTC, datetime, timedelta

    ts = (datetime.now(UTC) - timedelta(days=days_ago)).isoformat().replace("+00:00", "Z")
    path = paths.sync_journal_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as f:
        f.write(json.dumps({"ts": ts, "pass_id": "p", "level": "info", "event": event}) + "\n")


def test_prune_drops_only_records_older_than_the_cutoff(isolated_xdg: Path) -> None:
    """A pass every few minutes forever needs a way to trim, or the file is unbounded."""
    _append_dated("ancient", days_ago=40)
    _append_dated("recent", days_ago=1)

    assert journal.count_old(30) == (1, 1)
    assert journal.prune(30) == (1, 1)
    assert [e["event"] for e in journal.read_entries()] == ["recent"]


def test_prune_keeps_undatable_lines(isolated_xdg: Path) -> None:
    """A record we can't date is not one we should silently delete."""
    _append_dated("ancient", days_ago=40)
    with paths.sync_journal_file().open("a") as f:
        f.write('{"level": "info", "event": "no-ts"}\n')

    removed, kept = journal.prune(30)
    assert (removed, kept) == (1, 1)
    assert [e["event"] for e in journal.read_entries()] == ["no-ts"]


def test_prune_on_a_journal_with_nothing_old_rewrites_nothing(isolated_xdg: Path) -> None:
    journal.append(pass_id="p", level="info", event="fresh")
    before = paths.sync_journal_file().read_text()
    assert journal.prune(7) == (0, 1)
    assert paths.sync_journal_file().read_text() == before
