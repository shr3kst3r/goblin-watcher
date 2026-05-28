import subprocess
from pathlib import Path

from typer.testing import CliRunner

from goblin_watcher import state
from goblin_watcher.cli import app


def _init_repo(path: Path, branch: str = "main") -> None:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", "-b", branch, str(path)], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.name", "tester"], check=True)
    (path / "README.md").write_text("hi")
    subprocess.run(["git", "-C", str(path), "add", "."], check=True)
    subprocess.run(["git", "-C", str(path), "commit", "-qm", "init"], check=True)


def _register_project(repo: Path, name: str = "alpha") -> None:
    runner = CliRunner()
    res = runner.invoke(app, ["project", "new", name, "--dir", str(repo)])
    assert res.exit_code == 0, res.output


def test_new_branch_name_creates_branch_and_worktree(isolated_xdg: Path, tmp_path: Path) -> None:
    repo = tmp_path / "alpha"
    _init_repo(repo)
    _register_project(repo)

    runner = CliRunner()
    res = runner.invoke(
        app,
        ["new", "--branch-name", "spike/foo", "--title", "Trying a thing", "--no-launch"],
    )
    assert res.exit_code == 0, res.output

    proj = state.get_project("alpha")
    tasks = state.list_tasks(proj)
    assert len(tasks) == 1
    task = tasks[0]
    assert task.branch == "spike/foo"
    assert task.worktree_path.exists()
    assert task.base_branch == "main"


def test_new_branch_name_from_remote_only_base_auto_fetches(
    isolated_xdg: Path, tmp_path: Path
) -> None:
    """`--branch-name X --from feat/pr` works even when the base branch only
    exists on origin: gw fetches and creates a local tracking branch."""
    upstream = tmp_path / "upstream"
    _init_repo(upstream)
    subprocess.run(["git", "-C", str(upstream), "checkout", "-q", "-b", "feat/pr-base"], check=True)
    (upstream / "extra.txt").write_text("pr work")
    subprocess.run(["git", "-C", str(upstream), "add", "."], check=True)
    subprocess.run(["git", "-C", str(upstream), "commit", "-qm", "pr work"], check=True)
    subprocess.run(["git", "-C", str(upstream), "checkout", "-q", "main"], check=True)

    repo = tmp_path / "alpha"
    subprocess.run(["git", "clone", "-q", str(upstream), str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "tester"], check=True)
    _register_project(repo)

    runner = CliRunner()
    res = runner.invoke(
        app,
        ["new", "--branch-name", "spike/foo", "--from", "feat/pr-base", "--no-launch"],
    )
    assert res.exit_code == 0, res.output

    proj = state.get_project("alpha")
    [task] = state.list_tasks(proj)
    assert task.branch == "spike/foo"
    assert task.base_branch == "feat/pr-base"
    assert (task.worktree_path / "extra.txt").exists()


def test_new_branch_name_refreshes_default_base_from_remote(
    isolated_xdg: Path, tmp_path: Path
) -> None:
    """Creating a new branch from the project's default base picks up commits
    that landed on `origin/main` since the clone, so worktrees never start stale."""
    upstream = tmp_path / "upstream"
    _init_repo(upstream)

    repo = tmp_path / "alpha"
    subprocess.run(["git", "clone", "-q", str(upstream), str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "tester"], check=True)
    _register_project(repo)

    # Upstream advances after the clone is registered.
    (upstream / "upstream-only.txt").write_text("from upstream\n")
    subprocess.run(["git", "-C", str(upstream), "add", "."], check=True)
    subprocess.run(["git", "-C", str(upstream), "commit", "-qm", "upstream advance"], check=True)

    runner = CliRunner()
    res = runner.invoke(app, ["new", "--branch-name", "spike/foo", "--no-launch"])
    assert res.exit_code == 0, res.output

    proj = state.get_project("alpha")
    [task] = state.list_tasks(proj)
    # The new worktree must contain the upstream-only file, proving main was
    # fast-forwarded before the worktree was branched off.
    assert (task.worktree_path / "upstream-only.txt").exists()


def test_new_existing_branch_uses_local_branch(isolated_xdg: Path, tmp_path: Path) -> None:
    repo = tmp_path / "alpha"
    _init_repo(repo)
    subprocess.run(["git", "-C", str(repo), "branch", "feat/pre-existing"], check=True)
    _register_project(repo)

    runner = CliRunner()
    res = runner.invoke(app, ["new", "--branch", "feat/pre-existing", "--no-launch"])
    assert res.exit_code == 0, res.output

    proj = state.get_project("alpha")
    [task] = state.list_tasks(proj)
    assert task.branch == "feat/pre-existing"
    assert task.worktree_path.exists()


def test_new_dir_adopts_existing_checkout(isolated_xdg: Path, tmp_path: Path) -> None:
    repo = tmp_path / "alpha"
    _init_repo(repo)
    _register_project(repo)

    # Create a second checkout (worktree) outside the project's .worktrees dir.
    external = tmp_path / "external-checkout"
    subprocess.run(
        ["git", "-C", str(repo), "worktree", "add", str(external), "-b", "spike/external"],
        check=True,
    )

    runner = CliRunner()
    res = runner.invoke(app, ["new", "--dir", str(external), "--title", "Sandbox", "--no-launch"])
    assert res.exit_code == 0, res.output

    proj = state.get_project("alpha")
    [task] = state.list_tasks(proj)
    assert task.branch == "spike/external"
    assert task.worktree_path == external.resolve()


def test_new_branch_auto_creates_random_branch(isolated_xdg: Path, tmp_path: Path) -> None:
    from goblin_watcher.slug import _WORDS

    repo = tmp_path / "alpha"
    _init_repo(repo)
    _register_project(repo)

    runner = CliRunner()
    res = runner.invoke(app, ["new", "--branch-auto", "--no-launch"])
    assert res.exit_code == 0, res.output

    proj = state.get_project("alpha")
    [task] = state.list_tasks(proj)
    project, _, word = task.branch.rpartition("-")
    assert project == "alpha"
    assert word in _WORDS
    assert task.worktree_path.exists()


def test_new_prompt_rejects_no_launch(isolated_xdg: Path, tmp_path: Path) -> None:
    repo = tmp_path / "alpha"
    _init_repo(repo)
    _register_project(repo)

    runner = CliRunner()
    res = runner.invoke(
        app,
        ["new", "--branch-name", "spike/foo", "--no-launch", "--prompt", "do work"],
    )
    assert res.exit_code != 0
    assert res.exception is not None
    assert "no effect with --no-launch" in str(res.exception)


def test_new_prompt_seeds_fresh_session(isolated_xdg: Path, tmp_path: Path) -> None:
    from unittest.mock import patch

    repo = tmp_path / "alpha"
    _init_repo(repo)
    _register_project(repo)

    runner = CliRunner()
    with patch(
        "goblin_watcher.commands.new.launch",
        return_value=(0, None),
    ) as launch:
        res = runner.invoke(
            app,
            ["new", "--branch-name", "spike/foo", "--prompt", "Refactor the foo module."],
        )
    assert res.exit_code == 0, res.output
    choice = launch.call_args.kwargs["choice"]
    assert type(choice).__name__ == "Fresh"
    assert "Refactor the foo module." in choice.prompt
    assert "Wait for my next message" not in choice.prompt


def test_new_adversarial_review_seeds_slash_command(isolated_xdg: Path, tmp_path: Path) -> None:
    from unittest.mock import patch

    repo = tmp_path / "alpha"
    _init_repo(repo)
    _register_project(repo)

    runner = CliRunner()
    with patch(
        "goblin_watcher.commands.new.launch",
        return_value=(0, None),
    ) as launch:
        res = runner.invoke(
            app,
            ["new", "--branch-name", "spike/foo", "--adversarial-review"],
        )
    assert res.exit_code == 0, res.output
    choice = launch.call_args.kwargs["choice"]
    assert type(choice).__name__ == "Fresh"
    # Slash command must be the entire user message, not buried in the
    # seed template — Claude Code's parser only fires it then.
    assert choice.prompt == "/codex:adversarial-review --wait"
    # The agent should resolve to claude regardless of config default.
    agent_obj = launch.call_args.kwargs["agent"]
    assert agent_obj.name == "claude"


def test_new_adversarial_review_rejects_no_launch(isolated_xdg: Path, tmp_path: Path) -> None:
    repo = tmp_path / "alpha"
    _init_repo(repo)
    _register_project(repo)

    runner = CliRunner()
    res = runner.invoke(
        app,
        ["new", "--branch-name", "spike/foo", "--adversarial-review", "--no-launch"],
    )
    assert res.exit_code != 0
    assert res.exception is not None
    assert "no effect with --no-launch" in str(res.exception)


def test_new_adversarial_review_conflicts_with_prompt(isolated_xdg: Path, tmp_path: Path) -> None:
    repo = tmp_path / "alpha"
    _init_repo(repo)
    _register_project(repo)

    runner = CliRunner()
    res = runner.invoke(
        app,
        [
            "new",
            "--branch-name",
            "spike/foo",
            "--adversarial-review",
            "--prompt",
            "do work",
        ],
    )
    assert res.exit_code != 0
    assert res.exception is not None
    assert "mutually exclusive" in str(res.exception)


def test_new_adversarial_review_rejects_non_claude_agent(
    isolated_xdg: Path, tmp_path: Path
) -> None:
    repo = tmp_path / "alpha"
    _init_repo(repo)
    _register_project(repo)

    runner = CliRunner()
    res = runner.invoke(
        app,
        [
            "new",
            "--branch-name",
            "spike/foo",
            "--adversarial-review",
            "--agent",
            "codex",
        ],
    )
    assert res.exit_code != 0
    assert res.exception is not None
    assert "requires --agent claude" in str(res.exception)


def _clone_with_pr_branch(tmp_path: Path, *, head: str = "feat/pr-42", base: str = "main") -> Path:
    """Build an upstream carrying a PR head branch, clone it, return the clone."""
    upstream = tmp_path / "upstream"
    _init_repo(upstream, branch=base)
    subprocess.run(["git", "-C", str(upstream), "checkout", "-q", "-b", head], check=True)
    (upstream / "pr-work.txt").write_text("pr work")
    subprocess.run(["git", "-C", str(upstream), "add", "."], check=True)
    subprocess.run(["git", "-C", str(upstream), "commit", "-qm", "pr work"], check=True)
    subprocess.run(["git", "-C", str(upstream), "checkout", "-q", base], check=True)

    repo = tmp_path / "alpha"
    subprocess.run(["git", "clone", "-q", str(upstream), str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "tester"], check=True)
    return repo


def test_new_pr_creates_worktree_from_head(isolated_xdg: Path, tmp_path: Path) -> None:
    from unittest.mock import patch

    from goblin_watcher.gh import PrInfo

    repo = _clone_with_pr_branch(tmp_path)
    _register_project(repo)

    info = PrInfo(
        number=42,
        head_ref="feat/pr-42",
        base_ref="main",
        url="https://github.com/org/repo/pull/42",
        title="Add a thing",
        state="OPEN",
        is_cross_repository=False,
    )
    runner = CliRunner()
    with patch("goblin_watcher.commands.new.gh.pr_view", return_value=info) as pr_view:
        res = runner.invoke(app, ["new", "--pr", "42", "--project", "alpha", "--no-launch"])
    assert res.exit_code == 0, res.output
    assert pr_view.call_args.args[0] == "42"

    proj = state.get_project("alpha")
    [task] = state.list_tasks(proj)
    assert task.branch == "feat/pr-42"
    assert task.base_branch == "main"
    assert task.pr_url == "https://github.com/org/repo/pull/42"
    assert task.status == "pr-open"
    assert (task.worktree_path / "pr-work.txt").exists()


def test_new_pr_resolves_project_from_url(isolated_xdg: Path, tmp_path: Path) -> None:
    from unittest.mock import patch

    from goblin_watcher.gh import PrInfo

    repo = _clone_with_pr_branch(tmp_path)
    # Materialize the head branch locally, then point origin at a GitHub URL so
    # the PR URL resolves to this project. (The later fetch against the fake
    # URL fails harmlessly since the branch already exists locally.)
    subprocess.run(
        ["git", "-C", str(repo), "branch", "feat/pr-42", "origin/feat/pr-42"], check=True
    )
    subprocess.run(
        ["git", "-C", str(repo), "remote", "set-url", "origin", "https://github.com/org/repo.git"],
        check=True,
    )
    _register_project(repo)

    info = PrInfo(
        number=42,
        head_ref="feat/pr-42",
        base_ref="main",
        url="https://github.com/org/repo/pull/42",
        title="Add a thing",
        state="OPEN",
        is_cross_repository=False,
    )
    runner = CliRunner()
    # No --project: the repo must be inferred from the PR URL.
    with patch("goblin_watcher.commands.new.gh.pr_view", return_value=info):
        res = runner.invoke(
            app,
            ["new", "--pr", "https://github.com/org/repo/pull/42", "--no-launch"],
        )
    assert res.exit_code == 0, res.output

    proj = state.get_project("alpha")
    [task] = state.list_tasks(proj)
    assert task.branch == "feat/pr-42"
    assert task.pr_url == "https://github.com/org/repo/pull/42"


def test_new_pr_rejects_fork(isolated_xdg: Path, tmp_path: Path) -> None:
    from unittest.mock import patch

    from goblin_watcher.gh import PrInfo

    repo = _clone_with_pr_branch(tmp_path)
    _register_project(repo)

    info = PrInfo(
        number=7,
        head_ref="feat/forked",
        base_ref="main",
        url="https://github.com/org/repo/pull/7",
        title="From a fork",
        state="OPEN",
        is_cross_repository=True,
    )
    runner = CliRunner()
    with patch("goblin_watcher.commands.new.gh.pr_view", return_value=info):
        res = runner.invoke(app, ["new", "--pr", "7", "--project", "alpha", "--no-launch"])
    assert res.exit_code != 0
    assert res.exception is not None
    assert "fork" in str(res.exception).lower()
    assert not state.list_tasks(state.get_project("alpha"))


def test_new_pr_url_without_matching_project_errors(isolated_xdg: Path, tmp_path: Path) -> None:
    repo = _clone_with_pr_branch(tmp_path)
    _register_project(repo)  # origin is a local path, not the PR's github repo

    runner = CliRunner()
    res = runner.invoke(
        app,
        ["new", "--pr", "https://github.com/org/other/pull/9", "--no-launch"],
    )
    assert res.exit_code != 0
    assert res.exception is not None
    assert "org/other" in str(res.exception)


def test_new_pr_conflicts_with_other_source(isolated_xdg: Path, tmp_path: Path) -> None:
    repo = tmp_path / "alpha"
    _init_repo(repo)
    _register_project(repo)

    runner = CliRunner()
    res = runner.invoke(app, ["new", "--pr", "42", "--branch", "main", "--no-launch"])
    assert res.exit_code != 0


def test_new_rejects_zero_or_multiple_sources(isolated_xdg: Path, tmp_path: Path) -> None:
    repo = tmp_path / "alpha"
    _init_repo(repo)
    _register_project(repo)

    runner = CliRunner()
    res = runner.invoke(app, ["new"])
    assert res.exit_code != 0

    res2 = runner.invoke(app, ["new", "--branch", "main", "--branch-name", "spike/foo"])
    assert res2.exit_code != 0

    res3 = runner.invoke(app, ["new", "--branch-auto", "--branch-name", "spike/foo"])
    assert res3.exit_code != 0


def test_new_linear_without_key_errors(isolated_xdg: Path, monkeypatch) -> None:
    """Without LINEAR_API_KEY (and no config), --linear should refuse cleanly."""
    from goblin_watcher.errors import LinearAuthError

    monkeypatch.delenv("LINEAR_API_KEY", raising=False)
    runner = CliRunner()
    res = runner.invoke(app, ["new", "--linear", "ENG-1", "--no-launch"])
    assert res.exit_code != 0
    assert isinstance(res.exception, LinearAuthError)


def test_linear_shortcut_dispatcher_rewrites_argv() -> None:
    # Smoke: the rewriter recognizes Linear ids and rewrites the args.
    from goblin_watcher.cli import _rewrite_linear_shortcut

    assert _rewrite_linear_shortcut(["ENG-123"]) == ["new", "--linear", "ENG-123"]
    assert _rewrite_linear_shortcut(["ENG-123", "--agent", "claude"]) == [
        "new",
        "--linear",
        "ENG-123",
        "--agent",
        "claude",
    ]
    assert _rewrite_linear_shortcut(["--debug", "ENG-123"]) == [
        "--debug",
        "new",
        "--linear",
        "ENG-123",
    ]
    # Non-Linear positional is untouched.
    assert _rewrite_linear_shortcut(["project", "ls"]) == ["project", "ls"]
