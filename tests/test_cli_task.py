import subprocess
from pathlib import Path

from typer.testing import CliRunner

from goblin_watcher import git, state
from goblin_watcher.cli import app


def _init_repo(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", "-b", "main", str(path)], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.name", "tester"], check=True)
    (path / "README.md").write_text("hi")
    subprocess.run(["git", "-C", str(path), "add", "."], check=True)
    subprocess.run(["git", "-C", str(path), "commit", "-qm", "init"], check=True)


def _bootstrap(tmp_path: Path) -> Path:
    repo = tmp_path / "alpha"
    _init_repo(repo)
    runner = CliRunner()
    runner.invoke(app, ["project", "new", "alpha", "--dir", str(repo)])
    runner.invoke(app, ["new", "--branch-name", "spike/foo", "--title", "Foo", "--no-launch"])
    return repo


def test_task_ls_shows_created(isolated_xdg: Path, tmp_path: Path) -> None:
    _bootstrap(tmp_path)
    runner = CliRunner()
    res = runner.invoke(app, ["task", "ls"])
    assert res.exit_code == 0, res.output
    assert "spike/foo" in res.output


def test_task_show_prints_details(isolated_xdg: Path, tmp_path: Path) -> None:
    _bootstrap(tmp_path)
    proj = state.get_project("alpha")
    [task] = state.list_tasks(proj)
    runner = CliRunner()
    res = runner.invoke(app, ["task", "show", task.id])
    assert res.exit_code == 0, res.output
    assert task.id in res.output
    assert task.branch in res.output


def _bootstrap_two_projects(tmp_path: Path) -> tuple[Path, Path]:
    """Two projects, alpha and beta, each with an identically-named task."""
    repo_a = tmp_path / "alpha"
    repo_b = tmp_path / "beta"
    _init_repo(repo_a)
    _init_repo(repo_b)
    runner = CliRunner()
    runner.invoke(app, ["project", "new", "alpha", "--dir", str(repo_a)])
    runner.invoke(app, ["project", "new", "beta", "--dir", str(repo_b)])
    # Same branch name -> same task id in both projects.
    runner.invoke(app, ["new", "--project", "alpha", "--branch-name", "spike/foo", "--no-launch"])
    runner.invoke(app, ["new", "--project", "beta", "--branch-name", "spike/foo", "--no-launch"])
    return repo_a, repo_b


def test_task_show_project_scopes_lookup_to_one_project(isolated_xdg: Path, tmp_path: Path) -> None:
    """--project picks which project's identically-named task to show."""
    _bootstrap_two_projects(tmp_path)
    proj_b = state.get_project("beta")
    [task_b] = state.list_tasks(proj_b)
    runner = CliRunner()
    res = runner.invoke(app, ["task", "show", task_b.id, "--project", "beta"])
    assert res.exit_code == 0, res.output
    assert "project: beta" in res.output


def test_task_rm_force_removes_worktree_and_branch(isolated_xdg: Path, tmp_path: Path) -> None:
    repo = _bootstrap(tmp_path)
    proj = state.get_project("alpha")
    [task] = state.list_tasks(proj)
    worktree = task.worktree_path
    runner = CliRunner()
    res = runner.invoke(app, ["task", "rm", task.id, "--force"])
    assert res.exit_code == 0, res.output
    assert not worktree.exists()
    assert not git.branch_exists(repo, task.branch)
    assert state.list_tasks(proj) == []


def test_task_rm_project_scopes_lookup_to_one_project(isolated_xdg: Path, tmp_path: Path) -> None:
    """A task id present in two projects is disambiguated by --project."""
    _bootstrap_two_projects(tmp_path)
    proj_a = state.get_project("alpha")
    proj_b = state.get_project("beta")
    [task_b] = state.list_tasks(proj_b)
    runner = CliRunner()

    res = runner.invoke(app, ["task", "rm", task_b.id, "--project", "beta", "--force"])
    assert res.exit_code == 0, res.output
    # Beta's task is gone; alpha's identically-named task is untouched.
    assert state.list_tasks(proj_b) == []
    assert len(state.list_tasks(proj_a)) == 1


def test_task_rm_refuses_when_dirty_without_force(isolated_xdg: Path, tmp_path: Path) -> None:
    from goblin_watcher.errors import GoblinError

    _bootstrap(tmp_path)
    proj = state.get_project("alpha")
    [task] = state.list_tasks(proj)
    # Make the worktree dirty.
    (task.worktree_path / "README.md").write_text("local change")
    runner = CliRunner()
    res = runner.invoke(app, ["task", "rm", task.id], input="y\n")
    assert res.exit_code != 0
    assert isinstance(res.exception, GoblinError)
    assert "uncommitted" in res.exception.message.lower()


def test_task_rm_refuses_when_untracked_file_present(isolated_xdg: Path, tmp_path: Path) -> None:
    """Untracked files used to slip past the dirty check (status -uno) and then
    get nuked by the rmtree fallback. Both gates should now stop the removal."""
    from goblin_watcher.errors import GoblinError

    _bootstrap(tmp_path)
    proj = state.get_project("alpha")
    [task] = state.list_tasks(proj)
    untracked = task.worktree_path / "scratch.txt"
    untracked.write_text("precious")
    runner = CliRunner()
    res = runner.invoke(app, ["task", "rm", task.id], input="y\n")
    assert res.exit_code != 0
    assert isinstance(res.exception, GoblinError)
    assert task.worktree_path.exists()
    assert untracked.exists()
    # Task record is still around.
    assert len(state.list_tasks(proj)) == 1


def _commit_on_branch(worktree: Path, message: str = "branch work") -> None:
    (worktree / "scratch.txt").write_text("branch change")
    subprocess.run(["git", "-C", str(worktree), "add", "."], check=True)
    subprocess.run(["git", "-C", str(worktree), "commit", "-qm", message], check=True)


def test_task_prune_removes_merged_branch(isolated_xdg: Path, tmp_path: Path) -> None:
    repo = _bootstrap(tmp_path)
    proj = state.get_project("alpha")
    [task] = state.list_tasks(proj)

    # Make a real commit on the branch, then merge it into main.
    _commit_on_branch(task.worktree_path)
    subprocess.run(
        ["git", "-C", str(repo), "merge", "--no-ff", task.branch, "-m", "merge"],
        check=True,
    )

    runner = CliRunner()
    res = runner.invoke(app, ["task", "prune", "--no-fetch", "--force"])
    assert res.exit_code == 0, res.output
    assert state.list_tasks(proj) == []
    assert not task.worktree_path.exists()


def test_task_prune_dry_run_lists_but_doesnt_remove(isolated_xdg: Path, tmp_path: Path) -> None:
    repo = _bootstrap(tmp_path)
    proj = state.get_project("alpha")
    [task] = state.list_tasks(proj)
    _commit_on_branch(task.worktree_path)
    subprocess.run(
        ["git", "-C", str(repo), "merge", "--no-ff", task.branch, "-m", "merge"],
        check=True,
    )

    runner = CliRunner()
    res = runner.invoke(app, ["task", "prune", "--no-fetch", "--dry-run"])
    assert res.exit_code == 0, res.output
    assert task.id in res.output
    # Still registered.
    assert len(state.list_tasks(proj)) == 1


def test_task_ls_backfills_pr_url_from_gh(isolated_xdg: Path, tmp_path: Path) -> None:
    from unittest.mock import patch

    _bootstrap(tmp_path)
    proj = state.get_project("alpha")
    [task] = state.list_tasks(proj)
    assert task.pr_url is None

    fake = [
        {
            "headRefName": task.branch,
            "url": "https://github.com/x/y/pull/42",
            "state": "OPEN",
            "number": "42",
        }
    ]
    runner = CliRunner()
    with patch("goblin_watcher.commands.task.gh.list_repo_prs", return_value=fake):
        res = runner.invoke(app, ["task", "ls"])
    assert res.exit_code == 0, res.output
    assert "pr-open" in res.output

    [persisted] = state.list_tasks(proj)
    assert persisted.pr_url == "https://github.com/x/y/pull/42"
    assert persisted.status == "pr-open"


def test_task_ls_no_refresh_prs_skips_gh(isolated_xdg: Path, tmp_path: Path) -> None:
    from unittest.mock import patch

    _bootstrap(tmp_path)
    runner = CliRunner()
    with patch("goblin_watcher.commands.task.gh.list_repo_prs") as called:
        res = runner.invoke(app, ["task", "ls", "--no-refresh-prs"])
    assert res.exit_code == 0
    called.assert_not_called()


def test_task_ls_fuzzy_matches_pr_by_linear_id(isolated_xdg: Path, tmp_path: Path) -> None:
    """`-2` collision suffix on local branch and `user/` prefix on the PR
    branch both shouldn't block PR matching when there's a Linear ticket."""
    from unittest.mock import patch

    from goblin_watcher.models import LinearIssue

    repo = tmp_path / "alpha"
    _init_repo(repo)
    runner = CliRunner()
    runner.invoke(app, ["project", "new", "alpha", "--dir", str(repo), "--team", "PLAT"])
    runner.invoke(app, ["new", "--branch-name", "plat-7-do-the-thing-2", "--no-launch"])

    # Attach a Linear identifier so the fuzzy match kicks in.
    proj = state.get_project("alpha")
    [task] = state.list_tasks(proj)
    task = task.model_copy(
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
    )
    state.save_task(proj, task)

    fake = [
        {
            "headRefName": "you/plat-7-do-the-thing",
            "url": "https://github.com/x/y/pull/99",
            "state": "MERGED",
            "number": "99",
        }
    ]
    with patch("goblin_watcher.commands.task.gh.list_repo_prs", return_value=fake):
        res = runner.invoke(app, ["task", "ls"])
    assert res.exit_code == 0, res.output
    [persisted] = state.list_tasks(proj)
    assert persisted.pr_url == "https://github.com/x/y/pull/99"
    assert persisted.status == "merged"


def test_task_ls_fuzzy_match_requires_id_delimiter(isolated_xdg: Path, tmp_path: Path) -> None:
    """`plat-7` shouldn't match `plat-70-*` — the next character must be `-` or EOS."""
    from unittest.mock import patch

    from goblin_watcher.models import LinearIssue

    repo = tmp_path / "alpha"
    _init_repo(repo)
    runner = CliRunner()
    runner.invoke(app, ["project", "new", "alpha", "--dir", str(repo), "--team", "PLAT"])
    runner.invoke(app, ["new", "--branch-name", "plat-7-real", "--no-launch"])
    proj = state.get_project("alpha")
    [task] = state.list_tasks(proj)
    task = task.model_copy(
        update={
            "linear": LinearIssue(
                id="uuid",
                identifier="PLAT-7",
                title="x",
                state="x",
                team_key="PLAT",
                url="https://linear.app/x/issue/PLAT-7",
            )
        }
    )
    state.save_task(proj, task)

    fake = [
        {
            "headRefName": "plat-70-something-else",
            "url": "https://github.com/x/y/pull/100",
            "state": "OPEN",
            "number": "100",
        }
    ]
    with patch("goblin_watcher.commands.task.gh.list_repo_prs", return_value=fake):
        runner.invoke(app, ["task", "ls"])
    [persisted] = state.list_tasks(proj)
    assert persisted.pr_url is None  # No false match.


def test_task_prune_skips_unmerged(isolated_xdg: Path, tmp_path: Path) -> None:
    _bootstrap(tmp_path)
    proj = state.get_project("alpha")
    assert len(state.list_tasks(proj)) == 1

    runner = CliRunner()
    res = runner.invoke(app, ["task", "prune", "--no-fetch", "--force"])
    assert res.exit_code == 0, res.output
    # Task is unmerged — should still be there.
    assert len(state.list_tasks(proj)) == 1


def test_task_prune_skips_stale_project_registration(isolated_xdg: Path, tmp_path: Path) -> None:
    """A registered project whose metadata is gone must not abort pruning for
    the healthy projects."""
    import shutil

    from goblin_watcher import state as state_module

    repo = _bootstrap(tmp_path)
    del repo
    # Register a second project, then nuke its metadata so get_project fails.
    ghost = tmp_path / "ghost"
    _init_repo(ghost)
    runner = CliRunner()
    runner.invoke(app, ["project", "new", "ghost", "--dir", str(ghost)])
    shutil.rmtree(ghost / ".goblin")
    assert "ghost" in state_module.load_global().projects

    res = runner.invoke(app, ["task", "prune", "--dry-run", "--no-fetch"])
    assert res.exit_code == 0, res.output
    assert "Skipped project 'ghost'" in res.output


def test_task_rename_moves_record_only(isolated_xdg: Path, tmp_path: Path) -> None:
    _bootstrap(tmp_path)
    proj = state.get_project("alpha")
    [task] = state.list_tasks(proj)
    old_id = task.id

    runner = CliRunner()
    res = runner.invoke(app, ["task", "rename", old_id, "better-name"])
    assert res.exit_code == 0, res.output

    renamed = state.load_task(proj, "better-name")
    assert renamed.branch == task.branch
    assert renamed.worktree_path == task.worktree_path
    assert renamed.sessions == task.sessions
    assert not state.task_file(proj, old_id).exists()
    assert task.worktree_path.exists()  # worktree untouched on disk


def test_task_rename_rejects_same_id(isolated_xdg: Path, tmp_path: Path) -> None:
    from goblin_watcher.errors import GoblinError

    _bootstrap(tmp_path)
    proj = state.get_project("alpha")
    [task] = state.list_tasks(proj)
    runner = CliRunner()
    res = runner.invoke(app, ["task", "rename", task.id, task.id])
    assert res.exit_code != 0
    assert isinstance(res.exception, GoblinError)
    assert "already named" in res.exception.message


def test_task_rename_rejects_non_slug_id(isolated_xdg: Path, tmp_path: Path) -> None:
    from goblin_watcher.errors import GoblinError

    _bootstrap(tmp_path)
    proj = state.get_project("alpha")
    [task] = state.list_tasks(proj)
    runner = CliRunner()
    res = runner.invoke(app, ["task", "rename", task.id, "Spike Foo!"])
    assert res.exit_code != 0
    assert isinstance(res.exception, GoblinError)
    assert "spike-foo" in (res.exception.hint or "")
    # Nothing changed.
    assert state.task_file(proj, task.id).exists()


def test_task_rename_rejects_collision_in_same_project(isolated_xdg: Path, tmp_path: Path) -> None:
    from goblin_watcher.errors import GoblinError

    _bootstrap(tmp_path)
    runner = CliRunner()
    runner.invoke(app, ["new", "--branch-name", "other-task", "--no-launch"])
    res = runner.invoke(app, ["task", "rename", "spike-foo", "other-task"])
    assert res.exit_code != 0
    assert isinstance(res.exception, GoblinError)
    assert "already exists" in res.exception.message
    assert "alpha" in res.exception.message


def test_task_rename_rejects_collision_in_other_project(isolated_xdg: Path, tmp_path: Path) -> None:
    """An id in use by ANOTHER project is refused too — it would make the id
    ambiguous and force --project on every later command."""
    from goblin_watcher.errors import GoblinError

    _bootstrap_two_projects(tmp_path)
    runner = CliRunner()
    runner.invoke(app, ["new", "--project", "beta", "--branch-name", "beta-only", "--no-launch"])
    res = runner.invoke(app, ["task", "rename", "spike-foo", "beta-only", "--project", "alpha"])
    assert res.exit_code != 0
    assert isinstance(res.exception, GoblinError)
    assert "beta" in res.exception.message


def test_task_rename_project_scopes_lookup(isolated_xdg: Path, tmp_path: Path) -> None:
    """With the same task id in two projects, --project picks which one to rename."""
    _bootstrap_two_projects(tmp_path)
    proj_a = state.get_project("alpha")
    proj_b = state.get_project("beta")

    runner = CliRunner()
    res = runner.invoke(app, ["task", "rename", "spike-foo", "renamed", "--project", "beta"])
    assert res.exit_code == 0, res.output
    assert state.task_file(proj_b, "renamed").exists()
    assert not state.task_file(proj_b, "spike-foo").exists()
    # Alpha's identically-named task is untouched.
    assert state.task_file(proj_a, "spike-foo").exists()


def test_task_rename_ambiguous_without_project_errors(isolated_xdg: Path, tmp_path: Path) -> None:
    from goblin_watcher.errors import GoblinError

    _bootstrap_two_projects(tmp_path)
    runner = CliRunner()
    res = runner.invoke(app, ["task", "rename", "spike-foo", "renamed"])
    assert res.exit_code != 0
    assert isinstance(res.exception, GoblinError)
    assert "more than one project" in res.exception.message


def test_task_show_prints_the_github_issue(isolated_xdg: Path, tmp_path: Path) -> None:
    from goblin_watcher.models import GhIssue

    _bootstrap(tmp_path)
    proj = state.get_project("alpha")
    [task] = state.list_tasks(proj)
    state.save_task(
        proj,
        task.model_copy(
            update={
                "github_issue": GhIssue(
                    number=42,
                    repo="org/repo",
                    title="Add rate limit",
                    state="OPEN",
                    url="https://github.com/org/repo/issues/42",
                )
            }
        ),
    )

    res = CliRunner().invoke(app, ["task", "show", task.id])
    assert res.exit_code == 0, res.output
    assert "org/repo#42" in res.output
    # The state must survive Rich's markup parser (`[open]` would be eaten).
    assert "(open)" in res.output
    assert "https://github.com/org/repo/issues/42" in res.output


def test_first_pr_for_task_matches_a_github_issue_branch() -> None:
    """A re-created `gh-42` task lands on `gh-42-...-2`; the original PR is still
    matched via the `gh-42` basename."""
    from datetime import UTC, datetime

    from goblin_watcher.commands.task import _first_pr_for_task
    from goblin_watcher.models import GhIssue, Task

    issue = GhIssue(
        number=42,
        repo="org/repo",
        title="Add rate limit",
        state="OPEN",
        url="https://github.com/org/repo/issues/42",
    )
    task = Task(
        id="gh-42",
        project="alpha",
        github_issue=issue,
        branch="gh-42-add-rate-limit-2",
        worktree_path=Path("/tmp/wt"),
        base_branch="main",
        created_at=datetime.now(UTC),
    )
    prs = [
        {"headRefName": "unrelated", "url": "u1", "state": "OPEN", "number": "1"},
        {"headRefName": "dennis/gh-42-add-rate-limit", "url": "u2", "state": "OPEN", "number": "2"},
    ]
    assert _first_pr_for_task(prs, task) == prs[1]

    # No issue and no exact branch → no match, rather than a wrong one.
    plain = task.model_copy(update={"github_issue": None})
    assert _first_pr_for_task(prs, plain) is None
