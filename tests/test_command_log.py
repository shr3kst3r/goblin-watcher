from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from goblin_watcher import command_log


def _read_log() -> list[dict]:
    text = command_log.log_file().read_text()
    return [json.loads(line) for line in text.splitlines() if line.strip()]


def test_record_invocation_writes_jsonl_entry(isolated_xdg: Path) -> None:
    with command_log.record_invocation(["task", "ls"]) as entry:
        entry["exit_code"] = 0

    entries = _read_log()
    assert len(entries) == 1
    e = entries[0]
    assert e["argv"] == ["task", "ls"]
    assert e["exit_code"] == 0
    assert e["cwd"]
    assert e["ts"].endswith("Z")
    assert "version" in e
    assert isinstance(e["duration_ms"], int)


def test_record_invocation_captures_nonzero_exit(isolated_xdg: Path) -> None:
    with command_log.record_invocation(["doctor"]) as entry:
        entry["exit_code"] = 2

    entries = _read_log()
    assert entries[-1]["exit_code"] == 2


def test_record_invocation_records_even_on_exception(isolated_xdg: Path) -> None:
    with (
        pytest.raises(RuntimeError),
        command_log.record_invocation(["status"]) as entry,
    ):
        entry["exit_code"] = 99
        raise RuntimeError("boom")

    entries = _read_log()
    assert entries[-1]["argv"] == ["status"]
    assert entries[-1]["exit_code"] == 99


def test_record_invocation_skipped_when_subprocess_env_set(
    isolated_xdg: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GW_DESCRIBE_SUBPROCESS", "1")
    with command_log.record_invocation(["whatever"]) as entry:
        entry["exit_code"] = 0
    assert not command_log.log_file().exists()


def test_read_entries_skips_malformed_lines(isolated_xdg: Path) -> None:
    log = command_log.log_file()
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text(
        '{"ts":"2026-05-21T10:00:00.000000Z","argv":["a"],"exit_code":0}\n'
        "not-json-at-all\n"
        '{"ts":"2026-05-21T11:00:00.000000Z","argv":["b"],"exit_code":1}\n'
    )
    entries = command_log.read_entries()
    assert [e["argv"] for e in entries] == [["a"], ["b"]]


def _make_log(lines: list[dict]) -> None:
    log = command_log.log_file()
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text("\n".join(json.dumps(e) for e in lines) + "\n")


def test_prune_drops_entries_older_than_cutoff(isolated_xdg: Path) -> None:
    now = datetime.now(UTC)
    old_ts = (now - timedelta(days=10)).isoformat().replace("+00:00", "Z")
    recent_ts = (now - timedelta(days=1)).isoformat().replace("+00:00", "Z")
    _make_log(
        [
            {"ts": old_ts, "argv": ["old1"], "exit_code": 0},
            {"ts": recent_ts, "argv": ["recent"], "exit_code": 0},
            {"ts": old_ts, "argv": ["old2"], "exit_code": 0},
        ]
    )

    removed, kept = command_log.prune(older_than_days=5)
    assert (removed, kept) == (2, 1)

    remaining = command_log.read_entries()
    assert [e["argv"] for e in remaining] == [["recent"]]


def test_prune_keeps_entries_without_parseable_ts(isolated_xdg: Path) -> None:
    log = command_log.log_file()
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text('{"argv":["no-ts"],"exit_code":0}\n')

    removed, kept = command_log.prune(older_than_days=1)
    assert removed == 0
    assert kept == 1


def test_prune_on_missing_file_is_noop(isolated_xdg: Path) -> None:
    assert command_log.prune(older_than_days=30) == (0, 0)


def test_prune_rejects_negative_days(isolated_xdg: Path) -> None:
    with pytest.raises(ValueError):
        command_log.prune(older_than_days=-1)


def test_count_old_does_not_modify_file(isolated_xdg: Path) -> None:
    now = datetime.now(UTC)
    old_ts = (now - timedelta(days=10)).isoformat().replace("+00:00", "Z")
    _make_log([{"ts": old_ts, "argv": ["old"], "exit_code": 0}])

    before = command_log.log_file().read_text()
    removed, kept = command_log.count_old(older_than_days=5)
    after = command_log.log_file().read_text()

    assert (removed, kept) == (1, 0)
    assert before == after
