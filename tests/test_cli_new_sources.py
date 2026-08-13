import subprocess
from pathlib import Path

from typer.testing import CliRunner

from goblin_watcher import git, state
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


def test_new_research_rejects_no_launch(isolated_xdg: Path, tmp_path: Path) -> None:
    repo = tmp_path / "alpha"
    _init_repo(repo)
    _register_project(repo)

    runner = CliRunner()
    res = runner.invoke(app, ["new", "--issue", "42", "--research", "--no-launch"])
    assert res.exit_code != 0
    assert res.exception is not None
    assert "--research has no effect with --no-launch" in str(res.exception)


def test_new_research_conflicts_with_adversarial_review(isolated_xdg: Path, tmp_path: Path) -> None:
    repo = tmp_path / "alpha"
    _init_repo(repo)
    _register_project(repo)

    runner = CliRunner()
    res = runner.invoke(app, ["new", "--issue", "42", "--research", "--adversarial-review"])
    assert res.exit_code != 0
    assert res.exception is not None
    assert "--research and --adversarial-review are mutually exclusive" in str(res.exception)


def test_new_research_and_adversarial_review_conflict_is_reported_first(
    isolated_xdg: Path, tmp_path: Path
) -> None:
    """The mode conflict outranks --adversarial-review's own checks: --agent and
    --prompt are both fine with --research, so pointing at them would send the
    user off fixing the wrong flag."""
    repo = tmp_path / "alpha"
    _init_repo(repo)
    _register_project(repo)

    runner = CliRunner()
    for extra in (["--agent", "codex"], ["--prompt", "focus on sync"], ["--no-launch"]):
        res = runner.invoke(
            app, ["new", "--issue", "42", "--research", "--adversarial-review", *extra]
        )
        assert res.exit_code != 0
        assert res.exception is not None
        assert "--research and --adversarial-review are mutually exclusive" in str(res.exception), (
            extra
        )


def test_new_research_requires_a_tracking_item(isolated_xdg: Path, tmp_path: Path) -> None:
    """A research brief about nothing is a silent no-op, so the sources that
    attach no tracking item are refused before anything is created (ADR 0006)."""
    from unittest.mock import patch

    repo = tmp_path / "alpha"
    _init_repo(repo)
    _register_project(repo)

    runner = CliRunner()
    with patch("goblin_watcher.commands.new.launch") as launch:
        res = runner.invoke(app, ["new", "--branch-name", "spike/foo", "--research"])
    assert res.exit_code != 0
    assert res.exception is not None
    assert "--research requires a tracking item." in str(res.exception)
    launch.assert_not_called()
    # Refused before the branch/worktree was created.
    assert state.list_tasks(state.get_project("alpha")) == []


# ---------- `--mode` (ADR 0009): the registry the boolean flags are aliases for.


def test_new_mode_adversarial_review_matches_the_alias(isolated_xdg: Path, tmp_path: Path) -> None:
    from unittest.mock import patch

    repo = tmp_path / "alpha"
    _init_repo(repo)
    _register_project(repo)

    runner = CliRunner()
    with patch("goblin_watcher.commands.new.launch", return_value=(0, None)) as launch:
        res = runner.invoke(
            app, ["new", "--branch-name", "spike/foo", "--mode", "adversarial-review"]
        )
    assert res.exit_code == 0, res.output
    assert launch.call_args.kwargs["choice"].prompt == "/codex:adversarial-review --wait"
    assert launch.call_args.kwargs["agent"].name == "claude"


def test_new_mode_research_matches_the_alias(isolated_xdg: Path, tmp_path: Path) -> None:
    from unittest.mock import patch

    from goblin_watcher.gh import IssueInfo

    repo = tmp_path / "alpha"
    _init_repo(repo)
    _register_project(repo)

    info = IssueInfo(
        number=42,
        repo="org/repo",
        title="Add rate limit",
        body="We need a token bucket.",
        state="OPEN",
        url="https://github.com/org/repo/issues/42",
        labels=(),
        assignees=(),
    )
    runner = CliRunner()
    with (
        patch("goblin_watcher.commands.new.gh.issue_view", return_value=info),
        patch("goblin_watcher.commands.new.launch", return_value=(0, None)) as launch,
    ):
        res = runner.invoke(
            app, ["new", "--issue", "42", "--project", "alpha", "--mode", "research"]
        )
    assert res.exit_code == 0, res.output
    prompt = launch.call_args.kwargs["choice"].prompt
    assert prompt.startswith("Research task —")
    assert "org/repo#42: Add rate limit" in prompt


def test_new_mode_and_a_matching_alias_are_not_a_conflict(
    isolated_xdg: Path, tmp_path: Path
) -> None:
    """`--mode research --research` names one mode twice, which is harmless.
    Only two *different* modes conflict."""
    from unittest.mock import patch

    repo = tmp_path / "alpha"
    _init_repo(repo)
    _register_project(repo)

    runner = CliRunner()
    with patch("goblin_watcher.commands.new.launch", return_value=(0, None)):
        res = runner.invoke(
            app,
            [
                "new",
                "--branch-name",
                "spike/foo",
                "--mode",
                "adversarial-review",
                "--adversarial-review",
            ],
        )
    assert res.exit_code == 0, res.output


def test_new_mode_conflicting_with_an_alias_is_refused(isolated_xdg: Path, tmp_path: Path) -> None:
    repo = tmp_path / "alpha"
    _init_repo(repo)
    _register_project(repo)

    runner = CliRunner()
    res = runner.invoke(
        app, ["new", "--branch-name", "spike/foo", "--mode", "research", "--adversarial-review"]
    )
    assert res.exit_code != 0
    assert res.exception is not None
    assert "--mode research and --adversarial-review are mutually exclusive" in str(res.exception)


def test_new_unknown_mode_lists_the_available_ones(isolated_xdg: Path, tmp_path: Path) -> None:
    repo = tmp_path / "alpha"
    _init_repo(repo)
    _register_project(repo)

    runner = CliRunner()
    res = runner.invoke(app, ["new", "--branch-name", "spike/foo", "--mode", "reserch"])
    assert res.exit_code != 0
    assert res.exception is not None
    assert "Unknown mode 'reserch'" in str(res.exception)


def test_new_user_defined_mode_seeds_its_own_template(isolated_xdg: Path, tmp_path: Path) -> None:
    """A mode added to config.toml works without patching new.py — the point of
    the registry (ADR 0009)."""
    from unittest.mock import patch

    from goblin_watcher import config, paths
    from goblin_watcher.modes import ModeSpec

    repo = tmp_path / "alpha"
    _init_repo(repo)
    _register_project(repo)

    paths.config_dir().mkdir(parents=True, exist_ok=True)
    (paths.config_dir() / "spike_prompt.md").write_text(
        "Spike on {ticket_id}: {title}\n\n{repos_block}\n\n{description}\n{focus}"
    )
    cfg = config.load()
    cfg.modes["spike"] = ModeSpec(template="spike_prompt.md")
    config.save(cfg)

    runner = CliRunner()
    with patch("goblin_watcher.commands.new.launch", return_value=(0, None)) as launch:
        res = runner.invoke(
            app,
            ["new", "--branch-name", "spike/foo", "--mode", "spike", "--prompt", "the retry loop"],
        )
    assert res.exit_code == 0, res.output
    prompt = launch.call_args.kwargs["choice"].prompt
    assert prompt.startswith("Spike on SPIKE-FOO: spike-foo")
    # --prompt composes with a template mode, as a focus paragraph.
    assert "Focus on the following in particular:" in prompt
    assert "the retry loop" in prompt
    # A user mode inherits no built-in's constraints: `spike` never set
    # `requires_ticket`, so a --branch-name task is a valid target.
    assert "(no Linear issue or GitHub issue attached — fresh task)" in prompt


def test_new_user_mode_with_an_unknown_slot_is_a_goblin_error(
    isolated_xdg: Path, tmp_path: Path
) -> None:
    from goblin_watcher import config, paths
    from goblin_watcher.modes import ModeSpec

    repo = tmp_path / "alpha"
    _init_repo(repo)
    _register_project(repo)

    paths.config_dir().mkdir(parents=True, exist_ok=True)
    (paths.config_dir() / "bad_prompt.md").write_text("Work on {ticket_id} in {sprint}.")
    cfg = config.load()
    cfg.modes["bad"] = ModeSpec(template="bad_prompt.md")
    config.save(cfg)

    runner = CliRunner()
    res = runner.invoke(app, ["new", "--branch-name", "spike/foo", "--mode", "bad"])
    assert res.exit_code != 0
    assert res.exception is not None
    assert "references a slot gw doesn't fill" in str(res.exception)


def test_new_user_seed_mode_rejects_prompt(isolated_xdg: Path, tmp_path: Path) -> None:
    """The `--prompt` refusal is derived from the mode's shape, not hardcoded
    per mode — a user's own seed mode gets it for free."""
    from goblin_watcher import config
    from goblin_watcher.modes import ModeSpec

    repo = tmp_path / "alpha"
    _init_repo(repo)
    _register_project(repo)

    cfg = config.load()
    cfg.modes["shipit"] = ModeSpec(seed="/ship")
    config.save(cfg)

    runner = CliRunner()
    res = runner.invoke(
        app, ["new", "--branch-name", "spike/foo", "--mode", "shipit", "--prompt", "go"]
    )
    assert res.exit_code != 0
    assert res.exception is not None
    assert "--mode shipit and --prompt are mutually exclusive" in str(res.exception)


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


def test_new_pr_fork_fetches_pull_head(isolated_xdg: Path, tmp_path: Path) -> None:
    """A cross-repository (fork) PR is checked out via `refs/pull/<N>/head`
    instead of being refused."""
    from unittest.mock import patch

    from goblin_watcher.gh import PrInfo

    repo = _clone_with_pr_branch(tmp_path)
    # Simulate GitHub's PR ref on the upstream: point refs/pull/7/head at the
    # PR-work commit that only exists on the (would-be fork's) branch.
    upstream = tmp_path / "upstream"
    sha = subprocess.run(
        ["git", "-C", str(upstream), "rev-parse", "feat/pr-42"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    subprocess.run(["git", "-C", str(upstream), "update-ref", "refs/pull/7/head", sha], check=True)
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
    assert res.exit_code == 0, res.output

    proj = state.get_project("alpha")
    [task] = state.list_tasks(proj)
    assert task.branch == "feat/forked"
    assert task.pr_url == "https://github.com/org/repo/pull/7"
    assert (task.worktree_path / "pr-work.txt").exists()


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
    from goblin_watcher.cli import _rewrite_task_shortcut

    assert _rewrite_task_shortcut(["ENG-123"]) == ["new", "--linear", "ENG-123"]
    assert _rewrite_task_shortcut(["ENG-123", "--agent", "claude"]) == [
        "new",
        "--linear",
        "ENG-123",
        "--agent",
        "claude",
    ]
    assert _rewrite_task_shortcut(["--debug", "ENG-123"]) == [
        "--debug",
        "new",
        "--linear",
        "ENG-123",
    ]
    # Non-Linear positional is untouched.
    assert _rewrite_task_shortcut(["project", "ls"]) == ["project", "ls"]


def test_new_branch_name_task_id_collision_errors(isolated_xdg: Path, tmp_path: Path) -> None:
    """Task ids are branch slugs truncated to 60 chars — two distinct branch
    names can map to one id, and the second must not silently overwrite the
    first task's record."""
    repo = tmp_path / "alpha"
    _init_repo(repo)
    _register_project(repo)
    runner = CliRunner()

    res = runner.invoke(
        app, ["new", "--branch-name", "x" * 61, "--project", "alpha", "--no-launch"]
    )
    assert res.exit_code == 0, res.output
    [task] = state.list_tasks(state.get_project("alpha"))
    assert task.id == "x" * 60

    res = runner.invoke(
        app, ["new", "--branch-name", "x" * 60 + "y", "--project", "alpha", "--no-launch"]
    )
    assert res.exit_code != 0
    assert res.exception is not None
    assert "already exists" in str(res.exception)
    # The original record survived.
    [task] = state.list_tasks(state.get_project("alpha"))
    assert task.branch == "x" * 61


def test_new_branch_name_rm_replaces_existing(isolated_xdg: Path, tmp_path: Path) -> None:
    """`--rm` removes the colliding task (branch + worktree + record) and recreates
    it under the same branch name instead of bumping to -2."""
    repo = tmp_path / "alpha"
    _init_repo(repo)
    _register_project(repo)
    runner = CliRunner()

    res = runner.invoke(app, ["new", "--branch-name", "spike/foo", "--no-launch"])
    assert res.exit_code == 0, res.output

    # Without --rm, re-running errors and points at --rm.
    res = runner.invoke(app, ["new", "--branch-name", "spike/foo", "--no-launch"])
    # A same-name re-run bumps to spike/foo-2 (sibling); the id collision guard
    # only fires on truncation clashes, so this succeeds — assert two tasks.
    assert res.exit_code == 0, res.output
    assert len(state.list_tasks(state.get_project("alpha"))) == 2

    # --rm collapses back: the spike/foo task is replaced, not duplicated.
    res = runner.invoke(app, ["new", "--branch-name", "spike/foo", "--rm", "--no-launch"])
    assert res.exit_code == 0, res.output
    proj = state.get_project("alpha")
    branches = sorted(t.branch for t in state.list_tasks(proj))
    assert branches == ["spike/foo", "spike/foo-2"]
    [foo] = [t for t in state.list_tasks(proj) if t.branch == "spike/foo"]
    assert foo.worktree_path.exists()


def test_new_rm_refuses_dirty_worktree(isolated_xdg: Path, tmp_path: Path) -> None:
    """`--rm` won't discard uncommitted work in the worktree it would delete."""
    repo = tmp_path / "alpha"
    _init_repo(repo)
    _register_project(repo)
    runner = CliRunner()

    res = runner.invoke(app, ["new", "--branch-name", "spike/foo", "--no-launch"])
    assert res.exit_code == 0, res.output
    [task] = state.list_tasks(state.get_project("alpha"))
    (task.worktree_path / "scratch.txt").write_text("work in progress")

    res = runner.invoke(app, ["new", "--branch-name", "spike/foo", "--rm", "--no-launch"])
    assert res.exit_code != 0
    assert "uncommitted changes" in str(res.exception)
    # The original task and its work survived.
    [task] = state.list_tasks(state.get_project("alpha"))
    assert task.branch == "spike/foo"
    assert (task.worktree_path / "scratch.txt").exists()


def test_new_rm_force_discards_dirty_worktree(isolated_xdg: Path, tmp_path: Path) -> None:
    """`--rm-force` removes the colliding task even with uncommitted work, then
    recreates a clean worktree."""
    repo = tmp_path / "alpha"
    _init_repo(repo)
    _register_project(repo)
    runner = CliRunner()

    res = runner.invoke(app, ["new", "--branch-name", "spike/foo", "--no-launch"])
    assert res.exit_code == 0, res.output
    [task] = state.list_tasks(state.get_project("alpha"))
    (task.worktree_path / "scratch.txt").write_text("work in progress")

    res = runner.invoke(app, ["new", "--branch-name", "spike/foo", "--rm-force", "--no-launch"])
    assert res.exit_code == 0, res.output
    proj = state.get_project("alpha")
    [task] = state.list_tasks(proj)
    assert task.branch == "spike/foo"
    assert task.worktree_path.exists()
    # The dirty file was discarded with the old worktree.
    assert not (task.worktree_path / "scratch.txt").exists()


def test_new_existing_branch_rm_keeps_branch(isolated_xdg: Path, tmp_path: Path) -> None:
    """For an adopted branch, --rm clears the worktree + record but keeps the
    branch (gw didn't create it). The recreate would fail if the branch were gone."""
    repo = tmp_path / "alpha"
    _init_repo(repo)
    subprocess.run(["git", "-C", str(repo), "branch", "feat/pre-existing"], check=True)
    _register_project(repo)
    runner = CliRunner()

    res = runner.invoke(app, ["new", "--branch", "feat/pre-existing", "--no-launch"])
    assert res.exit_code == 0, res.output

    res = runner.invoke(app, ["new", "--branch", "feat/pre-existing", "--rm", "--no-launch"])
    assert res.exit_code == 0, res.output
    proj = state.get_project("alpha")
    [task] = state.list_tasks(proj)
    assert task.branch == "feat/pre-existing"
    assert task.worktree_path.exists()
    assert git.branch_exists(repo, "feat/pre-existing")


def test_new_dir_rm_resets_record_only(isolated_xdg: Path, tmp_path: Path) -> None:
    """For an in-place --dir checkout, --rm resets only the record: the directory
    and its (possibly dirty) contents are left untouched."""
    repo = tmp_path / "alpha"
    _init_repo(repo)
    _register_project(repo)
    external = tmp_path / "external-checkout"
    subprocess.run(
        ["git", "-C", str(repo), "worktree", "add", str(external), "-b", "spike/external"],
        check=True,
    )
    runner = CliRunner()

    res = runner.invoke(app, ["new", "--dir", str(external), "--no-launch"])
    assert res.exit_code == 0, res.output
    # Uncommitted work in the adopted dir must not block --rm (we don't delete it).
    (external / "wip.txt").write_text("in progress")

    res = runner.invoke(app, ["new", "--dir", str(external), "--rm", "--no-launch"])
    assert res.exit_code == 0, res.output
    proj = state.get_project("alpha")
    [task] = state.list_tasks(proj)
    assert task.branch == "spike/external"
    assert external.exists()
    assert (external / "wip.txt").read_text() == "in progress"
    assert git.branch_exists(repo, "spike/external")


def test_linear_shortcut_dispatcher_is_case_insensitive() -> None:
    from goblin_watcher.cli import _rewrite_task_shortcut

    assert _rewrite_task_shortcut(["eng-123"]) == ["new", "--linear", "eng-123"]
    # Single-char team keys are valid Linear identifiers too.
    assert _rewrite_task_shortcut(["X-1"]) == ["new", "--linear", "X-1"]
    # Subcommands keep winning: no digits-suffix pattern, no rewrite.
    assert _rewrite_task_shortcut(["run", "eng-123"]) == ["run", "eng-123"]
    assert _rewrite_task_shortcut(["status"]) == ["status"]
