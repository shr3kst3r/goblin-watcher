"""Smoke tests for the static zsh completion generator."""

from goblin_watcher.commands.completion import _static_zsh_script


def test_static_zsh_has_compdef_on_line_one() -> None:
    script = _static_zsh_script()
    assert script.splitlines()[0] == "#compdef gw"


def test_static_zsh_lists_all_top_level_commands() -> None:
    script = _static_zsh_script()
    for cmd in (
        "new",
        "cd",
        "run",
        "status",
        "doctor",
        "completion",
        "version",
        "project",
        "task",
        "session",
        "pr",
    ):
        assert f"'{cmd}:" in script, f"missing command {cmd!r} in top-level commands"


def test_static_zsh_emits_per_command_functions() -> None:
    script = _static_zsh_script()
    for fn in (
        "_gw_new()",
        "_gw_run()",
        "_gw_task()",
        "_gw_task_ls()",
        "_gw_task_prune()",
        "_gw_session_prune()",
        "_gw_pr_open()",
    ):
        assert fn in script, f"missing function {fn!r}"


def test_static_zsh_new_includes_all_branch_flags() -> None:
    script = _static_zsh_script()
    for flag in ("--linear", "--branch", "--branch-name", "--branch-auto", "--dir", "--unsafe"):
        assert f"'{flag}[" in script, f"missing flag {flag!r} on `gw new`"


def test_static_zsh_dual_flags_render_both_sides() -> None:
    """`--unsafe/--no-unsafe` and `--refresh-prs/--no-refresh-prs` must both appear."""
    script = _static_zsh_script()
    assert "'--unsafe[" in script
    assert "'--no-unsafe[" in script
    assert "'--refresh-prs[" in script
    assert "'--no-refresh-prs[" in script


def test_static_zsh_compdef_footer() -> None:
    script = _static_zsh_script()
    assert "compdef _gw gw" in script


def test_static_zsh_hidden_commands_not_listed() -> None:
    """`_describe` and `__complete` are internal entry points; not user-facing."""
    script = _static_zsh_script()
    assert "'_describe:" not in script
    assert "'__complete:" not in script


def test_static_zsh_emits_helper_preamble() -> None:
    """The script must define the helper functions that call `gw __complete`."""
    script = _static_zsh_script()
    for fn in (
        "_gw_complete_projects()",
        "_gw_complete_tasks()",
        "_gw_complete_sessions()",
        "_gw_complete_tasks_or_files()",
    ):
        assert fn in script, f"missing helper {fn!r}"
    assert "command gw __complete projects" in script
    assert "command gw __complete tasks" in script
    assert "command gw __complete sessions" in script


def test_static_zsh_positional_task_id_completes_tasks() -> None:
    script = _static_zsh_script()
    assert "'1:task id:_gw_complete_tasks'" in script


def test_static_zsh_positional_session_id_completes_sessions() -> None:
    script = _static_zsh_script()
    assert "'1:session id:_gw_complete_sessions'" in script


def test_static_zsh_run_and_cd_target_completes_tasks_or_files() -> None:
    """`gw cd <TAB>` and `gw run <TAB>` should offer tasks and falls back to files."""
    script = _static_zsh_script()
    # Appears at least twice: once for cd, once for run.
    assert script.count("'1:target:_gw_complete_tasks_or_files'") >= 2


def test_static_zsh_project_option_completes_projects() -> None:
    """`--project <TAB>` everywhere should call the project enumerator."""
    script = _static_zsh_script()
    # Many commands have --project; the value completer should appear repeatedly.
    assert "PROJECT:_gw_complete_projects" in script


def test_static_zsh_with_project_option_completes_projects() -> None:
    """`--with-project <TAB>` should enumerate projects, same as `--project`."""
    script = _static_zsh_script()
    assert "WITH_PROJECT:_gw_complete_projects" in script


def test_static_zsh_with_project_is_repeatable() -> None:
    """`--with-project` takes multiple values; the `*` prefix keeps zsh offering it."""
    script = _static_zsh_script()
    assert "'*--with-project[" in script


def test_static_zsh_task_project_option_completes_projects() -> None:
    script = _static_zsh_script()
    assert "TASK_PROJECT:_gw_complete_projects" in script


def test_static_zsh_task_option_completes_tasks() -> None:
    script = _static_zsh_script()
    assert "TASK:_gw_complete_tasks" in script


def test_static_zsh_session_option_completes_sessions() -> None:
    script = _static_zsh_script()
    assert "SESSION:_gw_complete_sessions" in script


def test_static_zsh_project_new_name_does_not_complete_projects() -> None:
    """`gw project new <NAME>` is for a fresh name; don't suggest existing projects."""
    script = _static_zsh_script()
    # The positional under _gw_project_new() should be plain (no completer).
    assert "_gw_project_new()" in script
    new_block = script.split("_gw_project_new()", 1)[1].split("}", 1)[0]
    assert "_gw_complete_projects" not in new_block


def test_static_zsh_agent_flag_lists_choices() -> None:
    """`--agent` should advertise the known agent set via a static Choice list."""
    script = _static_zsh_script()
    assert "(claude codex gemini antigravity managed)" in script


def test_static_zsh_windowing_flag_lists_choices() -> None:
    script = _static_zsh_script()
    assert "(inline tmux headless)" in script
