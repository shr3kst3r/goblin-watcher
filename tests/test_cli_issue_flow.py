"""End-to-end tests for `gw new --issue` (GitHub issues as a task source)."""

import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from goblin_watcher import state
from goblin_watcher.cli import _rewrite_task_shortcut, app
from goblin_watcher.errors import GoblinError
from goblin_watcher.gh import IssueInfo


def _init_repo(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", "-b", "main", str(path)], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.name", "tester"], check=True)
    (path / "README.md").write_text("hi")
    subprocess.run(["git", "-C", str(path), "add", "."], check=True)
    subprocess.run(["git", "-C", str(path), "commit", "-qm", "init"], check=True)


def _register(repo: Path, name: str = "alpha", repo_url: str | None = None) -> None:
    args = ["project", "new", name, "--dir", str(repo)]
    res = CliRunner().invoke(app, args)
    assert res.exit_code == 0, res.output
    if repo_url is not None:
        # `project new --dir` records the local origin (or none); point the
        # registered project at a GitHub URL so repo matching can find it.
        proj = state.get_project(name)
        state.register_project(proj.model_copy(update={"repo_url": repo_url}))


def _issue(
    number: int = 42,
    repo: str = "org/repo",
    title: str = "Add rate limit",
    body: str = "We need a token bucket.",
    state_: str = "OPEN",
) -> IssueInfo:
    return IssueInfo(
        number=number,
        repo=repo,
        title=title,
        body=body,
        url=f"https://github.com/{repo}/issues/{number}",
        state=state_,
        labels=("enhancement",),
        assignees=("alice",),
    )


def test_new_issue_creates_task_and_branch(isolated_xdg: Path, tmp_path: Path) -> None:
    repo = tmp_path / "alpha"
    _init_repo(repo)
    _register(repo)

    runner = CliRunner()
    with patch("goblin_watcher.commands.new.gh.issue_view", return_value=_issue()) as issue_view:
        res = runner.invoke(app, ["new", "--issue", "42", "--project", "alpha", "--no-launch"])
    assert res.exit_code == 0, res.output
    # A bare number resolves against the project's repo — no --repo hint.
    assert issue_view.call_args.args[0].repo is None
    assert issue_view.call_args.args[0].number == 42

    proj = state.get_project("alpha")
    [task] = state.list_tasks(proj)
    assert task.id == "gh-42"
    assert task.branch == "gh-42-add-rate-limit"
    assert task.base_branch == "main"
    assert task.worktree_path.exists()
    assert task.github_issue is not None
    assert task.github_issue.number == 42
    assert task.github_issue.repo == "org/repo"
    assert task.github_issue.title == "Add rate limit"
    assert task.github_issue.body == "We need a token bucket."
    assert task.github_issue.state == "OPEN"
    assert task.github_issue.labels == ["enhancement"]
    assert task.github_issue.assignees == ["alice"]
    assert task.github_issue_state_updated_at is not None
    # The task record survives a round-trip through JSON.
    assert state.load_task(proj, "gh-42").github_issue == task.github_issue


def test_new_issue_honors_branch_prefix_and_from(isolated_xdg: Path, tmp_path: Path) -> None:
    repo = tmp_path / "alpha"
    _init_repo(repo)
    subprocess.run(["git", "-C", str(repo), "branch", "release/1.x"], check=True)
    _register(repo)
    proj = state.get_project("alpha")
    state.register_project(proj.model_copy(update={"branch_prefix": "dennis/"}))

    with patch("goblin_watcher.commands.new.gh.issue_view", return_value=_issue()):
        res = CliRunner().invoke(
            app,
            ["new", "--issue", "42", "--project", "alpha", "--from", "release/1.x", "--no-launch"],
        )
    assert res.exit_code == 0, res.output

    [task] = state.list_tasks(state.get_project("alpha"))
    assert task.branch == "dennis/gh-42-add-rate-limit"
    assert task.base_branch == "release/1.x"


def test_new_issue_url_resolves_project_from_repo(isolated_xdg: Path, tmp_path: Path) -> None:
    repo = tmp_path / "alpha"
    _init_repo(repo)
    _register(repo, repo_url="https://github.com/org/repo.git")

    with patch("goblin_watcher.commands.new.gh.issue_view", return_value=_issue()):
        res = CliRunner().invoke(
            app,
            ["new", "--issue", "https://github.com/org/repo/issues/42", "--no-launch"],
        )
    assert res.exit_code == 0, res.output

    [task] = state.list_tasks(state.get_project("alpha"))
    assert task.id == "gh-42"
    assert task.github_issue is not None
    assert task.github_issue.url == "https://github.com/org/repo/issues/42"


def test_new_issue_cross_repo_uses_explicit_project(isolated_xdg: Path, tmp_path: Path) -> None:
    """A tracking issue in another repo works when --project names where to work."""
    repo = tmp_path / "alpha"
    _init_repo(repo)
    _register(repo, repo_url="https://github.com/org/repo.git")

    tracking = _issue(number=3, repo="org/tracker", title="Ship the thing")
    with patch("goblin_watcher.commands.new.gh.issue_view", return_value=tracking) as issue_view:
        res = CliRunner().invoke(
            app, ["new", "--issue", "org/tracker#3", "--project", "alpha", "--no-launch"]
        )
    assert res.exit_code == 0, res.output
    assert issue_view.call_args.args[0].repo == "org/tracker"

    [task] = state.list_tasks(state.get_project("alpha"))
    assert task.id == "gh-3"
    assert task.branch == "gh-3-ship-the-thing"
    assert task.github_issue is not None
    assert task.github_issue.repo == "org/tracker"
    assert task.github_issue.reference == "org/tracker#3"


def test_new_issue_cross_repo_falls_back_to_cwd_project(
    isolated_xdg: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "alpha"
    _init_repo(repo)
    _register(repo, repo_url="https://github.com/org/repo.git")
    monkeypatch.chdir(repo)

    tracking = _issue(number=3, repo="org/tracker", title="Ship the thing")
    with patch("goblin_watcher.commands.new.gh.issue_view", return_value=tracking):
        res = CliRunner().invoke(app, ["new", "--issue", "org/tracker#3", "--no-launch"])
    assert res.exit_code == 0, res.output

    [task] = state.list_tasks(state.get_project("alpha"))
    assert task.id == "gh-3"
    assert task.github_issue is not None and task.github_issue.repo == "org/tracker"


def test_new_issue_unmatched_repo_outside_a_project_errors(
    isolated_xdg: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "alpha"
    _init_repo(repo)
    _register(repo)  # origin is a local path, not the issue's GitHub repo
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)

    res = CliRunner().invoke(app, ["new", "--issue", "org/other#9", "--no-launch"])
    assert res.exit_code != 0
    assert isinstance(res.exception, GoblinError)
    assert "org/other" in res.exception.message


def test_new_issue_rejects_bad_reference(isolated_xdg: Path, tmp_path: Path) -> None:
    repo = tmp_path / "alpha"
    _init_repo(repo)
    _register(repo)

    res = CliRunner().invoke(
        app, ["new", "--issue", "ENG-123", "--project", "alpha", "--no-launch"]
    )
    assert res.exit_code != 0
    assert isinstance(res.exception, GoblinError)
    assert "not a GitHub issue reference" in res.exception.message


def test_new_issue_twice_refuses_without_rm(isolated_xdg: Path, tmp_path: Path) -> None:
    """`gh-<number>` is only unique per repo; a collision is refused, not renamed."""
    repo = tmp_path / "alpha"
    _init_repo(repo)
    _register(repo)

    runner = CliRunner()
    with patch("goblin_watcher.commands.new.gh.issue_view", return_value=_issue()):
        first = runner.invoke(app, ["new", "--issue", "42", "--project", "alpha", "--no-launch"])
    assert first.exit_code == 0, first.output

    other = _issue(number=42, repo="org/tracker", title="Different issue, same number")
    with patch("goblin_watcher.commands.new.gh.issue_view", return_value=other):
        second = runner.invoke(
            app, ["new", "--issue", "org/tracker#42", "--project", "alpha", "--no-launch"]
        )
    assert second.exit_code != 0
    assert isinstance(second.exception, GoblinError)
    assert "already exists" in second.exception.message

    # --rm replaces it, reusing the same id and branch.
    with patch("goblin_watcher.commands.new.gh.issue_view", return_value=other):
        third = runner.invoke(
            app, ["new", "--issue", "org/tracker#42", "--project", "alpha", "--no-launch", "--rm"]
        )
    assert third.exit_code == 0, third.output
    [task] = state.list_tasks(state.get_project("alpha"))
    assert task.id == "gh-42"
    assert task.github_issue is not None and task.github_issue.repo == "org/tracker"


def test_new_issue_research_seeds_the_research_brief(isolated_xdg: Path, tmp_path: Path) -> None:
    repo = tmp_path / "alpha"
    _init_repo(repo)
    _register(repo)

    runner = CliRunner()
    with (
        patch("goblin_watcher.commands.new.gh.issue_view", return_value=_issue()),
        patch("goblin_watcher.commands.new.launch", return_value=(0, None)) as launch,
    ):
        res = runner.invoke(app, ["new", "--issue", "42", "--project", "alpha", "--research"])
    assert res.exit_code == 0, res.output

    choice = launch.call_args.kwargs["choice"]
    assert type(choice).__name__ == "Fresh"
    assert choice.prompt.startswith("Research task —")
    assert "Report your findings here, in this session" in choice.prompt
    # The issue context survives, the PR-opening instruction does not.
    assert "org/repo#42: Add rate limit" in choice.prompt
    assert "We need a token bucket." in choice.prompt
    assert "open a PR via" not in choice.prompt


def test_new_issue_research_composes_with_prompt(isolated_xdg: Path, tmp_path: Path) -> None:
    """`--prompt` narrows a research session instead of conflicting with it
    (ADR 0006) — the combination the README leads with."""
    repo = tmp_path / "alpha"
    _init_repo(repo)
    _register(repo)

    runner = CliRunner()
    with (
        patch("goblin_watcher.commands.new.gh.issue_view", return_value=_issue()),
        patch("goblin_watcher.commands.new.launch", return_value=(0, None)) as launch,
    ):
        res = runner.invoke(
            app,
            [
                "new",
                "--issue",
                "42",
                "--project",
                "alpha",
                "--research",
                "--prompt",
                "Just the sync path.",
            ],
        )
    assert res.exit_code == 0, res.output

    choice = launch.call_args.kwargs["choice"]
    assert choice.prompt.startswith("Research task —")
    assert "Focus this research on the following" in choice.prompt
    assert "Just the sync path." in choice.prompt
    # The focus narrows the brief; it does not become the work template's trailer.
    assert "open a PR via" not in choice.prompt


def test_new_issue_conflicts_with_other_sources(isolated_xdg: Path, tmp_path: Path) -> None:
    repo = tmp_path / "alpha"
    _init_repo(repo)
    _register(repo)

    res = CliRunner().invoke(app, ["new", "--issue", "42", "--branch", "main", "--no-launch"])
    assert res.exit_code != 0


def test_gh_shorthand_rewrites_to_issue_source() -> None:
    assert _rewrite_task_shortcut(["gh-42"]) == ["new", "--issue", "42"]
    assert _rewrite_task_shortcut(["GH-42"]) == ["new", "--issue", "42"]
    assert _rewrite_task_shortcut(["gh-42", "--agent", "codex"]) == [
        "new",
        "--issue",
        "42",
        "--agent",
        "codex",
    ]
    assert _rewrite_task_shortcut(["--debug", "gh-7"]) == ["--debug", "new", "--issue", "7"]
    # `gh-<digits>` is claimed by the issue shorthand, so a Linear team keyed
    # `GH` needs the explicit flag. Everything else still routes to Linear.
    assert _rewrite_task_shortcut(["ENG-123"]) == ["new", "--linear", "ENG-123"]
    assert _rewrite_task_shortcut(["ghost-1"]) == ["new", "--linear", "ghost-1"]
    # Neither pattern matches a trailing non-digit, so argv is left alone.
    assert _rewrite_task_shortcut(["gh-42x"]) == ["gh-42x"]
    # Subcommands keep their positionals.
    assert _rewrite_task_shortcut(["run", "gh-42"]) == ["run", "gh-42"]


def test_gh_shorthand_creates_the_task(isolated_xdg: Path, tmp_path: Path) -> None:
    repo = tmp_path / "alpha"
    _init_repo(repo)
    _register(repo)

    argv = _rewrite_task_shortcut(["gh-42", "--project", "alpha", "--no-launch"])
    with patch("goblin_watcher.commands.new.gh.issue_view", return_value=_issue()):
        res = CliRunner().invoke(app, argv)
    assert res.exit_code == 0, res.output
    [task] = state.list_tasks(state.get_project("alpha"))
    assert task.id == "gh-42"


def test_new_issue_with_repo_clones_and_registers(
    isolated_xdg: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`--repo` bootstraps a project the same way the Linear path does."""
    upstream = tmp_path / "upstream"
    _init_repo(upstream)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)

    with patch("goblin_watcher.commands.new.gh.issue_view", return_value=_issue()):
        res = CliRunner().invoke(
            app, ["new", "--issue", "42", "--repo", str(upstream), "--no-launch"]
        )
    assert res.exit_code == 0, res.output

    proj = state.get_project("upstream")
    assert proj.repo_url == str(upstream)
    [task] = state.list_tasks(proj)
    assert task.id == "gh-42"
    assert task.worktree_path.exists()


def test_new_issue_cross_repo_with_repo_names_project_after_the_working_repo(
    isolated_xdg: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With a cross-repo tracking issue, the cloned project is named after
    --repo (where the work happens), not after the issue's repo."""
    upstream = tmp_path / "worker"
    _init_repo(upstream)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)

    tracking = _issue(number=3, repo="org/tracker", title="Ship the thing")
    with patch("goblin_watcher.commands.new.gh.issue_view", return_value=tracking):
        res = CliRunner().invoke(
            app, ["new", "--issue", "org/tracker#3", "--repo", str(upstream), "--no-launch"]
        )
    assert res.exit_code == 0, res.output

    assert "tracker" not in state.load_global().projects
    [task] = state.list_tasks(state.get_project("worker"))
    assert task.id == "gh-3"
    assert task.github_issue is not None and task.github_issue.repo == "org/tracker"


def test_new_issue_repo_flag_reuses_a_registered_project(
    isolated_xdg: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """--repo pointing at an already-registered remote must not clone a second copy."""
    repo = tmp_path / "alpha"
    _init_repo(repo)
    _register(repo, repo_url="https://github.com/org/repo.git")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)

    tracking = _issue(number=3, repo="org/tracker", title="Ship the thing")
    with (
        patch("goblin_watcher.commands.new.gh.issue_view", return_value=tracking),
        patch("goblin_watcher.commands.new.git.clone") as clone,
    ):
        res = CliRunner().invoke(
            app,
            [
                "new",
                "--issue",
                "org/tracker#3",
                "--repo",
                "git@github.com:org/repo.git",
                "--no-launch",
            ],
        )
    assert res.exit_code == 0, res.output
    clone.assert_not_called()
    assert sorted(state.load_global().projects) == ["alpha"]
    [task] = state.list_tasks(state.get_project("alpha"))
    assert task.id == "gh-3"
