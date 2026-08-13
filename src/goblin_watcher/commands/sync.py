"""`gw sync` — background synchronization (ADR 0005).

Bare `gw sync` runs one pass in the foreground and narrates it. `gw sync run` is
the quiet variant the scheduler invokes. `watch` follows the journal live,
`status` reports installation and component health, and `install`/`uninstall`
manage the launchd job.
"""

from __future__ import annotations

import shutil
import sys
from datetime import UTC, datetime

import typer
from rich.markup import escape
from rich.table import Table

from goblin_watcher import config, paths, secrets
from goblin_watcher.completion_enumerators import complete_projects
from goblin_watcher.console import console, print_success
from goblin_watcher.errors import GoblinError
from goblin_watcher.sync import engine, journal, launchd, store
from goblin_watcher.sync.notify import resolve as resolve_notifier

app = typer.Typer(no_args_is_help=False, invoke_without_command=True)

_LEVEL_STYLES = {
    "info": "muted",
    "action": "cyan",
    "notify": "bold yellow",
    "error": "red",
}


@app.callback(invoke_without_command=True)
def main(ctx: typer.Context) -> None:
    """Refresh tasks, cache indicators, prune, and notify."""
    if ctx.invoked_subcommand is None:
        _run_pass(verbose=True, project=None)


@app.command("run")
def run(
    project: str | None = typer.Option(
        None, "--project", help="Limit to one project.", autocompletion=complete_projects
    ),
    verbose: bool = typer.Option(
        False, "--verbose", help="Print each action as it happens (default for bare `gw sync`)."
    ),
) -> None:
    """Run one sync pass. Quiet by default — this is what the scheduler calls."""
    _run_pass(verbose=verbose, project=project)


def _run_pass(*, verbose: bool, project: str | None) -> None:
    def on_event(level: str, message: str) -> None:
        # Messages embed git/gh output; escape so brackets don't parse as markup.
        console.print(f"[{_LEVEL_STYLES.get(level, 'muted')}]{escape(message)}[/]")

    report = engine.run_pass(verbose=verbose, on_event=on_event, project_name=project)

    if report.status == "skipped":
        console.print("[muted]Another sync pass is already running; nothing to do.[/]")
        return
    if verbose:
        duration = report.duration_seconds or 0.0
        print_success(
            f"Sync {report.status}: {report.tasks} task(s) across "
            f"{report.projects} project(s) in {duration:.1f}s"
        )
        if report.notifications:
            console.print(f"[muted]Notifications: {len(report.notifications)}[/]")
            for n in report.notifications:
                console.print(f"  [bold yellow]{escape(n)}[/]")
        if report.actions:
            console.print(f"[muted]Actions: {len(report.actions)}[/]")
            for a in report.actions:
                console.print(f"  [cyan]{escape(a)}[/]")
        if report.pruned:
            console.print(f"[muted]Pruned: {escape(', '.join(report.pruned))}[/]")
    for err in report.errors:
        console.print(f"[red]{escape(err)}[/]")
    if report.status == "error":
        raise typer.Exit(code=1)


@app.command("watch")
def watch(
    lines: int = typer.Option(20, "--lines", "-n", help="Journal lines to show before following."),
) -> None:
    """Follow background sync activity live. Ctrl-C to stop."""
    for entry in journal.read_entries(limit=lines):
        _print_entry(entry)
    console.print("[muted]— following sync journal (Ctrl-C to stop) —[/]")
    try:
        for entry in journal.follow():
            _print_entry(entry)
    except KeyboardInterrupt:
        console.print("[muted]Stopped.[/]")


def _print_entry(entry: dict) -> None:
    """Render one journal record.

    Every dynamic field is escaped: journal details carry raw git/gh error text,
    and an unescaped `[` would be eaten as Rich markup (or corrupt the line).
    """
    level = str(entry.get("level", "info"))
    style = _LEVEL_STYLES.get(level, "muted")
    ts = escape(str(entry.get("ts", ""))[11:19])
    where = ""
    if entry.get("project") and entry.get("task"):
        where = f" · {escape(str(entry['project']))}/{escape(str(entry['task']))}"
    elif entry.get("project"):
        where = f" · {escape(str(entry['project']))}"
    detail = f" — {escape(str(entry['detail']))}" if entry.get("detail") else ""
    event = escape(str(entry.get("event", "?")))
    console.print(f"[muted]{ts}[/] [{style}]{event}{where}{detail}[/]")


@app.command("prune-journal")
def prune_journal(
    days: int = typer.Option(..., "--days", help="Drop journal records older than N days."),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Report what would be removed but don't write."
    ),
) -> None:
    """Trim the sync journal.

    A scheduled pass appends every few minutes forever; without this the journal
    is an unbounded file. Same shape as `gw history prune`.
    """
    if days < 0:
        raise GoblinError("--days must be non-negative.", exit_code=2)
    if not paths.sync_journal_file().exists():
        console.print("[muted]No sync journal to prune.[/]")
        return
    if dry_run:
        removed, kept = journal.count_old(days)
        console.print(f"Would remove {removed} of {removed + kept} records older than {days} days.")
        return
    removed, kept = journal.prune(days)
    print_success(f"Removed {removed} records older than {days} days; {kept} remain.")


@app.command("status")
def status() -> None:
    """Show whether sync is installed, when it last ran, and what it needs."""
    cfg = config.load()
    st = store.load_state()
    rows: list[tuple[str, bool, str]] = []

    # `gw sync install --interval N` writes the plist without touching config,
    # so the scheduled interval is whatever the plist says — not the config key.
    scheduled_interval: int | None = None
    if launchd.is_supported():
        plist = launchd.plist_path()
        if plist.exists():
            loaded = launchd.is_loaded()
            scheduled_interval = launchd.installed_interval()
            every = (
                f"every {scheduled_interval}s"
                if scheduled_interval is not None
                else "interval unreadable"
            )
            rows.append(
                (
                    "schedule",
                    loaded,
                    f"launchd job {'loaded' if loaded else 'installed but NOT loaded'} "
                    f"· {every} · {plist}",
                )
            )
        else:
            rows.append(("schedule", True, "not installed — run `gw sync install`"))
    else:
        rows.append(
            (
                "schedule",
                True,
                f"launchd unavailable on {sys.platform}; add cron: "
                f"{launchd.crontab_line(cfg.sync.interval_seconds)}",
            )
        )

    last = st.last_pass
    if last is None:
        rows.append(("last run", True, "never"))
    else:
        when = last.finished_at or last.started_at
        age = _relative(when)
        detail = f"{last.status} · {last.tasks} task(s) · {age}"
        if last.errors:
            detail += f" · {len(last.errors)} error(s)"
        rows.append(("last run", last.status in {"ok", "skipped"}, detail))
        if last.finished_at and scheduled_interval is not None:
            next_at = last.finished_at.timestamp() + scheduled_interval
            remaining = int(next_at - datetime.now(UTC).timestamp())
            rows.append(("next run", True, "due now" if remaining <= 0 else f"in ~{remaining}s"))

    try:
        secrets.get_linear_api_key()
        rows.append(("linear key", True, "resolved"))
    except GoblinError as e:
        rows.append(("linear key", True, f"unavailable — Linear refresh skipped ({e.message})"))

    gh_ok = shutil.which("gh") is not None
    rows.append(
        (
            "gh cli",
            True,
            "found — PR state and checks enabled"
            if gh_ok
            else "missing — PR state and checks skipped",
        )
    )

    notifier = resolve_notifier(cfg.sync)
    if notifier.name == "command" and not cfg.sync.notify_command:
        rows.append(
            (
                "notifications",
                False,
                "transport is 'command' but sync.notify_command is empty",
            )
        )
    else:
        rows.append(
            (
                "notifications",
                True,
                f"{notifier.name} · events: {', '.join(cfg.sync.notify_events) or 'none'}",
            )
        )

    # Opt-in and empty by default, so say plainly which of the two modes sync is
    # in — reporter, or supervisor (ADR 0012).
    wired = {event: names for event, names in cfg.sync.on.items() if names}
    if not wired:
        rows.append(("actions", True, "none configured — sync reports, it doesn't act"))
    else:
        rules = " · ".join(
            f"{event} → {', '.join(names)}" for event, names in sorted(wired.items())
        )
        limits = (
            f"cooldown {cfg.sync.action_rate_limit_seconds}s"
            if cfg.sync.action_rate_limit_seconds > 0
            else "no cooldown"
        )
        cap = (
            f"max {cfg.sync.max_actions_per_pass}/pass"
            if cfg.sync.max_actions_per_pass > 0
            else "uncapped"
        )
        rows.append(("actions", True, f"{rules} · {limits} · {cap}"))

    rows.append(
        (
            "prune",
            True,
            ("merged + clean tasks" if cfg.sync.prune else "disabled")
            + (
                f" · scratch idle > {cfg.sync.scratch_prune_days}d"
                if cfg.sync.scratch_prune_days > 0
                else " · scratch untouched"
            ),
        )
    )
    rows.append(("journal", True, str(paths.sync_journal_file())))

    table = Table(show_header=True, header_style="bold", title="gw sync")
    table.add_column("")
    table.add_column("Component")
    table.add_column("Detail")
    for name, ok, detail in rows:
        mark = "[green]✓[/]" if ok else "[red]✗[/]"
        table.add_row(mark, name, detail)
    console.print(table)

    if any(not ok for _n, ok, _d in rows):
        raise typer.Exit(code=1)


@app.command("install")
def install(
    interval: int | None = typer.Option(
        None, "--interval", help="Seconds between passes (default: sync.interval_seconds)."
    ),
) -> None:
    """Schedule `gw sync run` to fire periodically."""
    cfg = config.load()
    seconds = interval if interval is not None else cfg.sync.interval_seconds
    if seconds < 60:
        raise GoblinError(
            "Sync interval must be at least 60 seconds.",
            hint="A pass does network I/O per task; firing more often than that just queues work.",
        )
    if not launchd.is_supported():
        console.print(f"[hint]launchd is macOS-only. On {sys.platform}, add this crontab line:[/]")
        console.print(f"  {launchd.crontab_line(seconds)}")
        return
    path = launchd.install(seconds)
    print_success(f"Scheduled `gw sync run` every {seconds}s")
    console.print(f"  plist   {path}")
    console.print("  [muted]Check on it with `gw sync status`, watch it with `gw sync watch`.[/]")


@app.command("uninstall")
def uninstall() -> None:
    """Remove the scheduled sync job."""
    if not launchd.is_supported():
        console.print("[muted]Nothing to remove: launchd scheduling is macOS-only.[/]")
        return
    removed = launchd.uninstall()
    if removed:
        print_success("Removed the scheduled sync job")
    else:
        console.print("[muted]No scheduled sync job was installed.[/]")


def _relative(ts: datetime) -> str:
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=UTC)
    seconds = int((datetime.now(UTC) - ts).total_seconds())
    if seconds < 60:
        return "just now"
    if seconds < 3600:
        return f"{seconds // 60}m ago"
    if seconds < 86400:
        return f"{seconds // 3600}h ago"
    return f"{seconds // 86400}d ago"
