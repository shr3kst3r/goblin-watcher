"""Keep README's commands-reference block honest about the actual CLI surface.

The block is hand-maintained and has drifted more than once (gh-13). These tests
don't police wording — only that every command exists in it and that the
commands whose flags it spells out spell out all of them.
"""

from __future__ import annotations

import re
from pathlib import Path

import click
import typer

from goblin_watcher.cli import app

README = Path(__file__).resolve().parents[1] / "README.md"

# Commands the reference block documents flag-by-flag. The rest are listed with a
# subcommand summary only, so their options are out of scope here.
FLAG_COMMANDS = ("new", "run", "scratch")


def _reference_block() -> str:
    match = re.search(
        r"## Commands reference\n+```text\n(.*?)^```", README.read_text(), re.DOTALL | re.MULTILINE
    )
    assert match, "README no longer has a '## Commands reference' text block"
    return match.group(1)


def _root_group() -> click.Group:
    group = typer.main.get_command(app)
    assert isinstance(group, click.Group)
    return group


def _visible_commands() -> dict[str, click.Command]:
    group = _root_group()
    ctx = click.Context(group)
    named = ((name, group.get_command(ctx, name)) for name in group.list_commands(ctx))
    return {name: cmd for name, cmd in named if cmd is not None and not cmd.hidden}


def _long_options(cmd: click.Command) -> list[str]:
    return [
        opt
        for param in cmd.params
        if isinstance(param, click.Option) and not param.hidden
        for opt in (*param.opts, *param.secondary_opts)
        if opt.startswith("--") and opt != "--help"
    ]


def test_every_command_is_in_the_reference_block() -> None:
    block = _reference_block()
    for name in _visible_commands():
        assert re.search(rf"^gw {re.escape(name)}\b", block, re.MULTILINE), (
            f"`gw {name}` is missing from the README commands-reference block"
        )


def test_flag_documented_commands_list_every_option() -> None:
    block = _reference_block()
    commands = _visible_commands()
    for name in FLAG_COMMANDS:
        cmd = commands[name]
        for opt in _long_options(cmd):
            assert re.search(rf"{re.escape(opt)}(?![\w-])", block), (
                f"`gw {name} {opt}` is missing from the README commands-reference block"
            )
