"""Completion hook implementing spg's contract for the `gw` command.

spg (https://github.com/shr3kst3r/spg) installs a single zsh dispatcher that
routes tab completion through `spg __complete cmd <name> ...`. Without a
`complete_hook` in `spg.toml`, that dispatcher knows nothing about gw's
Typer-driven subcommand tree and returns no candidates — so `gw <TAB>`
silently produces nothing under spg.

This module walks gw's Click command tree and emits candidates in spg's
hook protocol: one `value:description` line per candidate (the zsh
dispatcher feeds them into `_describe`).

Hook input contract (from `spg/src/spg/completion.py:_run_hook`):
    <hook-args>... <cursor-index> <word1> <word2> ...

where `cursor-index` is the 0-indexed position in the original zsh
`$words` array (words[0] = "gw"), and `word1..wordN` are `words[1:]`.
"""

from __future__ import annotations

import sys
from collections.abc import Callable

import click
import typer

from goblin_watcher.completion_enumerators import (
    enumerate_projects,
    enumerate_sessions,
    enumerate_tasks,
)

_FILES_SENTINEL = "__files__"
_DIRS_SENTINEL = "__directories__"

# Long-flag → enumerator. Mirrors `_OPTION_NAME_COMPLETERS` in
# `commands/completion.py` so spg-driven completion offers the same data as
# gw's native zsh script.
_OPTION_VALUE_ENUMERATORS: dict[str, Callable[[], list[str]]] = {
    "--project": enumerate_projects,
    "--task": enumerate_tasks,
    "--session": enumerate_sessions,
}

# Positional arg name → enumerator. Mirrors `_POSITIONAL_COMPLETERS`.
# `target` is the task-id-or-path positional on `gw cd` / `gw run`; under
# spg we drop the "_or_files" half (path completion doesn't compose with
# our `value:description` line protocol) and offer task ids only.
_POSITIONAL_ENUMERATORS: dict[str, Callable[[], list[str]]] = {
    "task_id": enumerate_tasks,
    "session_id": enumerate_sessions,
    "target": enumerate_tasks,
}


def run(
    index: int,
    words_after_cmd: list[str],
    subcommand: tuple[str, ...] = (),
) -> None:
    """Emit spg-formatted candidates for cursor at `index` in original words.

    `subcommand` lets shell wrappers like `gwcd` / `gwcode` (declared in
    `spg.toml`) borrow `gw cd`'s completion: passing `("cd",)` walks into
    that leaf so the positional offers task ids instead of top-level gw
    subcommands.
    """
    from goblin_watcher.cli import app as gw_app

    root = typer.main.get_command(gw_app)
    if not isinstance(root, click.Group):
        return

    start: click.Command = root
    for part in subcommand:
        if not isinstance(start, click.Group):
            return
        nxt = start.commands.get(part)
        if nxt is None:
            return
        start = nxt

    tab_idx = max(0, index - 1)
    current, consumed_positionals = _walk(start, words_after_cmd, tab_idx)

    cur_word = words_after_cmd[tab_idx] if tab_idx < len(words_after_cmd) else ""
    prev_word = (
        words_after_cmd[tab_idx - 1] if tab_idx > 0 and tab_idx - 1 < len(words_after_cmd) else ""
    )

    # Case A: previous word is a value-taking flag → complete its values.
    if prev_word.startswith("-"):
        opt = _find_option(current, prev_word)
        if opt is not None and not _is_flag_only(opt):
            _emit_option_values(opt)
            return

    # Case B: user is typing a flag prefix → emit available flags.
    if cur_word.startswith("-"):
        _emit_flags(current)
        return

    # Case C: still on a Group → emit subcommands.
    if isinstance(current, click.Group):
        _emit_subcommands(current)
        return

    # Case D: leaf Command — positional candidates first, then flags as
    # additional matches. zsh's `_describe` filters by the user's prefix, so
    # `gw task rm <TAB>` offers both task ids and `--force`.
    _emit_positional(current, consumed_positionals)
    _emit_flags(current)


def _walk(
    root: click.Command,
    words_after_cmd: list[str],
    tab_idx: int,
) -> tuple[click.Command, int]:
    """Walk the Click tree using words before the cursor.

    Returns (current_command, positionals_consumed_at_current_level).
    `root` may be a leaf (e.g. when `run()` was invoked with a `subcommand`
    starting point) — the descent branch is skipped in that case.
    """
    current: click.Command = root
    consumed_positionals = 0
    i = 0
    while i < tab_idx and i < len(words_after_cmd):
        word = words_after_cmd[i]
        if (
            isinstance(current, click.Group)
            and not word.startswith("-")
            and word in current.commands
        ):
            current = current.commands[word]
            consumed_positionals = 0
            i += 1
            continue
        if word.startswith("-"):
            # `--flag=value` is one token; the value is already attached.
            if "=" in word:
                i += 1
                continue
            opt = _find_option(current, word)
            if opt is not None and not _is_flag_only(opt):
                i += 2  # consume flag and its separate value
                continue
            i += 1
            continue
        consumed_positionals += 1
        i += 1
    return current, consumed_positionals


def _find_option(cmd: click.Command, flag: str) -> click.Option | None:
    if not hasattr(cmd, "params"):
        return None
    for p in cmd.params:
        if isinstance(p, click.Option):
            opts = list(p.opts) + list(getattr(p, "secondary_opts", []) or [])
            if flag in opts:
                return p
    return None


def _is_flag_only(opt: click.Option) -> bool:
    return bool(opt.is_flag) or bool(getattr(opt, "count", False))


def _emit_subcommands(group: click.Group) -> None:
    for name in sorted(group.commands):
        cmd = group.commands[name]
        if cmd.hidden:
            continue
        _emit(name, _short(cmd.short_help or cmd.help or ""))


def _emit_flags(cmd: click.Command) -> None:
    for param in cmd.params:
        if not isinstance(param, click.Option):
            continue
        help_text = _short(param.help or "")
        for flag in list(param.opts) + list(getattr(param, "secondary_opts", []) or []):
            _emit(flag, help_text)


def _emit_option_values(opt: click.Option) -> None:
    if isinstance(opt.type, click.Choice):
        for c in opt.type.choices:
            _emit(str(c), "")
        return
    if isinstance(opt.type, click.types.Path):
        _emit_path_sentinel(opt.type)
        return
    for flag in opt.opts:
        if flag in _OPTION_VALUE_ENUMERATORS:
            for v in _OPTION_VALUE_ENUMERATORS[flag]():
                _emit(v, "")
            return


def _emit_positional(cmd: click.Command, slot: int) -> None:
    args = [p for p in cmd.params if isinstance(p, click.Argument)]
    if slot >= len(args):
        return
    arg = args[slot]
    if isinstance(arg.type, click.Choice):
        for c in arg.type.choices:
            _emit(str(c), "")
        return
    if isinstance(arg.type, click.types.Path):
        _emit_path_sentinel(arg.type)
        return
    name = arg.name or ""
    if name in _POSITIONAL_ENUMERATORS:
        for v in _POSITIONAL_ENUMERATORS[name]():
            _emit(v, "")


def _emit_path_sentinel(t: click.types.Path) -> None:
    if getattr(t, "dir_okay", True) and not getattr(t, "file_okay", True):
        sys.stdout.write(_DIRS_SENTINEL + "\n")
    else:
        sys.stdout.write(_FILES_SENTINEL + "\n")


def _emit(value: str, description: str) -> None:
    # spg's zsh dispatcher uses ":" as the value/description separator, so
    # any ":" in the description would corrupt the line. Replace it.
    if description:
        sys.stdout.write(f"{value}:{_clean(description)}\n")
    else:
        sys.stdout.write(f"{value}\n")


def _clean(text: str) -> str:
    return text.replace(":", " —").replace("\n", " ").strip()


def _short(text: str) -> str:
    text = text.strip()
    return text.splitlines()[0] if text else ""
