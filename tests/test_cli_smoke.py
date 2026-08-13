import subprocess
import sys


def _run_gw(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "goblin_watcher", *args],
        capture_output=True,
        text=True,
        check=False,
    )


def test_help_exits_zero() -> None:
    res = _run_gw("--help")
    assert res.returncode == 0, res.stderr
    assert "goblin-watcher" in res.stdout


def test_version_prints_version() -> None:
    res = _run_gw("version")
    assert res.returncode == 0, res.stderr
    assert "goblin-watcher" in res.stdout


def test_subcommand_groups_appear_in_help() -> None:
    res = _run_gw("--help")
    out = res.stdout
    for cmd in (
        "project",
        "task",
        "session",
        "pr",
        "prompt",
        "history",
        "new",
        "run",
        "status",
        "diff",
        "doctor",
        "version",
    ):
        assert cmd in out, f"missing `{cmd}` in help output"


def test_help_unknown_command_exits_nonzero() -> None:
    res = _run_gw("definitely-not-a-command")
    assert res.returncode != 0


def test_no_args_shows_help_without_traceback() -> None:
    # `no_args_is_help` raises a NoArgsIsHelpError; with standalone_mode=False
    # it must be rendered as help, not dumped as a Python traceback.
    res = _run_gw()
    combined = res.stdout + res.stderr
    assert "Usage: gw" in combined
    assert "Traceback" not in combined
    assert "NoArgsIsHelpError" not in combined


def test_unknown_option_shows_error_without_traceback() -> None:
    res = _run_gw("--definitely-not-a-flag")
    combined = res.stdout + res.stderr
    assert res.returncode != 0
    assert "No such option" in combined
    assert "Traceback" not in combined
    assert "NoSuchOption" not in combined


def test_inject_session_pick_sentinel_eoa() -> None:
    from goblin_watcher.cli import _inject_session_pick_sentinel
    from goblin_watcher.picker import SESSION_PICK_SENTINEL

    assert _inject_session_pick_sentinel(["run", "--session"]) == [
        "run",
        "--session",
        SESSION_PICK_SENTINEL,
    ]


def test_inject_session_pick_sentinel_before_flag() -> None:
    from goblin_watcher.cli import _inject_session_pick_sentinel
    from goblin_watcher.picker import SESSION_PICK_SENTINEL

    assert _inject_session_pick_sentinel(["run", "--session", "--agent", "codex"]) == [
        "run",
        "--session",
        SESSION_PICK_SENTINEL,
        "--agent",
        "codex",
    ]


def test_inject_session_pick_sentinel_preserves_value() -> None:
    from goblin_watcher.cli import _inject_session_pick_sentinel

    assert _inject_session_pick_sentinel(["run", "--session", "abc123"]) == [
        "run",
        "--session",
        "abc123",
    ]


def test_inject_project_sentinel_eoa() -> None:
    from goblin_watcher.cli import _inject_project_sentinel
    from goblin_watcher.commands.prompt import PROJECT_PICK_SENTINEL

    assert _inject_project_sentinel(["prompt", "edit", "--project"]) == [
        "prompt",
        "edit",
        "--project",
        PROJECT_PICK_SENTINEL,
    ]


def test_inject_project_sentinel_before_flag() -> None:
    from goblin_watcher.cli import _inject_project_sentinel
    from goblin_watcher.commands.prompt import PROJECT_PICK_SENTINEL

    assert _inject_project_sentinel(["prompt", "clear", "--project", "--force"]) == [
        "prompt",
        "clear",
        "--project",
        PROJECT_PICK_SENTINEL,
        "--force",
    ]


def test_inject_project_sentinel_preserves_value() -> None:
    from goblin_watcher.cli import _inject_project_sentinel

    assert _inject_project_sentinel(["prompt", "set", "x", "--project", "alpha"]) == [
        "prompt",
        "set",
        "x",
        "--project",
        "alpha",
    ]


def test_inject_project_sentinel_skips_when_no_prompt_subcommand() -> None:
    from goblin_watcher.cli import _inject_project_sentinel

    # `run --project alpha` (a different subcommand's --project) is left untouched.
    assert _inject_project_sentinel(["run", "--project", "alpha"]) == [
        "run",
        "--project",
        "alpha",
    ]
