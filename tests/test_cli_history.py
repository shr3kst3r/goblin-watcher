from __future__ import annotations

import json
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path


def _run_gw(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "goblin_watcher", *args],
        capture_output=True,
        text=True,
        check=False,
    )


def _log_path(xdg_data: Path) -> Path:
    return xdg_data / "goblin-watcher" / "logs" / "commands.jsonl"


def _write_log(xdg_data: Path, entries: list[dict]) -> None:
    log = _log_path(xdg_data)
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text("\n".join(json.dumps(e) for e in entries) + "\n")


def test_history_empty_says_so(isolated_xdg: Path) -> None:
    # Need to remove the entry that `gw history` would create itself by using
    # the in-process API instead of a subprocess call.
    from goblin_watcher import command_log

    assert command_log.read_entries() == []


def test_history_shows_recent_entries(isolated_xdg: Path) -> None:
    data = isolated_xdg / "data"
    _write_log(
        data,
        [
            {
                "ts": "2026-05-20T10:00:00.000000Z",
                "argv": ["task", "ls"],
                "cwd": "/x",
                "exit_code": 0,
                "duration_ms": 50,
                "version": "0.1",
            },
            {
                "ts": "2026-05-21T11:00:00.000000Z",
                "argv": ["doctor"],
                "cwd": "/x",
                "exit_code": 2,
                "duration_ms": 12,
                "version": "0.1",
            },
        ],
    )

    res = _run_gw("history", "--json")
    assert res.returncode == 0, res.stderr
    # The subprocess invocation itself appends a row too; ignore it.
    lines = [line for line in res.stdout.strip().splitlines() if line.strip()]
    parsed = [json.loads(line) for line in lines]
    argvs = [e["argv"] for e in parsed]
    assert ["task", "ls"] in argvs
    assert ["doctor"] in argvs


def test_history_tail_limits_output(isolated_xdg: Path) -> None:
    data = isolated_xdg / "data"
    entries = [
        {
            "ts": f"2026-05-{i + 1:02d}T10:00:00.000000Z",
            "argv": [f"cmd{i}"],
            "cwd": "/x",
            "exit_code": 0,
            "duration_ms": 1,
            "version": "0.1",
        }
        for i in range(10)
    ]
    _write_log(data, entries)

    res = _run_gw("history", "--tail", "2", "--json")
    assert res.returncode == 0, res.stderr
    parsed = [json.loads(line) for line in res.stdout.strip().splitlines() if line.strip()]
    # The `history` invocation logs itself in its finally block, after the read,
    # so only the two newest pre-seeded entries appear in the output.
    assert [e["argv"] for e in parsed] == [["cmd8"], ["cmd9"]]


def test_history_prune_drops_old_entries(isolated_xdg: Path) -> None:
    data = isolated_xdg / "data"
    now = datetime.now(UTC)
    old = (now - timedelta(days=10)).isoformat().replace("+00:00", "Z")
    recent = (now - timedelta(days=1)).isoformat().replace("+00:00", "Z")
    _write_log(
        data,
        [
            {"ts": old, "argv": ["old"], "exit_code": 0},
            {"ts": recent, "argv": ["recent"], "exit_code": 0},
        ],
    )

    res = _run_gw("history", "prune", "--days", "5")
    assert res.returncode == 0, res.stderr
    assert "Removed 1 entries" in res.stdout or "Removed 1 entries" in res.stderr

    remaining = [
        json.loads(line) for line in _log_path(data).read_text().splitlines() if line.strip()
    ]
    # `recent` survives plus the prune command's own log entry.
    argvs = [e["argv"] for e in remaining]
    assert ["recent"] in argvs
    assert ["old"] not in argvs


def test_history_prune_dry_run_does_not_modify(isolated_xdg: Path) -> None:
    data = isolated_xdg / "data"
    now = datetime.now(UTC)
    old = (now - timedelta(days=10)).isoformat().replace("+00:00", "Z")
    _write_log(data, [{"ts": old, "argv": ["old"], "exit_code": 0}])
    before = _log_path(data).read_text()

    res = _run_gw("history", "prune", "--days", "5", "--dry-run")
    assert res.returncode == 0, res.stderr
    assert "Would remove 1" in res.stdout

    # Dry-run still logs its own invocation; check the old entry is still there.
    after_entries = [
        json.loads(line) for line in _log_path(data).read_text().splitlines() if line.strip()
    ]
    assert any(e["argv"] == ["old"] for e in after_entries)
    # Sanity: file actually changed (dry-run command itself was logged).
    assert before != _log_path(data).read_text()


def test_history_prune_rejects_negative(isolated_xdg: Path) -> None:
    res = _run_gw("history", "prune", "--days", "-1")
    assert res.returncode != 0


def test_history_tail_zero_shows_nothing(isolated_xdg: Path) -> None:
    data = isolated_xdg / "data"
    _write_log(
        data,
        [
            {
                "ts": "2026-05-20T10:00:00.000000Z",
                "argv": ["task", "ls"],
                "cwd": "/x",
                "exit_code": 0,
                "duration_ms": 50,
                "version": "0.1",
            }
        ],
    )
    res = _run_gw("history", "--tail", "0")
    assert res.returncode == 0, res.stderr
    assert "task ls" not in res.stdout
    assert "No entries selected" in res.stdout
