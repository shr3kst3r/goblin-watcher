import subprocess
from pathlib import Path
from unittest.mock import patch

from typer.testing import CliRunner

from goblin_watcher import state
from goblin_watcher.cli import app
from goblin_watcher.errors import LinearAuthError
from goblin_watcher.models import LinearIssue


def _init_repo(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", "-b", "main", str(path)], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.name", "tester"], check=True)
    (path / "README.md").write_text("hi")
    subprocess.run(["git", "-C", str(path), "add", "."], check=True)
    subprocess.run(["git", "-C", str(path), "commit", "-qm", "init"], check=True)


def _bootstrap_with_linear(tmp_path: Path, *, cached_state: str = "In Progress") -> None:
    repo = tmp_path / "alpha"
    _init_repo(repo)
    runner = CliRunner()
    runner.invoke(app, ["project", "new", "alpha", "--dir", str(repo), "--team", "PLAT"])
    runner.invoke(app, ["new", "--branch-name", "plat-7-do-the-thing", "--no-launch"])
    proj = state.get_project("alpha")
    [task] = state.list_tasks(proj)
    task = task.model_copy(
        update={
            "linear": LinearIssue(
                id="uuid",
                identifier="PLAT-7",
                title="Do the thing",
                state=cached_state,
                team_key="PLAT",
                url="https://linear.app/x/issue/PLAT-7",
            )
        }
    )
    state.save_task(proj, task)


def test_status_renders_linear_state_when_present(isolated_xdg: Path, tmp_path: Path) -> None:
    _bootstrap_with_linear(tmp_path, cached_state="In Progress")
    runner = CliRunner()
    # No API key configured → falls back to cached state silently.
    with patch(
        "goblin_watcher.secrets.get_linear_api_key",
        side_effect=LinearAuthError("no key"),
    ):
        res = runner.invoke(app, ["status"])
    assert res.exit_code == 0, res.output
    assert "linear: In Progress" in res.output
    assert "Do the thing" in res.output


def test_status_renders_the_task_status(isolated_xdg: Path, tmp_path: Path) -> None:
    """Rich reads `[open]` as a markup tag and swallows it, so the status has to
    be rendered in parens or it never reaches the user at all."""
    repo = tmp_path / "alpha"
    _init_repo(repo)
    runner = CliRunner()
    runner.invoke(app, ["project", "new", "alpha", "--dir", str(repo)])
    runner.invoke(app, ["new", "--branch-name", "spike/foo", "--no-launch"])

    proj = state.get_project("alpha")
    [task] = state.list_tasks(proj)
    # A hyphenated status is the harder case: Rich parses `[pr-open]` too.
    state.save_task(proj, task.model_copy(update={"status": "pr-open"}))

    res = runner.invoke(app, ["status"])
    assert res.exit_code == 0, res.output
    assert "(pr-open)" in res.output


def test_status_omits_linear_state_when_no_linear(isolated_xdg: Path, tmp_path: Path) -> None:
    repo = tmp_path / "alpha"
    _init_repo(repo)
    runner = CliRunner()
    runner.invoke(app, ["project", "new", "alpha", "--dir", str(repo)])
    runner.invoke(app, ["new", "--branch-name", "spike/foo", "--title", "Foo", "--no-launch"])
    res = runner.invoke(app, ["status"])
    assert res.exit_code == 0, res.output
    assert "linear:" not in res.output


def test_status_live_fetch_updates_displayed_and_persisted_state(
    isolated_xdg: Path, tmp_path: Path
) -> None:
    _bootstrap_with_linear(tmp_path, cached_state="Todo")
    runner = CliRunner()

    with (
        patch(
            "goblin_watcher.secrets.get_linear_api_key",
            return_value="fake-key",
        ),
        patch(
            "goblin_watcher.linear.client.LinearClient.fetch_issue_state",
            return_value="Done",
        ),
    ):
        res = runner.invoke(app, ["status"])

    assert res.exit_code == 0, res.output
    assert "linear: Done" in res.output
    assert "linear: Todo" not in res.output

    proj = state.get_project("alpha")
    [persisted] = state.list_tasks(proj)
    assert persisted.linear is not None
    assert persisted.linear.state == "Done"


def test_status_adopts_orphan_claude_sessions(isolated_xdg: Path, tmp_path: Path) -> None:
    from goblin_watcher.agents.claude import ClaudeAgent

    repo = tmp_path / "alpha"
    _init_repo(repo)
    runner = CliRunner()
    runner.invoke(app, ["project", "new", "alpha", "--dir", str(repo)])
    runner.invoke(app, ["new", "--branch-name", "spike/foo", "--no-launch"])

    proj = state.get_project("alpha")
    [task] = state.list_tasks(proj)
    assert task.sessions == []

    encoded = ClaudeAgent._encode_cwd(task.worktree_path)
    proj_dir = isolated_xdg / "home" / ".claude" / "projects" / encoded
    proj_dir.mkdir(parents=True)
    (proj_dir / "abc-123.jsonl").write_text(
        '{"type":"user","message":{"content":"hello"}}\n'
        '{"type":"assistant","message":{"content":"hi back"}}\n'
    )

    res = runner.invoke(app, ["status"])
    assert res.exit_code == 0, res.output
    assert "no sessions yet" not in res.output

    [persisted] = state.list_tasks(proj)
    assert len(persisted.sessions) == 1
    assert persisted.sessions[0].session_id == "abc-123"
    assert persisted.sessions[0].agent == "claude"


def test_status_falls_back_to_cached_state_when_fetch_fails(
    isolated_xdg: Path, tmp_path: Path
) -> None:
    from goblin_watcher.errors import GoblinError

    _bootstrap_with_linear(tmp_path, cached_state="In Review")
    runner = CliRunner()

    with (
        patch(
            "goblin_watcher.secrets.get_linear_api_key",
            return_value="fake-key",
        ),
        patch(
            "goblin_watcher.linear.client.LinearClient.fetch_issue_state",
            side_effect=GoblinError("network down"),
        ),
    ):
        res = runner.invoke(app, ["status"])

    assert res.exit_code == 0, res.output
    assert "linear: In Review" in res.output

    proj = state.get_project("alpha")
    [persisted] = state.list_tasks(proj)
    assert persisted.linear is not None
    assert persisted.linear.state == "In Review"


def test_status_project_flag_filters_to_one_project(isolated_xdg: Path, tmp_path: Path) -> None:
    repo_a = tmp_path / "alpha"
    repo_b = tmp_path / "beta"
    _init_repo(repo_a)
    _init_repo(repo_b)
    runner = CliRunner()
    runner.invoke(app, ["project", "new", "alpha", "--dir", str(repo_a)])
    runner.invoke(app, ["project", "new", "beta", "--dir", str(repo_b)])
    runner.invoke(app, ["new", "--branch-name", "spike/foo", "--project", "alpha", "--no-launch"])
    runner.invoke(app, ["new", "--branch-name", "spike/bar", "--project", "beta", "--no-launch"])

    res = runner.invoke(app, ["status", "--project", "alpha"])
    assert res.exit_code == 0, res.output
    assert "alpha" in res.output
    assert "beta" not in res.output


def test_status_project_flag_unknown_errors(isolated_xdg: Path, tmp_path: Path) -> None:
    repo = tmp_path / "alpha"
    _init_repo(repo)
    runner = CliRunner()
    runner.invoke(app, ["project", "new", "alpha", "--dir", str(repo)])

    res = runner.invoke(app, ["status", "--project", "nope"])
    assert res.exit_code != 0


def _add_commit(worktree: Path, filename: str = "extra.txt") -> None:
    """Add and commit a new file inside `worktree` (assumes git identity is inherited)."""
    (worktree / filename).write_text("more")
    subprocess.run(["git", "-C", str(worktree), "add", filename], check=True)
    subprocess.run(["git", "-C", str(worktree), "commit", "-qm", "more"], check=True)


def _bare_remote(path: Path) -> Path:
    """Create a bare repo at `path` seeded with a single initial commit on `main`."""
    seed = path.parent / (path.name + ".seed")
    _init_repo(seed)
    subprocess.run(
        ["git", "clone", "--quiet", "--bare", str(seed), str(path)],
        check=True,
        capture_output=True,
    )
    return path


def test_status_flags_uncommitted_changes(isolated_xdg: Path, tmp_path: Path) -> None:
    repo = tmp_path / "alpha"
    _init_repo(repo)
    runner = CliRunner()
    runner.invoke(app, ["project", "new", "alpha", "--dir", str(repo)])
    runner.invoke(app, ["new", "--branch-name", "spike/foo", "--no-launch"])

    proj = state.get_project("alpha")
    [task] = state.list_tasks(proj)
    (task.worktree_path / "dirty.txt").write_text("wip")

    res = runner.invoke(app, ["status"])
    assert res.exit_code == 0, res.output
    assert "uncommitted" in res.output


def test_status_flags_unmerged_commits_for_local_only_project(
    isolated_xdg: Path, tmp_path: Path
) -> None:
    repo = tmp_path / "alpha"
    _init_repo(repo)
    runner = CliRunner()
    runner.invoke(app, ["project", "new", "alpha", "--dir", str(repo)])
    runner.invoke(app, ["new", "--branch-name", "spike/foo", "--no-launch"])

    proj = state.get_project("alpha")
    assert proj.repo_url is None
    [task] = state.list_tasks(proj)
    _add_commit(task.worktree_path)

    res = runner.invoke(app, ["status"])
    assert res.exit_code == 0, res.output
    assert "1 unmerged" in res.output
    assert "unpushed" not in res.output


def test_status_flags_unpushed_commits_when_project_has_remote(
    isolated_xdg: Path, tmp_path: Path
) -> None:
    bare = _bare_remote(tmp_path / "remote.git")
    runner = CliRunner()
    res_new = runner.invoke(app, ["project", "new", "alpha", "--repo", str(bare)])
    assert res_new.exit_code == 0, res_new.output
    runner.invoke(app, ["new", "--branch-name", "spike/foo", "--no-launch"])

    proj = state.get_project("alpha")
    assert proj.repo_url is not None
    [task] = state.list_tasks(proj)
    # Inherit committer identity from the clone (none configured by default).
    subprocess.run(
        ["git", "-C", str(task.worktree_path), "config", "user.email", "t@t"], check=True
    )
    subprocess.run(
        ["git", "-C", str(task.worktree_path), "config", "user.name", "tester"], check=True
    )
    _add_commit(task.worktree_path)

    res = runner.invoke(app, ["status"])
    assert res.exit_code == 0, res.output
    assert "1 unpushed" in res.output
    assert "unmerged" not in res.output


def test_status_omits_sync_indicators_when_clean(isolated_xdg: Path, tmp_path: Path) -> None:
    repo = tmp_path / "alpha"
    _init_repo(repo)
    runner = CliRunner()
    runner.invoke(app, ["project", "new", "alpha", "--dir", str(repo)])
    runner.invoke(app, ["new", "--branch-name", "spike/foo", "--no-launch"])

    res = runner.invoke(app, ["status"])
    assert res.exit_code == 0, res.output
    assert "uncommitted" not in res.output
    assert "unpushed" not in res.output
    assert "unmerged" not in res.output


def test_status_no_linear_skips_fetch_but_shows_cached_state(
    isolated_xdg: Path, tmp_path: Path
) -> None:
    _bootstrap_with_linear(tmp_path, cached_state="Todo")
    runner = CliRunner()
    with (
        patch("goblin_watcher.secrets.get_linear_api_key", return_value="fake-key"),
        patch(
            "goblin_watcher.linear.client.LinearClient.fetch_issue_state",
            return_value="Done",
        ) as fetch,
    ):
        res = runner.invoke(app, ["status", "--no-linear"])
    assert res.exit_code == 0, res.output
    fetch.assert_not_called()
    assert "linear: Todo" in res.output


def test_status_linear_ttl_skips_fresh_cache(isolated_xdg: Path, tmp_path: Path) -> None:
    from datetime import UTC, datetime

    _bootstrap_with_linear(tmp_path, cached_state="Todo")
    proj = state.get_project("alpha")
    [task] = state.list_tasks(proj)
    state.save_task(proj, task.model_copy(update={"linear_state_updated_at": datetime.now(UTC)}))

    runner = CliRunner()
    with (
        patch("goblin_watcher.secrets.get_linear_api_key", return_value="fake-key"),
        patch(
            "goblin_watcher.linear.client.LinearClient.fetch_issue_state",
            return_value="Done",
        ) as fetch,
    ):
        res = runner.invoke(app, ["status"])
    assert res.exit_code == 0, res.output
    fetch.assert_not_called()
    assert "linear: Todo" in res.output


def test_status_linear_ttl_expired_refetches_and_stamps(isolated_xdg: Path, tmp_path: Path) -> None:
    from datetime import UTC, datetime, timedelta

    _bootstrap_with_linear(tmp_path, cached_state="Todo")
    proj = state.get_project("alpha")
    [task] = state.list_tasks(proj)
    stale = datetime.now(UTC) - timedelta(hours=2)
    state.save_task(proj, task.model_copy(update={"linear_state_updated_at": stale}))

    runner = CliRunner()
    with (
        patch("goblin_watcher.secrets.get_linear_api_key", return_value="fake-key"),
        patch(
            "goblin_watcher.linear.client.LinearClient.fetch_issue_state",
            return_value="Done",
        ),
    ):
        res = runner.invoke(app, ["status"])
    assert res.exit_code == 0, res.output
    assert "linear: Done" in res.output
    [persisted] = state.list_tasks(state.get_project("alpha"))
    assert persisted.linear_state_updated_at is not None
    assert persisted.linear_state_updated_at > stale


def test_status_shows_active_badge_for_fresh_transcript(isolated_xdg: Path, tmp_path: Path) -> None:
    from datetime import UTC, datetime

    from goblin_watcher.models import SessionRecord

    repo = tmp_path / "alpha"
    _init_repo(repo)
    runner = CliRunner()
    runner.invoke(app, ["project", "new", "alpha", "--dir", str(repo)])
    runner.invoke(app, ["new", "--branch-name", "spike/foo", "--no-launch"])
    proj = state.get_project("alpha")
    [task] = state.list_tasks(proj)
    transcript = tmp_path / "t.jsonl"
    transcript.write_text("{}\n")  # fresh mtime = now
    now = datetime.now(UTC)
    record = SessionRecord(
        agent="claude",
        session_id="s1",
        created_at=now,
        last_used_at=now,
        summary="working away",
        summary_updated_at=now,
        transcript_path=transcript,
    )
    state.save_task(proj, task.model_copy(update={"sessions": [record]}))

    res = runner.invoke(app, ["status"])
    assert res.exit_code == 0, res.output
    assert "● active" in res.output


def test_status_shows_idle_for_old_transcript(isolated_xdg: Path, tmp_path: Path) -> None:
    import os
    from datetime import UTC, datetime

    from goblin_watcher.models import SessionRecord

    repo = tmp_path / "alpha"
    _init_repo(repo)
    runner = CliRunner()
    runner.invoke(app, ["project", "new", "alpha", "--dir", str(repo)])
    runner.invoke(app, ["new", "--branch-name", "spike/foo", "--no-launch"])
    proj = state.get_project("alpha")
    [task] = state.list_tasks(proj)
    transcript = tmp_path / "t.jsonl"
    transcript.write_text("{}\n")
    hour_ago = datetime.now(UTC).timestamp() - 3600
    os.utime(transcript, (hour_ago, hour_ago))
    now = datetime.now(UTC)
    record = SessionRecord(
        agent="claude",
        session_id="s1",
        created_at=now,
        last_used_at=now,
        summary="paused",
        summary_updated_at=now,
        transcript_path=transcript,
    )
    state.save_task(proj, task.model_copy(update={"sessions": [record]}))

    res = runner.invoke(app, ["status"])
    assert res.exit_code == 0, res.output
    assert "● active" not in res.output
    assert "idle" in res.output


def _bootstrap_task_with_usage(tmp_path: Path, usage: list) -> None:
    """A project with one task carrying one claude session with `usage`."""
    from datetime import UTC, datetime

    from goblin_watcher.models import SessionRecord

    repo = tmp_path / "alpha"
    _init_repo(repo)
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
        usage=usage,
    )
    state.save_task(proj, task.model_copy(update={"sessions": [record]}))


def test_status_cost_rolls_up_session_task_and_project(isolated_xdg: Path, tmp_path: Path) -> None:
    from goblin_watcher.models import UsageBucket

    _bootstrap_task_with_usage(
        tmp_path,
        [UsageBucket(model="claude-opus-5", input_tokens=1_000_000, output_tokens=1_000_000)],
    )
    runner = CliRunner(env={"COLUMNS": "400"})
    res = runner.invoke(app, ["status", "--cost", "--no-linear"])
    assert res.exit_code == 0, res.output
    # $5 input + $25 output, shown on the session, its task, its project, and the total.
    assert res.output.count("~$30.00") == 4
    assert "1.0M in · 1.0M out" in res.output
    assert "list prices" in res.output


def test_status_without_cost_stays_quiet_about_tokens(isolated_xdg: Path, tmp_path: Path) -> None:
    from goblin_watcher.models import UsageBucket

    _bootstrap_task_with_usage(
        tmp_path, [UsageBucket(model="claude-opus-5", output_tokens=1_000_000)]
    )
    runner = CliRunner()
    res = runner.invoke(app, ["status", "--no-linear"])
    assert res.exit_code == 0, res.output
    assert "$" not in res.output


def test_status_cost_says_so_when_nothing_recorded(isolated_xdg: Path, tmp_path: Path) -> None:
    _bootstrap_task_with_usage(tmp_path, [])
    runner = CliRunner()
    res = runner.invoke(app, ["status", "--cost", "--no-linear"])
    assert res.exit_code == 0, res.output
    assert "No token usage recorded" in res.output


def test_status_dims_an_archived_task(isolated_xdg: Path, tmp_path: Path) -> None:
    """An archived task (worktree dropped, record kept) reads as parked (gh-23)."""
    repo = tmp_path / "alpha"
    _init_repo(repo)
    runner = CliRunner()
    runner.invoke(app, ["project", "new", "alpha", "--dir", str(repo)])
    runner.invoke(app, ["new", "--branch-name", "spike/foo", "--no-launch"])
    proj = state.get_project("alpha")
    [task] = state.list_tasks(proj)
    assert runner.invoke(app, ["task", "archive", task.id]).exit_code == 0

    res = runner.invoke(app, ["status", "--no-linear"])
    assert res.exit_code == 0, res.output
    assert "(archived)" in res.output
    assert task.id in res.output
