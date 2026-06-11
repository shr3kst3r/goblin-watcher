"""Emit shell completion scripts.

For zsh we generate a static `compdef`-style script by walking the Click
command tree. That way `gw <subcommand> <TAB>` suggests flags right away
(no leading `--` required), and there's no per-tab subprocess.

For bash and fish we fall back to Typer's dynamic `_GW_COMPLETE=source_<shell>`
machinery, invoked as a subprocess so `shellingham` doesn't have to detect the
shell (it fails under `uv run` wrappers).
"""

from __future__ import annotations

import os
import subprocess
import sys

import click
import typer

from goblin_watcher.errors import GoblinError

_SHELLS = ("zsh", "bash", "fish")


def completion(
    shell: str = typer.Argument(..., help=f"Target shell ({'/'.join(_SHELLS)})."),
    dynamic: bool = typer.Option(
        False,
        "--dynamic",
        help="Use Typer's dynamic completion (one subprocess per tab press). "
        "Default for bash/fish; opt-in for zsh.",
    ),
) -> None:
    """Print a shell-completion script for `gw` to stdout.

    Pipe the output into a file your shell sources at startup:

      gw completion zsh > ~/.zfunc/_gw     # then add ~/.zfunc to $fpath and `compinit`
      gw completion bash > ~/.gw-completion.bash   # then `source ~/.gw-completion.bash`
      gw completion fish > ~/.config/fish/completions/gw.fish
    """
    if shell not in _SHELLS:
        raise GoblinError(
            f"Unsupported shell {shell!r}.",
            hint=f"Pick one of: {', '.join(_SHELLS)}.",
        )
    if shell == "zsh" and not dynamic:
        sys.stdout.write(_static_zsh_script())
        return
    _emit_dynamic(shell)


def _emit_dynamic(shell: str) -> None:
    env = {**os.environ, "_GW_COMPLETE": f"source_{shell}"}
    res = subprocess.run(
        [sys.executable, "-m", "goblin_watcher"],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    if res.returncode != 0 or not res.stdout.strip():
        raise GoblinError(
            "Failed to generate completion script.",
            hint=(res.stderr or res.stdout).strip() or None,
        )
    sys.stdout.write(res.stdout.lstrip("\n"))


# ---------- static zsh generator ------------------------------------------------


# Helper zsh functions emitted once at the top of the generated script. The
# script calls `gw __complete <kind>` (a hidden subcommand) on tab to enumerate
# project / task / session ids from gw's state. One subprocess per tab press
# only — option flag completion stays static.
_HELPERS_PREAMBLE = """\
_gw_complete_projects() {
  local -a items
  items=( ${(f)"$(command gw __complete projects 2>/dev/null)"} )
  compadd -a items
}

_gw_complete_tasks() {
  local -a items
  items=( ${(f)"$(command gw __complete tasks 2>/dev/null)"} )
  compadd -a items
}

_gw_complete_sessions() {
  local -a items
  items=( ${(f)"$(command gw __complete sessions 2>/dev/null)"} )
  compadd -a items
}

_gw_complete_tasks_or_files() {
  _alternative \\
    'tasks:task:_gw_complete_tasks' \\
    'files:path:_files'
}
"""


def _static_zsh_script() -> str:
    from goblin_watcher.cli import app

    root = typer.main.get_command(app)
    assert isinstance(root, click.Group), "gw root must be a Click Group"

    # Hidden commands (`_describe`, `__complete`) are internal entry points;
    # surfacing them in completion would be misleading.
    visible = sorted(n for n in root.commands if not root.commands[n].hidden)

    lines: list[str] = ["#compdef gw", "", _HELPERS_PREAMBLE, "_gw() {", "  local state line", ""]

    # Top-level commands.
    lines.append("  local -a commands")
    lines.append("  commands=(")
    for name in visible:
        lines.append(f"    {_describe_entry(name, root.commands[name])}")
    lines.append("  )")
    lines.append("")

    # Top-level dispatch.
    lines += [
        "  _arguments -C \\",
        "    '1: :->command' \\",
        "    '*::arg:->args'",
        "",
        "  case $state in",
        "    command) _describe -t commands 'gw command' commands ;;",
        "    args)",
        "      case $words[1] in",
    ]
    for name in visible:
        lines.append(f"        {name}) {_fn_for(name)} ;;")
    lines += ["      esac", "      ;;", "  esac", "}", ""]

    # Per-command functions.
    for name in visible:
        lines += _emit_command(name, root.commands[name])
        lines.append("")

    lines.append("compdef _gw gw")
    lines.append("")
    return "\n".join(lines)


def _fn_for(*parts: str) -> str:
    """Function name from a command path. `_gw_task_ls`, `_gw_project_new`, etc."""
    return "_gw_" + "_".join(p.replace("-", "_") for p in parts)


def _emit_command(name: str, cmd: click.Command, path: tuple[str, ...] = ()) -> list[str]:
    full_path = (*path, name)
    fn = _fn_for(*full_path)
    out: list[str] = [f"{fn}() {{"]

    if isinstance(cmd, click.Group):
        visible = sorted(n for n in cmd.commands if not cmd.commands[n].hidden)
        out.append("  local state line")
        out.append("  local -a subcmds")
        out.append("  subcmds=(")
        for sub_name in visible:
            out.append(f"    {_describe_entry(sub_name, cmd.commands[sub_name])}")
        out.append("  )")
        out += [
            "  _arguments -C \\",
            "    '1: :->subcommand' \\",
            "    '*::arg:->subargs'",
            "  case $state in",
            f"    subcommand) _describe -t commands '{name} subcommand' subcmds ;;",
            "    subargs)",
            "      case $words[1] in",
        ]
        for sub_name in visible:
            out.append(f"        {sub_name}) {_fn_for(*full_path, sub_name)} ;;")
        out += ["      esac", "      ;;", "  esac", "}"]
        for sub_name in visible:
            out.append("")
            out += _emit_command(sub_name, cmd.commands[sub_name], path=full_path)
        return out

    # Leaf command. `_arguments` handles both listing flags (with `[help]`
    # descriptions and per-flag value completers from `_spec_for`) and
    # completing values after a value-taking flag. Raw `compadd` does not
    # reliably register matches when the leaf is reached through a nested
    # `_arguments '*::arg:->state'` dispatch chain, so it can't be used here.
    args = list(_iter_positional_specs(cmd, full_path)) + list(_iter_arg_specs(cmd))
    if not args:
        out += ["  return 0", "}"]
        return out
    out.append("  _arguments \\")
    for i, spec in enumerate(args):
        suffix = " \\" if i < len(args) - 1 else ""
        out.append(f"    {spec}{suffix}")
    out.append("}")
    return out


def _iter_arg_specs(cmd: click.Command):
    """Yield `_arguments` specs for each Click option on `cmd`.

    Includes secondary opts so dual flags like `--unsafe/--no-unsafe` both show up.
    """
    for param in cmd.params:
        if not isinstance(param, click.Option):
            continue
        for flag in list(param.opts) + list(getattr(param, "secondary_opts", []) or []):
            yield _spec_for(flag, param)


def _iter_positional_specs(cmd: click.Command, path: tuple[str, ...]):
    """Yield `_arguments` specs for each positional `click.Argument` on `cmd`.

    Positions are 1-indexed in zsh's `_arguments` spec language. The completer
    is picked from the argument's `name` (see `_positional_completer`).
    """
    pos = 0
    for param in cmd.params:
        if not isinstance(param, click.Argument):
            continue
        pos += 1
        completer = _positional_completer(param.name or "", path)
        label = (param.name or "value").replace("_", " ")
        if completer:
            yield f"'{pos}:{label}:{completer}'"
        else:
            yield f"'{pos}:{label}:'"


# Argument names → zsh completer function emitted in the helpers preamble.
_POSITIONAL_COMPLETERS: dict[str, str] = {
    "task_id": "_gw_complete_tasks",
    "session_id": "_gw_complete_sessions",
    "target": "_gw_complete_tasks_or_files",
}


def _positional_completer(name: str, path: tuple[str, ...]) -> str | None:
    """Pick a zsh completer for a positional arg by name + command path.

    `name` is special-cased: only complete project names when the command is
    under the `project` group (so `gw project new <NAME>` — a fresh project
    name — doesn't suggest existing projects).
    """
    if name in _POSITIONAL_COMPLETERS:
        return _POSITIONAL_COMPLETERS[name]
    if name == "name" and path and path[0] == "project" and path[-1] != "new":
        return "_gw_complete_projects"
    return None


def _spec_for(flag: str, param: click.Option) -> str:
    help_text = _zsh_escape(param.help or "")
    # `*` lets repeatable options (multiple=True) keep being offered after use.
    prefix = "*" if param.multiple else ""
    if param.is_flag or _is_count(param):
        return f"'{prefix}{flag}[{help_text}]'"
    placeholder = flag.lstrip("-").upper().replace("-", "_") or "VALUE"
    completer = _value_completer(param)
    if completer:
        return f"'{prefix}{flag}[{help_text}]:{placeholder}:{completer}'"
    return f"'{prefix}{flag}[{help_text}]:{placeholder}:'"


def _is_count(param: click.Option) -> bool:
    return getattr(param, "count", False)


# Long-flag names → zsh completer function emitted in the helpers preamble.
_OPTION_NAME_COMPLETERS: dict[str, str] = {
    "--project": "_gw_complete_projects",
    "--with-project": "_gw_complete_projects",
    "--task-project": "_gw_complete_projects",
    "--task": "_gw_complete_tasks",
    "--session": "_gw_complete_sessions",
}


def _value_completer(param: click.Option) -> str | None:
    """Return a zsh completion verb for parameters with a known value space.

    Order: Click `Choice` and `Path` types first (most precise), then a
    name-based fallback for our well-known flags (see `_OPTION_NAME_COMPLETERS`)
    that don't have a richer Click type.
    """
    t = param.type
    if isinstance(t, click.Choice):
        choices = " ".join(str(c) for c in t.choices)
        return f"({choices})"
    if isinstance(t, click.types.Path):
        if getattr(t, "dir_okay", True) and not getattr(t, "file_okay", True):
            return "_directories"
        return "_files"
    for flag in param.opts:
        if flag in _OPTION_NAME_COMPLETERS:
            return _OPTION_NAME_COMPLETERS[flag]
    return None


def _describe_entry(name: str, cmd: click.Command) -> str:
    """A `_describe` entry: `'name:short help'` with zsh-safe escaping."""
    return f"'{name}:{_zsh_escape(_short_help(cmd))}'"


def _short_help(cmd: click.Command) -> str:
    text = cmd.short_help or cmd.help or ""
    return text.strip().splitlines()[0] if text.strip() else ""


def _zsh_escape(text: str) -> str:
    """Escape characters that would break _describe/_arguments syntax."""
    if not text:
        return ""
    return (
        text.replace("\\", "\\\\")
        .replace("'", "'\\''")
        .replace("[", "\\[")
        .replace("]", "\\]")
        .replace(":", "\\:")
        .replace("\n", " ")
    )
