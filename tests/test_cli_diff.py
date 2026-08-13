"""`gw diff` and `gw status --diffstat` (gh-30).

Real git repos in tmp_path, as in tests/test_cli_task_archive.py — nothing here
needs a remote, an agent, or `gh`.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from goblin_watcher import state
from goblin_watcher.cli import app
from goblin_watcher.commands import diff as diff_cmd
from goblin_watcher.errors import GoblinError
from goblin_watcher.models import LinearIssue, Task


def _init_repo(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", "-b", "main", str(path)], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.name", "tester"], check=True)
    (path / "README.md").write_text("hi\n")
    subprocess.run(["git", "-C", str(path), "add", "."], check=True)
    subprocess.run(["git", "-C", str(path), "commit", "-qm", "init"], check=True)


def _commit(path: Path, name: str, body: str, message: str) -> None:
    (path / name).write_text(body)
    subprocess.run(["git", "-C", str(path), "add", name], check=True)
    subprocess.run(["git", "-C", str(path), "commit", "-qm", message], check=True)


def _bootstrap(tmp_path: Path) -> tuple[Path, Task]:
    """One project `alpha`, one task on `spike/foo` with a materialized worktree."""
    repo = tmp_path / "alpha"
    _init_repo(repo)
    runner = CliRunner()
    runner.invoke(app, ["project", "new", "alpha", "--dir", str(repo)])
    res = runner.invoke(
        app, ["new", "--branch-name", "spike/foo", "--title", "Do the thing", "--no-launch"]
    )
    assert res.exit_code == 0, res.output
    return repo, _task()


def _task() -> Task:
    [task] = state.list_tasks(state.get_project("alpha"))
    return task


# ---------- diffstat parsing ----------------------------------------------------


def test_parse_totals_reads_gits_summary_line() -> None:
    totals = diff_cmd.parse_totals(
        " a.py | 2 +-\n b.py | 8 ++++++--\n 2 files changed, 7 insertions(+), 3 deletions(-)"
    )
    assert totals is not None
    assert totals == diff_cmd.Totals(files=2, insertions=7, deletions=3)
    assert totals.one_line == "+7 -3 · 2 files"


def test_parse_totals_handles_a_one_sided_diff() -> None:
    """A pure addition has no deletions clause at all, and vice versa."""
    added = diff_cmd.parse_totals(" a.py | 2 ++\n 1 file changed, 2 insertions(+)")
    assert added is not None
    assert added == diff_cmd.Totals(files=1, insertions=2, deletions=0)
    assert added.one_line == "+2 -0 · 1 file"

    removed = diff_cmd.parse_totals(" a.py | 2 --\n 1 file changed, 2 deletions(-)")
    assert removed == diff_cmd.Totals(files=1, insertions=0, deletions=2)


def test_parse_totals_returns_none_when_there_is_nothing_to_read() -> None:
    """None, not zeroes: git prints nothing for an empty diff, and rendering
    `+0 -0 · 0 files` would read like a real result."""
    assert diff_cmd.parse_totals("") is None
    assert diff_cmd.parse_totals("   \n\n") is None
    assert diff_cmd.parse_totals("fatal: bad revision") is None


def test_totals_add_across_repos() -> None:
    a = diff_cmd.Totals(files=1, insertions=2, deletions=3)
    b = diff_cmd.Totals(files=4, insertions=5, deletions=6)
    assert a + b == diff_cmd.Totals(files=5, insertions=7, deletions=9)


# ---------- gw diff -------------------------------------------------------------


def test_diff_shows_commits_stat_and_patch(isolated_xdg: Path, tmp_path: Path) -> None:
    _repo, task = _bootstrap(tmp_path)
    _commit(task.worktree_path, "feature.py", "line1\nline2\n", "Add the feature")

    res = CliRunner().invoke(app, ["diff", task.id])
    assert res.exit_code == 0, res.output
    # Heading: task id, branch vs base, commit tally, totals.
    assert "spike-foo" in res.output
    assert "spike/foo vs main" in res.output
    assert "1 commit" in res.output
    assert "+2 -0 · 1 file" in res.output
    # Commit subject, diffstat, and the patch itself.
    assert "Add the feature" in res.output
    assert "feature.py | 2 ++" in res.output
    assert "diff --git a/feature.py b/feature.py" in res.output
    assert "+line1" in res.output


def test_diff_stat_omits_the_patch(isolated_xdg: Path, tmp_path: Path) -> None:
    _repo, task = _bootstrap(tmp_path)
    _commit(task.worktree_path, "feature.py", "line1\n", "Add the feature")

    res = CliRunner().invoke(app, ["diff", task.id, "--stat"])
    assert res.exit_code == 0, res.output
    assert "feature.py | 1 +" in res.output
    assert "diff --git" not in res.output
    assert "+line1" not in res.output


def test_diff_heading_carries_the_tracking_ticket(isolated_xdg: Path, tmp_path: Path) -> None:
    """A `--branch-name` task has no upstream, so the title only appears once one does."""
    _repo, task = _bootstrap(tmp_path)
    proj = state.get_project("alpha")
    state.save_task(
        proj,
        task.model_copy(
            update={
                "linear": LinearIssue(
                    id="uuid",
                    identifier="PLAT-7",
                    title="Do the thing",
                    state="In Progress",
                    team_key="PLAT",
                    url="https://linear.app/x/issue/PLAT-7",
                )
            }
        ),
    )

    res = CliRunner().invoke(app, ["diff", task.id, "--stat"])
    assert res.exit_code == 0, res.output
    assert "Do the thing" in res.output


def test_diff_defaults_to_the_cwds_task(
    isolated_xdg: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _repo, task = _bootstrap(tmp_path)
    _commit(task.worktree_path, "feature.py", "line1\n", "Add the feature")

    monkeypatch.chdir(task.worktree_path)
    res = CliRunner().invoke(app, ["diff"])
    assert res.exit_code == 0, res.output
    assert "spike/foo vs main" in res.output


def test_diff_shows_uncommitted_work_and_untracked_files(
    isolated_xdg: Path, tmp_path: Path
) -> None:
    """The case that matters for a live agent: it hasn't committed anything yet."""
    _repo, task = _bootstrap(tmp_path)
    (task.worktree_path / "README.md").write_text("hi\nmid-flight\n")
    (task.worktree_path / "brand-new.txt").write_text("untracked\n")

    res = CliRunner().invoke(app, ["diff", task.id])
    assert res.exit_code == 0, res.output
    assert "Uncommitted" in res.output
    assert "+mid-flight" in res.output
    # `git diff` can't see an untracked file, so it's named separately.
    assert "brand-new.txt" in res.output
    assert "No committed changes on this branch." in res.output


def test_diff_no_uncommitted_hides_the_worktree_overlay(isolated_xdg: Path, tmp_path: Path) -> None:
    _repo, task = _bootstrap(tmp_path)
    _commit(task.worktree_path, "feature.py", "line1\n", "Add the feature")
    (task.worktree_path / "README.md").write_text("hi\nmid-flight\n")

    res = CliRunner().invoke(app, ["diff", task.id, "--no-uncommitted"])
    assert res.exit_code == 0, res.output
    assert "Uncommitted" not in res.output
    assert "mid-flight" not in res.output
    assert "diff --git a/feature.py b/feature.py" in res.output


def test_diff_uses_the_merge_base_so_an_advancing_main_is_not_a_reversion(
    isolated_xdg: Path, tmp_path: Path
) -> None:
    """Two-dot would report everything main gained since the branch was cut as a
    deletion. The task's own diff has to be immune to that."""
    repo, task = _bootstrap(tmp_path)
    _commit(task.worktree_path, "feature.py", "line1\n", "Add the feature")
    _commit(repo, "main-only.py", "landed elsewhere\n", "Something else landed on main")

    res = CliRunner().invoke(app, ["diff", task.id])
    assert res.exit_code == 0, res.output
    assert "feature.py" in res.output
    assert "main-only.py" not in res.output
    assert "1 file changed" in res.output


def test_diff_base_option_overrides_the_recorded_base(isolated_xdg: Path, tmp_path: Path) -> None:
    _repo, task = _bootstrap(tmp_path)
    _commit(task.worktree_path, "first.py", "a\n", "First")
    subprocess.run(
        ["git", "-C", str(task.worktree_path), "tag", "checkpoint"],
        check=True,
    )
    _commit(task.worktree_path, "second.py", "b\n", "Second")

    res = CliRunner().invoke(app, ["diff", task.id, "--stat", "--base", "checkpoint"])
    assert res.exit_code == 0, res.output
    assert "second.py" in res.output
    assert "first.py" not in res.output
    assert "spike/foo vs checkpoint" in res.output


def test_diff_on_an_archived_task_still_shows_the_branch(
    isolated_xdg: Path, tmp_path: Path
) -> None:
    """The branch outlives the worktree (gh-23), so the committed diff survives
    archiving — only the uncommitted overlay is unavailable."""
    _repo, task = _bootstrap(tmp_path)
    _commit(task.worktree_path, "feature.py", "line1\n", "Add the feature")

    runner = CliRunner()
    assert runner.invoke(app, ["task", "archive", task.id]).exit_code == 0
    assert not _task().worktree_path.exists()

    res = runner.invoke(app, ["diff", task.id])
    assert res.exit_code == 0, res.output
    assert "Archived, so there's no worktree" in res.output
    # `gw run <id>` is the fix, but Rich wraps the note, so match its tail only.
    assert "rematerializes it" in res.output
    assert "diff --git a/feature.py b/feature.py" in res.output
    assert "Uncommitted" not in res.output


def test_diff_reports_a_missing_worktree_that_was_never_archived(
    isolated_xdg: Path, tmp_path: Path
) -> None:
    """The note is keyed on the directory, not on `Task.archived` — a checkout
    deleted behind gw's back leaves the flag unset and the tree just as gone."""
    _repo, task = _bootstrap(tmp_path)
    _commit(task.worktree_path, "feature.py", "line1\n", "Add the feature")
    shutil.rmtree(task.worktree_path)

    res = CliRunner().invoke(app, ["diff", task.id, "--stat"])
    assert res.exit_code == 0, res.output
    assert "No worktree at" in res.output
    assert "feature.py" in res.output


def test_diff_reports_a_deleted_branch_instead_of_crashing(
    isolated_xdg: Path, tmp_path: Path
) -> None:
    repo, task = _bootstrap(tmp_path)
    _commit(task.worktree_path, "feature.py", "line1\n", "Add the feature")
    runner = CliRunner()
    assert runner.invoke(app, ["task", "archive", task.id]).exit_code == 0
    subprocess.run(["git", "-C", str(repo), "branch", "-D", "spike/foo"], check=True)

    res = runner.invoke(app, ["diff", task.id])
    assert res.exit_code == 0, res.output
    assert "no longer exists" in res.output
    assert "Nothing to show" in res.output


def test_diff_reports_an_unresolvable_base_ref(isolated_xdg: Path, tmp_path: Path) -> None:
    _repo, task = _bootstrap(tmp_path)
    _commit(task.worktree_path, "feature.py", "line1\n", "Add the feature")

    res = CliRunner().invoke(app, ["diff", task.id, "--base", "gone-with-the-parent"])
    assert res.exit_code == 0, res.output
    assert "can't be resolved" in res.output


def test_diff_with_nothing_at_all_says_so(isolated_xdg: Path, tmp_path: Path) -> None:
    _repo, task = _bootstrap(tmp_path)
    res = CliRunner().invoke(app, ["diff", task.id])
    assert res.exit_code == 0, res.output
    assert "Nothing to show" in res.output


def test_diff_rejects_a_scratch_task(isolated_xdg: Path) -> None:
    runner = CliRunner()
    assert runner.invoke(app, ["scratch", "pad", "--no-launch"]).exit_code == 0
    res = runner.invoke(app, ["diff", "pad"])
    assert res.exit_code != 0
    assert isinstance(res.exception, GoblinError)
    assert "scratch space" in res.exception.message


def test_diff_rejects_an_unknown_repo_filter(isolated_xdg: Path, tmp_path: Path) -> None:
    _repo, task = _bootstrap(tmp_path)
    res = CliRunner().invoke(app, ["diff", task.id, "--repo", "nope"])
    assert res.exit_code != 0
    assert isinstance(res.exception, GoblinError)
    assert "no repo for project" in res.exception.message


def test_diff_repo_filter_accepts_the_tasks_own_project(isolated_xdg: Path, tmp_path: Path) -> None:
    _repo, task = _bootstrap(tmp_path)
    _commit(task.worktree_path, "feature.py", "line1\n", "Add the feature")
    res = CliRunner().invoke(app, ["diff", task.id, "--stat", "--repo", "alpha"])
    assert res.exit_code == 0, res.output
    assert "feature.py" in res.output


# ---------- gw status --diffstat ------------------------------------------------


def test_status_diffstat_annotates_the_task_line(isolated_xdg: Path, tmp_path: Path) -> None:
    _repo, task = _bootstrap(tmp_path)
    _commit(task.worktree_path, "feature.py", "line1\nline2\n", "Add the feature")

    res = CliRunner().invoke(app, ["status", "--diffstat", "--no-linear"])
    assert res.exit_code == 0, res.output
    assert "+2 -0 · 1 file" in res.output


def test_status_omits_totals_without_the_flag(isolated_xdg: Path, tmp_path: Path) -> None:
    _repo, task = _bootstrap(tmp_path)
    _commit(task.worktree_path, "feature.py", "line1\n", "Add the feature")

    res = CliRunner().invoke(app, ["status", "--no-linear"])
    assert res.exit_code == 0, res.output
    assert "1 file" not in res.output


def test_status_diffstat_still_reports_an_archived_task(isolated_xdg: Path, tmp_path: Path) -> None:
    """No worktree, but the branch is still there to diff."""
    _repo, task = _bootstrap(tmp_path)
    _commit(task.worktree_path, "feature.py", "line1\n", "Add the feature")
    runner = CliRunner()
    assert runner.invoke(app, ["task", "archive", task.id]).exit_code == 0

    res = runner.invoke(app, ["status", "--diffstat", "--no-linear"])
    assert res.exit_code == 0, res.output
    assert "+1 -0 · 1 file" in res.output


def test_status_diffstat_skips_scratch_tasks(isolated_xdg: Path) -> None:
    runner = CliRunner()
    assert runner.invoke(app, ["scratch", "pad", "--no-launch"]).exit_code == 0
    proj = state.get_project("scratch")
    [task] = state.list_tasks(proj)

    assert diff_cmd.status_suffix(proj, task) == ""
    res = runner.invoke(app, ["status", "--diffstat", "--no-linear"])
    assert res.exit_code == 0, res.output


def test_status_diffstat_survives_an_unregistered_secondary_project(
    isolated_xdg: Path, tmp_path: Path
) -> None:
    """A secondary repo whose project record is gone must not take the row down."""
    _repo, task = _bootstrap(tmp_path)
    proj = state.get_project("alpha")
    _commit(task.worktree_path, "feature.py", "line1\n", "Add the feature")
    orphan = task.primary_repo().model_copy(update={"project": "vanished"})
    state.save_task(proj, task.model_copy(update={"secondary_repos": [orphan]}))

    suffix = diff_cmd.status_suffix(proj, _task())
    assert "+1 -0 · 1 file" in suffix
