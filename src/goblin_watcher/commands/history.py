"""`gw history` — show and prune the command-invocation log."""

from __future__ import annotations

import json
from typing import Any

import typer
from rich.table import Table

from goblin_watcher import command_log
from goblin_watcher.console import console, print_success
from goblin_watcher.errors import GoblinError

app = typer.Typer()


@app.callback(invoke_without_command=True)
def history(
    ctx: typer.Context,
    tail: int = typer.Option(20, "--tail", "-n", help="Show the last N entries."),
    show_all: bool = typer.Option(False, "--all", help="Show every entry (overrides --tail)."),
    json_out: bool = typer.Option(False, "--json", help="Print raw JSON lines instead of a table."),
) -> None:
    """Show recent `gw` command invocations."""
    if ctx.invoked_subcommand is not None:
        return
    entries = command_log.read_entries()
    if not entries:
        console.print("[muted]No commands have been logged yet.[/]")
        return
    # `entries[-0:]` would be the whole list, so handle non-positive --tail
    # explicitly: it means "show nothing".
    if show_all:
        selected = entries
    elif tail <= 0:
        selected = []
    else:
        selected = entries[-tail:]
    if not selected:
        console.print("[muted]No entries selected (--tail 0).[/]")
        return
    if json_out:
        # Bypass Rich so long JSON lines aren't soft-wrapped at the console width.
        for entry in selected:
            print(json.dumps(entry, separators=(",", ":")))
        return
    console.print(_render_table(selected))


@app.command("prune")
def prune_cmd(
    days: int = typer.Option(..., "--days", help="Drop entries older than N days."),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Report what would be removed but don't write.",
    ),
) -> None:
    """Remove old entries from the command-invocation log."""
    if days < 0:
        raise GoblinError("--days must be non-negative.", exit_code=2)
    if not command_log.log_file().exists():
        console.print("[muted]No log file to prune.[/]")
        return
    if dry_run:
        removed, kept = command_log.count_old(days)
        console.print(f"Would remove {removed} of {removed + kept} entries older than {days} days.")
        return
    removed, kept = command_log.prune(days)
    print_success(f"Removed {removed} entries older than {days} days; {kept} remain.")


def _render_table(entries: list[dict[str, Any]]) -> Table:
    table = Table(show_header=True, header_style="bold", box=None)
    table.add_column("when")
    table.add_column("exit", justify="right")
    table.add_column("dur", justify="right")
    table.add_column("argv")
    for entry in entries:
        ts = entry.get("ts", "")
        when = ts.replace("T", " ")[:19] if isinstance(ts, str) else ""
        exit_code = entry.get("exit_code", 0)
        if isinstance(exit_code, int) and exit_code != 0:
            exit_str = f"[red]{exit_code}[/]"
        else:
            exit_str = str(exit_code)
        dur = entry.get("duration_ms", 0)
        dur_str = f"{dur}ms" if isinstance(dur, int) else ""
        argv = entry.get("argv", [])
        argv_str = " ".join(argv) if isinstance(argv, list) and argv else "[dim](no args)[/]"
        table.add_row(when, exit_str, dur_str, argv_str)
    return table
