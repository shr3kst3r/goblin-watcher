"""State-drift detection and `gw doctor --repair` (gh-29).

Every fixture here builds a real local git repo — the drift checks are about what
git and the filesystem actually say, so faking them would test nothing. No
remotes, no agent CLIs, no launchctl: the one launchd probe is monkeypatched.
"""

from __future__ import annotations

import shutil
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from typer.testing import CliRunner

from goblin_watcher import drift, paths, state
from goblin_watcher.cli import app
from goblin_watcher.commands import doctor as doctor_cmd
from goblin_watcher.models import Task
from goblin_watcher.sync import store
from goblin_watcher.sync.models import IndicatorCache, PassReport, SyncState, TaskIndicators


def _init_repo(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", "-b", "main", str(path)], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.name", "tester"], check=True)
    (path / "README.md").write_text("hi")
    subprocess.run(["git", "-C", str(path), "add", "."], check=True)
    subprocess.run(["git", "-C", str(path), "commit", "-qm", "init"], check=True)


def _bootstrap(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Task]:
    """One project with one task, and a Linear key so only drift can fail doctor."""
    monkeypatch.setenv("LINEAR_API_KEY", "lin_api_test")
    repo = tmp_path / "alpha"
    _init_repo(repo)
    runner = CliRunner()
    res = runner.invoke(app, ["project", "new", "alpha", "--dir", str(repo)])
    assert res.exit_code == 0, res.output
    res = runner.invoke(app, ["new", "--branch-name", "spike/foo", "--title", "Foo", "--no-launch"])
    assert res.exit_code == 0, res.output
    [task] = state.list_tasks(state.get_project("alpha"))
    return repo, task


def _flat(output: str) -> str:
    """Rich wraps cells; collapse whitespace before asserting on fragments."""
    return " ".join(output.split())


def _doctor(*args: str) -> tuple[int, str]:
    res = CliRunner().invoke(app, ["doctor", *args])
    return res.exit_code, _flat(res.output)


def test_clean_project_reports_no_drift(
    isolated_xdg: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _bootstrap(tmp_path, monkeypatch)
    code, out = _doctor()
    assert code == 0, out
    assert "no drift detected" in out


def test_freshly_registered_project_has_the_exclude_entries(
    isolated_xdg: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Couples `drift.LOCAL_EXCLUDE_PATTERNS` to what `gw project new` writes.

    If either side grows a pattern the other doesn't know about, this fails
    rather than doctor quietly reporting drift on every healthy project.
    """
    _bootstrap(tmp_path, monkeypatch)
    findings = drift.detect()
    assert [f for f in findings if f.kind == "missing-exclude"] == []


def test_orphan_worktree_is_reported_but_never_repaired(
    isolated_xdg: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, _ = _bootstrap(tmp_path, monkeypatch)
    stray = repo / ".worktrees" / "handmade"
    subprocess.run(
        ["git", "-C", str(repo), "worktree", "add", "-q", "-b", "handmade", str(stray)],
        check=True,
    )

    [finding] = [f for f in drift.detect() if f.kind == "orphan-worktree"]
    assert finding.where == str(stray)
    assert not finding.repairable

    code, out = _doctor()
    assert code != 0, out
    assert "untracked worktree" in out

    # --repair must leave it alone: the directory may hold uncommitted work.
    code, out = _doctor("--repair")
    assert code != 0, out
    assert stray.is_dir()
    assert "untracked worktree" in out


def test_worktree_outside_the_worktree_root_is_not_flagged(
    isolated_xdg: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A worktree the user made elsewhere is none of gw's business."""
    repo, _ = _bootstrap(tmp_path, monkeypatch)
    elsewhere = tmp_path / "somewhere-else"
    subprocess.run(
        ["git", "-C", str(repo), "worktree", "add", "-q", "-b", "mine", str(elsewhere)],
        check=True,
    )
    assert [f for f in drift.detect() if f.kind == "orphan-worktree"] == []


def test_missing_worktree_with_surviving_branch_keeps_the_record(
    isolated_xdg: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, task = _bootstrap(tmp_path, monkeypatch)
    shutil.rmtree(task.worktree_path)

    code, out = _doctor("--repair")
    assert code != 0, out
    assert "worktree gone" in out
    # The branch still holds the commits, so the record has to survive to point
    # at them.
    assert state.list_tasks(state.get_project("alpha"))


def test_missing_branch_under_a_live_worktree_is_report_only(
    isolated_xdg: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, task = _bootstrap(tmp_path, monkeypatch)
    # `git branch -D` refuses for a checked-out branch; update-ref is how a
    # branch actually disappears from under a worktree.
    subprocess.run(
        ["git", "-C", str(repo), "update-ref", "-d", f"refs/heads/{task.branch}"], check=True
    )

    code, out = _doctor("--repair")
    assert code != 0, out
    assert "branch gone" in out
    assert state.list_tasks(state.get_project("alpha"))


def test_repair_drops_record_when_worktree_and_branch_are_both_gone(
    isolated_xdg: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, task = _bootstrap(tmp_path, monkeypatch)
    shutil.rmtree(task.worktree_path)
    subprocess.run(["git", "-C", str(repo), "worktree", "prune"], check=True)
    subprocess.run(["git", "-C", str(repo), "branch", "-qD", task.branch], check=True)

    code, out = _doctor()
    assert code != 0, out
    assert "dead task record" in out

    code, out = _doctor("--repair")
    assert "dropped task record" in out
    assert state.list_tasks(state.get_project("alpha")) == []
    # Nothing left to report, so doctor goes green again.
    assert code == 0, out


def test_repair_reappends_missing_exclude_entries(
    isolated_xdg: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, _ = _bootstrap(tmp_path, monkeypatch)
    exclude = repo / ".git" / "info" / "exclude"
    exclude.write_text("# wiped\n")

    code, out = _doctor()
    assert code != 0, out
    assert "exclude entries" in out

    code, out = _doctor("--repair")
    assert code == 0, out
    lines = {line.strip() for line in exclude.read_text().splitlines()}
    assert drift.LOCAL_EXCLUDE_PATTERNS
    assert set(drift.LOCAL_EXCLUDE_PATTERNS) <= lines
    # Idempotent: repairing twice doesn't duplicate the patterns.
    _doctor("--repair")
    assert exclude.read_text().count(".goblin/") == 1


def test_repair_prunes_stale_indicator_cache_rows(
    isolated_xdg: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _bootstrap(tmp_path, monkeypatch)
    paths.sync_dir().mkdir(parents=True, exist_ok=True)
    store.save_cache(
        IndicatorCache(entries={store.cache_key("alpha", "ghost-1"): TaskIndicators()})
    )

    code, out = _doctor()
    assert code != 0, out
    assert "stale indicator cache" in out

    code, out = _doctor("--repair")
    assert code == 0, out
    assert store.load_cache().entries == {}


def test_indicator_rows_for_live_tasks_survive(
    isolated_xdg: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, task = _bootstrap(tmp_path, monkeypatch)
    key = store.cache_key("alpha", task.id)
    paths.sync_dir().mkdir(parents=True, exist_ok=True)
    store.save_cache(IndicatorCache(entries={key: TaskIndicators()}))

    code, out = _doctor("--repair")
    assert code == 0, out
    assert key in store.load_cache().entries


def test_scratch_record_with_missing_directory_is_repairable(
    isolated_xdg: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LINEAR_API_KEY", "lin_api_test")
    runner = CliRunner()
    res = runner.invoke(app, ["scratch", "notes", "--no-launch"])
    assert res.exit_code == 0, res.output
    proj = state.get_project("scratch")
    [task] = state.list_tasks(proj)
    shutil.rmtree(task.worktree_path)

    code, out = _doctor("--repair")
    assert code == 0, out
    assert "dead task record" in out
    assert state.list_tasks(proj) == []


def test_repair_reports_a_failed_fix_instead_of_crashing(
    isolated_xdg: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, task = _bootstrap(tmp_path, monkeypatch)
    shutil.rmtree(task.worktree_path)
    subprocess.run(["git", "-C", str(repo), "worktree", "prune"], check=True)
    subprocess.run(["git", "-C", str(repo), "branch", "-qD", task.branch], check=True)

    def boom(*_args: object, **_kwargs: object) -> None:
        raise OSError("disk on fire")

    monkeypatch.setattr(state, "delete_task_record", boom)
    code, out = _doctor("--repair")
    assert code != 0, out
    assert "disk on fire" in out


def test_findings_cannot_declare_an_unsafe_kind_repairable() -> None:
    with pytest.raises(ValueError):
        drift.Finding(
            kind="orphan-worktree",
            where="/tmp/wt",
            detail="nope",
            repairable=True,
        )


# ---------------------------------------------------------------------------
# launchd job installed but not firing


def _install_plist() -> Path:
    plist = Path.home() / "Library" / "LaunchAgents" / "com.goblin-watcher.sync.plist"
    plist.parent.mkdir(parents=True, exist_ok=True)
    plist.write_bytes(b"<plist/>")
    return plist


def _fake_launchd(
    monkeypatch: pytest.MonkeyPatch, *, loaded: bool, interval: int | None = 300
) -> None:
    """Stand in for the real job. `launchctl` is never invoked from a test."""
    from goblin_watcher.sync import launchd

    monkeypatch.setattr(launchd, "is_supported", lambda platform=None: True)
    monkeypatch.setattr(launchd, "is_loaded", lambda: loaded)
    monkeypatch.setattr(launchd, "installed_interval", lambda: interval)
    _install_plist()


def _record_pass(when: datetime) -> None:
    report = PassReport(pass_id="p1", started_at=when, finished_at=when)
    store.save_state(SyncState(last_pass=report))


def test_sync_check_fails_when_installed_but_not_loaded(
    isolated_xdg: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fake_launchd(monkeypatch, loaded=False)
    check = doctor_cmd._sync_check()
    assert not check.ok
    assert "launchd has not loaded it" in check.detail

    # And it takes doctor's exit code with it.
    monkeypatch.setenv("LINEAR_API_KEY", "lin_api_test")
    code, _ = _doctor()
    assert code != 0


def test_sync_check_fails_when_job_is_loaded_but_not_firing(
    isolated_xdg: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fake_launchd(monkeypatch, loaded=True)
    _record_pass(datetime.now(UTC) - timedelta(hours=6))
    check = doctor_cmd._sync_check()
    assert not check.ok
    assert "the last pass finished" in check.detail
    assert "300s interval" in check.detail


def test_sync_check_green_for_a_recent_pass(
    isolated_xdg: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fake_launchd(monkeypatch, loaded=True)
    _record_pass(datetime.now(UTC))
    check = doctor_cmd._sync_check()
    assert check.ok, check.detail
    assert "last pass ok" in check.detail


def test_sync_check_tolerates_a_fresh_install_with_no_pass_yet(
    isolated_xdg: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # RunAtLoad is off, so a just-installed job legitimately hasn't run yet: the
    # plist's mtime is what keeps this from reading as "never fired".
    _fake_launchd(monkeypatch, loaded=True)
    check = doctor_cmd._sync_check()
    assert check.ok, check.detail
    assert "never run" in check.detail


def test_sync_check_fails_when_the_interval_is_unreadable(
    isolated_xdg: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fake_launchd(monkeypatch, loaded=True, interval=None)
    check = doctor_cmd._sync_check()
    assert not check.ok
    assert "StartInterval" in check.detail
