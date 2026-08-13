# CLI Architecture

Current-state design of the `gw` command line: how Typer assembles the app, how the Linear shortcut is dispatched, and how errors propagate.

## Typer assembly

`cli.py` is the only place `Typer()` is instantiated and the only place sub-typers are registered. Three categories:

```python
app = typer.Typer(name="gw", no_args_is_help=True, rich_markup_mode="rich")

# Subcommand groups (each has its own Typer in commands/*.py):
app.add_typer(project_cmd.app, name="project")
app.add_typer(task_cmd.app,    name="task")
app.add_typer(session_cmd.app, name="session")
app.add_typer(pr_cmd.app,      name="pr")

# Top-level single commands:
app.command("new")(new_cmd.new)
app.command("run")(run_cmd.run)
app.command("status")(status_cmd.status)
app.command("doctor")(doctor_cmd.doctor)
app.command("version")(version_cmd.version)
```

Each `commands/<group>.py` exports `app = typer.Typer(...)` and registers its leaf commands locally. The root `cli.py` doesn't know about individual subcommand signatures — it just wires the groups.

## The ticket shortcuts: `gw <LINEAR-ID>` and `gw gh-<N>`

`gw ENG-123` is sugar over `gw new --linear ENG-123`, and `gw gh-42` over `gw new --issue 42`. Typer can't naturally dispatch a positional argument to a subcommand (every Typer command is a registered name), so we rewrite argv in `main()` before handing it to the app:

```python
_LINEAR_ID = re.compile(r"^[A-Za-z][A-Za-z0-9_]*-\d+$")
_GH_ISSUE_ID = re.compile(r"^gh-(?P<number>\d+)$", re.IGNORECASE)

def _rewrite_task_shortcut(argv):
    for i, arg in enumerate(argv):
        if arg.startswith("-"):       # skip global flags like --debug
            continue
        gh_issue = _GH_ISSUE_ID.match(arg)
        if gh_issue is not None:      # checked FIRST — `gh-42` also matches _LINEAR_ID
            return [*argv[:i], "new", "--issue", gh_issue["number"], *argv[i + 1 :]]
        if _LINEAR_ID.match(arg):
            return [*argv[:i], "new", "--linear", arg, *argv[i + 1 :]]
        return argv                   # first non-flag isn't a ticket id → no rewrite
    return argv
```

Four properties worth knowing:

1. **Only the first non-flag positional** is checked. `gw new ENG-123` (with `new` explicitly typed) is left alone.
2. **Global flags pass through.** `gw --debug ENG-123` becomes `gw --debug new --linear ENG-123`.
3. **Order matters.** `gh-42` matches both patterns, so the GitHub-issue check runs first. The cost is that a Linear team keyed literally `GH` loses the shorthand and needs `gw new --linear GH-42` — a deliberate tradeoff, flagged in a comment next to the pattern.
4. **Both patterns are case-insensitive but shape-strict.** `eng-123` and `GH-42` are rewritten; `ENG123` and `gh-42x` are not. False positives turning random arguments into ticket lookups would be worse than requiring the standard format.

No subcommand name can collide with either pattern: they're all bare lowercase words with no `-<digits>` suffix.

The rewriter is a pure function, tested directly in `tests/test_cli_new_sources.py::test_linear_shortcut_dispatcher_rewrites_argv` and `tests/test_cli_issue_flow.py::test_gh_shorthand_rewrites_to_issue_source`.

## Error propagation

Single root exception: `errors.GoblinError(message, hint=None, exit_code=1)`. Subclasses for common shapes (`ProjectNotFoundError`, `TaskNotFoundError`, `GitCommandError`, `LinearAuthError`, `MissingDependencyError`).

`main()` wraps the Typer app and handles three exit paths:

```python
def main():
    argv = _rewrite_task_shortcut(sys.argv[1:])
    try:
        app(args=argv, prog_name="gw", standalone_mode=False)
    except GoblinError as err:
        print_error(err.message, hint=err.hint)
        raise SystemExit(err.exit_code)
    except typer.Exit as err:
        raise SystemExit(err.exit_code)
    except KeyboardInterrupt:
        print_error("Interrupted.")
        raise SystemExit(130)
```

`standalone_mode=False` is important: it makes Typer surface exceptions instead of catching them itself, so our `except GoblinError` block sees them and renders consistently via the Rich console (`[error]Error[/]: msg` + `[hint]Hint[/]: hint`).

In test mode, `CliRunner.invoke(app, ...)` calls the Typer app directly — **not** `main()`. So tests inspecting error output have to look at `res.exception` (the raised `GoblinError`), not `res.output`. This was a real footgun during Phase 5 implementation; the design doc captures it so future agents don't re-stumble.

The `--debug` global flag (also via `GW_DEBUG=1` env var) installs `rich.traceback` so unexpected exceptions show a real traceback. Without it, unexpected exceptions just propagate — agents/users can re-run with `--debug` if they need detail.

## Console + colors

`console.py` exposes a Rich `Console` singleton with a project-specific theme:

- `error` → `bold red`
- `hint` → `yellow`
- `success` → `bold green`
- `muted` → `dim`
- `agent.claude` → `bold magenta`
- `agent.codex` → `bold cyan`
- `agent.gemini` → `bold green`
- `agent.antigravity` → `bold blue`

Every output goes through this console (or `err_console` for stderr). `print` is banned by convention; ruff doesn't enforce that yet, but it would be a one-line `flake8-print` add if drift creeps in.

The `agent_badge(name)` helper wraps an agent name in its theme token. Used by `gw status`, `gw task show`, `gw session ls` to make agents visually scannable.

## Command surface, one-glance

```
gw <LINEAR-ID> [options]               → sugar over `gw new --linear`
gw gh-<N> [options]                    → sugar over `gw new --issue`
gw new --linear|--issue|--pr|--branch|--branch-name|--branch-auto|--dir [options]
gw run [PATH|TASK-ID] [options]
gw status

gw project new|ls|info|rm
gw task ls|show|rm
gw session ls|show|refresh|rm
gw pr open|status|checks

gw doctor
gw version
```

`--help` works at every level: `gw --help`, `gw project --help`, `gw project new --help`. Typer generates the help text from docstrings + `typer.Option(help=...)` strings on parameters.

## Adding a new subcommand

The pattern, in order:

1. Add the implementation to `commands/<group>.py` (existing group) or create a new module.
2. Register it on the local `app: typer.Typer` (for grouped commands) or call `app.command(...)(fn)` for a top-level.
3. If creating a new group, wire it from `cli.py` with `app.add_typer(...)`.
4. Add a `--help`-text-driven test to confirm it shows up.
5. Update root `AGENTS.md`'s command surface section if the surface itself changed.
6. Update `README.md`'s command reference.

Resist:

- Adding a Click extension layer beneath Typer.
- Plugins / entry points for third-party commands. The surface is small enough to stay static.
- Async command bodies. The CLI is sync end-to-end; `httpx.Client` (sync) and `subprocess.run` are the I/O calls that matter.

## Code map

- `src/goblin_watcher/cli.py` — Typer assembly, `_rewrite_linear_shortcut`, `main()`.
- `src/goblin_watcher/console.py` — Rich console + theme + helpers.
- `src/goblin_watcher/errors.py` — `GoblinError` + subclasses.
- `src/goblin_watcher/commands/` — one module per subcommand or group.

## Tests

- `tests/test_cli_smoke.py` — `gw --help`, `gw version`, unknown-command non-zero exit. Uses `subprocess` so it covers `main()`'s error rendering.
- `tests/test_cli_*.py` — group-specific via `typer.testing.CliRunner`. Note these bypass `main()` and surface exceptions via `res.exception`.
- `tests/test_cli_new_sources.py::test_linear_shortcut_dispatcher_rewrites_argv` — pure-function unit test for the rewriter.
