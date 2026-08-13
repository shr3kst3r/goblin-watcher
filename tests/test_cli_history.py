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


def _bootstrap_usage(tmp_path: Path, buckets: list) -> None:
    """One project, one task, one session carrying `buckets`."""
    from datetime import UTC, datetime

    from typer.testing import CliRunner

    from goblin_watcher import state
    from goblin_watcher.cli import app
    from goblin_watcher.models import SessionRecord

    repo = tmp_path / "alpha"
    repo.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "tester"], check=True)
    (repo / "README.md").write_text("hi")
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "init"], check=True)
    runner = CliRunner()
    runner.invoke(app, ["project", "new", "alpha", "--dir", str(repo)])
    runner.invoke(app, ["new", "--branch-name", "spike/foo", "--no-launch"])
    proj = state.get_project("alpha")
    [task] = state.list_tasks(proj)
    now = datetime.now(UTC)
    record = SessionRecord(
        agent="claude",
        session_id="s1",
        created_at=now,
        last_used_at=now,
        summary="working",
        summary_updated_at=now,
        usage=buckets,
    )
    state.save_task(proj, task.model_copy(update={"sessions": [record]}))


def test_history_cost_rolls_up_by_day(isolated_xdg: Path, tmp_path: Path) -> None:
    from typer.testing import CliRunner

    from goblin_watcher.cli import app
    from goblin_watcher.models import UsageBucket

    today = datetime.now(UTC).astimezone().date()
    yesterday = today - timedelta(days=1)
    _bootstrap_usage(
        tmp_path,
        [
            UsageBucket(model="claude-opus-5", day=today, output_tokens=1_000_000),
            UsageBucket(model="claude-opus-5", day=yesterday, output_tokens=2_000_000),
        ],
    )
    res = CliRunner(env={"COLUMNS": "400"}).invoke(app, ["history", "--cost"])
    assert res.exit_code == 0, res.output
    assert today.isoformat() in res.output
    assert yesterday.isoformat() in res.output
    # $25/M output: 25 yesterday, 50 today, 75 total.
    assert "~$25.00" in res.output and "~$50.00" in res.output and "~$75.00" in res.output


def test_history_cost_window_excludes_older_days(isolated_xdg: Path, tmp_path: Path) -> None:
    from typer.testing import CliRunner

    from goblin_watcher.cli import app
    from goblin_watcher.models import UsageBucket

    today = datetime.now(UTC).astimezone().date()
    old = today - timedelta(days=40)
    _bootstrap_usage(
        tmp_path,
        [
            UsageBucket(model="claude-opus-5", day=today, output_tokens=1_000_000),
            UsageBucket(model="claude-opus-5", day=old, output_tokens=4_000_000),
        ],
    )
    runner = CliRunner(env={"COLUMNS": "400"})
    windowed = runner.invoke(app, ["history", "--cost"])
    assert windowed.exit_code == 0, windowed.output
    assert old.isoformat() not in windowed.output
    assert "~$25.00" in windowed.output

    everything = runner.invoke(app, ["history", "--cost", "--days", "0"])
    assert everything.exit_code == 0, everything.output
    assert old.isoformat() in everything.output
    assert "~$125.00" in everything.output


def test_history_cost_reports_nothing_recorded(isolated_xdg: Path) -> None:
    from typer.testing import CliRunner

    from goblin_watcher.cli import app

    res = CliRunner().invoke(app, ["history", "--cost"])
    assert res.exit_code == 0, res.output
    assert "No token usage recorded" in res.output
