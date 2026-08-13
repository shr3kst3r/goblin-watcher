import sys

import click
import typer
from rich.traceback import install as install_rich_traceback
from typer import rich_utils

from goblin_watcher import command_log
from goblin_watcher.commands import (
    _complete as complete_cmd,
)
from goblin_watcher.commands import (
    cd as cd_cmd,
)
from goblin_watcher.commands import (
    completion as completion_cmd,
)
from goblin_watcher.commands import (
    config_cmd,
)
from goblin_watcher.commands import (
    describe as describe_cmd,
)
from goblin_watcher.commands import (
    diff as diff_cmd,
)
from goblin_watcher.commands import (
    doctor as doctor_cmd,
)
from goblin_watcher.commands import (
    history as history_cmd,
)
from goblin_watcher.commands import (
    new as new_cmd,
)
from goblin_watcher.commands import (
    pr as pr_cmd,
)
from goblin_watcher.commands import (
    project as project_cmd,
)
from goblin_watcher.commands import (
    prompt as prompt_cmd,
)
from goblin_watcher.commands import (
    run as run_cmd,
)
from goblin_watcher.commands import (
    scratch as scratch_cmd,
)
from goblin_watcher.commands import (
    session as session_cmd,
)
from goblin_watcher.commands import (
    status as status_cmd,
)
from goblin_watcher.commands import (
    sync as sync_cmd,
)
from goblin_watcher.commands import (
    task as task_cmd,
)
from goblin_watcher.commands import (
    version as version_cmd,
)
from goblin_watcher.commands.prompt import PROJECT_PICK_SENTINEL
from goblin_watcher.console import print_error
from goblin_watcher.errors import GoblinError
from goblin_watcher.picker import SESSION_PICK_SENTINEL

app = typer.Typer(
    name="gw",
    help="goblin-watcher: parallel AI coding agents in git worktrees.",
    no_args_is_help=True,
    rich_markup_mode="rich",
    # Enables `gw --install-completion` and `gw --show-completion` (bash/zsh/fish/powershell).
    add_completion=True,
)

app.add_typer(project_cmd.app, name="project", help="Manage projects (repos under gw's control).")
app.add_typer(task_cmd.app, name="task", help="Inspect and manage tasks.")
app.add_typer(session_cmd.app, name="session", help="Inspect and manage agent sessions.")
app.add_typer(pr_cmd.app, name="pr", help="Open and inspect GitHub PRs for tasks.")
app.add_typer(
    prompt_cmd.app,
    name="prompt",
    help="Manage the user-configured addition appended to fresh-spawn prompts.",
)
app.add_typer(history_cmd.app, name="history", help="Show and prune the gw command log.")
app.add_typer(
    sync_cmd.app,
    name="sync",
    help="Refresh tasks in the background: run a pass, watch it, check or install the schedule.",
)
app.add_typer(
    config_cmd.app,
    name="config",
    help="Inspect and edit the gw config file (show/get/set/unset/edit/path).",
)

app.command(
    "new",
    help="Create a task from a Linear ticket, GitHub issue or PR, branch, new branch, or dir.",
)(new_cmd.new)
app.command(
    "cd",
    help="Print a task's worktree path (workspace for multi-repo tasks). "
    "Pair with the `gwcd` shell function (see `gw completion`).",
)(cd_cmd.cd)
app.command("run", help="Pick a session for an existing task and spawn the agent.")(run_cmd.run)
app.command(
    "scratch",
    help="Create a scratch space: a plain directory not associated with any project.",
)(scratch_cmd.scratch)
app.command("status", help="Tree view of projects, tasks, and sessions.")(status_cmd.status)
app.command("diff", help="Show what a task's branch changed: commits, diffstat, and patch.")(
    diff_cmd.diff
)
app.command("doctor", help="Check dependencies and configuration.")(doctor_cmd.doctor)
app.command("completion", help="Print a shell completion script (zsh/bash/fish).")(
    completion_cmd.completion
)
app.command("version", help="Print version information.")(version_cmd.version)
# Internal entry point spawned by description.schedule_if_stale. Hidden from
# `gw --help`; not part of the user-facing CLI contract.
app.command("_describe", hidden=True)(describe_cmd.describe)
# Hidden enumerator group called from the static zsh completion script.
app.add_typer(complete_cmd.app, name="__complete", hidden=True)


@app.callback()
def _root(debug: bool = typer.Option(False, "--debug", envvar="GW_DEBUG")) -> None:
    if debug:
        install_rich_traceback(show_locals=False)


_re = __import__("re")

# Case-insensitive and 1+ char team key, matching `linear.parse_identifier` —
# `gw eng-123` should work the same as `gw ENG-123`. No subcommand name can
# collide: they're all bare lowercase words with no `-<digits>` suffix.
_LINEAR_ID = _re.compile(r"^[A-Za-z][A-Za-z0-9_]*-\d+$")
# `gh-42` is a GitHub issue in the *current* repo, and is matched before
# `_LINEAR_ID` (which would otherwise swallow it and hand `gh-42` to the Linear
# API). Deliberate tradeoff: a Linear team whose key is literally `GH` can't use
# the shorthand and must go through `gw new --linear GH-42`. Cross-repo issues
# have no shorthand — use `gw new --issue owner/repo#42`.
_GH_ISSUE_ID = _re.compile(r"^gh-(?P<number>\d+)$", _re.IGNORECASE)


def _rewrite_task_shortcut(argv: list[str]) -> list[str]:
    """`gw ENG-123 ...` → `gw new --linear ENG-123 ...`; `gw gh-42 ...` → `--issue 42`.

    Pattern-match the first non-flag positional. Leave the rest of argv intact.
    """
    for i, arg in enumerate(argv):
        if arg.startswith("-"):
            continue
        gh_issue = _GH_ISSUE_ID.match(arg)
        if gh_issue is not None:
            return [*argv[:i], "new", "--issue", gh_issue.group("number"), *argv[i + 1 :]]
        if _LINEAR_ID.match(arg):
            return [*argv[:i], "new", "--linear", arg, *argv[i + 1 :]]
        return argv
    return argv


def _inject_session_pick_sentinel(argv: list[str]) -> list[str]:
    """Allow `--session` with no following value to trigger the session picker.

    Typer/Click can't natively model "flag with optional value". When `--session`
    is followed by another flag or end-of-args, we splice in a sentinel string
    that the command handler recognizes as a request for the picker. The
    `--session=ID` form is left alone (Click parses it as one token).
    """
    out: list[str] = []
    i = 0
    while i < len(argv):
        arg = argv[i]
        out.append(arg)
        if arg == "--session":
            nxt = argv[i + 1] if i + 1 < len(argv) else None
            if nxt is None or nxt.startswith("-"):
                out.append(SESSION_PICK_SENTINEL)
        i += 1
    return out


def _inject_project_sentinel(argv: list[str]) -> list[str]:
    """Allow `gw prompt … --project` (no value) to open the project picker.

    Same idea as `_inject_session_pick_sentinel`. Only rewrites within the
    `prompt` subcommand so other commands' `--project` flags (if any) are
    unaffected.
    """
    try:
        scope = argv.index("prompt")
    except ValueError:
        return argv
    out: list[str] = list(argv[: scope + 1])
    i = scope + 1
    while i < len(argv):
        arg = argv[i]
        out.append(arg)
        if arg == "--project":
            nxt = argv[i + 1] if i + 1 < len(argv) else None
            if nxt is None or nxt.startswith("-"):
                out.append(PROJECT_PICK_SENTINEL)
        i += 1
    return out


def main() -> None:
    raw_argv = sys.argv[1:]
    argv = _rewrite_task_shortcut(raw_argv)
    argv = _inject_session_pick_sentinel(argv)
    argv = _inject_project_sentinel(argv)
    with command_log.record_invocation(raw_argv) as entry:
        try:
            app(args=argv, prog_name="gw", standalone_mode=False)
        except GoblinError as err:
            print_error(err.message, hint=err.hint)
            entry["exit_code"] = err.exit_code
            raise SystemExit(err.exit_code) from err
        except typer.Exit as err:
            entry["exit_code"] = err.exit_code
            raise SystemExit(err.exit_code) from err
        except click.ClickException as err:
            # `standalone_mode=False` makes Click/Typer re-raise usage errors
            # (bad options, and the `no_args_is_help` help text, which is a
            # NoArgsIsHelpError) instead of rendering them. Render them the way
            # Typer's standalone mode would, rather than dumping a traceback.
            rich_utils.rich_format_error(err)
            entry["exit_code"] = err.exit_code
            raise SystemExit(err.exit_code) from err
        except click.Abort:
            rich_utils.rich_abort_error()
            entry["exit_code"] = 1
            raise SystemExit(1) from None
        except KeyboardInterrupt:
            print_error("Interrupted.")
            entry["exit_code"] = 130
            raise SystemExit(130) from None
        except BaseException:
            entry["exit_code"] = 1
            raise
