"""launchd scheduling for `gw sync run` (ADR 0005).

Each firing executes whatever `gw` is currently installed, so an upgrade takes
effect on the next tick — the version-skew problem a resident daemon has simply
does not arise here. A crashed pass is retried by the next interval.

Non-darwin platforms get the equivalent crontab line printed rather than any
file written; gw does not edit a user's crontab behind their back.
"""

from __future__ import annotations

import json
import os
import plistlib
import shutil
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import unquote, urlparse

from goblin_watcher import paths
from goblin_watcher.errors import GoblinError
from goblin_watcher.sync.models import PassReport

LABEL = "com.goblin-watcher.sync"


def is_supported(platform: str | None = None) -> bool:
    return (platform or sys.platform) == "darwin"


def plist_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"


def resolve_gw_binary() -> str:
    """Absolute path to the `gw` executable launchd should run.

    launchd runs with a minimal PATH, so a bare `gw` would not resolve.
    """
    found = shutil.which("gw")
    if found:
        return found
    raise GoblinError(
        "Could not find the `gw` executable on PATH.",
        hint="Install gw so `which gw` resolves, then re-run `gw sync install`.",
    )


def editable_checkout(program: str | None = None) -> Path | None:
    """The git working tree `program`'s gw is installed *from*, or None.

    An editable install runs whatever is in the checkout at that moment, so a
    scheduled pass inherits every broken intermediate state of that tree — a
    half-applied edit, a rebase in progress, a venv whose interpreter drifted
    out from under the syntax the source uses. The pass then dies at import,
    which journals nothing, so `gw sync status` keeps reporting the job healthy.
    Callers warn on this; gw does not refuse to install, because developing gw
    on the machine that runs it is a legitimate and common setup.

    Read from the install's PEP 610 `direct_url.json`, which records both the
    source directory and the editable flag. Returns None whenever it can't tell
    — a normal install, a layout this doesn't recognize, an unreadable file.
    """
    binary = Path(program or shutil.which("gw") or "")
    venv = binary.parent.parent
    if not venv.is_dir():
        return None
    try:
        records = sorted(venv.glob("lib/python*/site-packages/goblin_watcher-*.dist-info"))
    except OSError:
        return None
    for record in records:
        try:
            payload = json.loads((record / "direct_url.json").read_text())
        except OSError, ValueError:
            continue
        if not isinstance(payload, dict) or not payload.get("dir_info", {}).get("editable"):
            continue
        url = payload.get("url")
        if not isinstance(url, str) or not url.startswith("file://"):
            continue
        root = Path(unquote(urlparse(url).path))
        return root if (root / ".git").exists() else None
    return None


def build_plist(interval_seconds: int, program: str | None = None) -> dict[str, object]:
    binary = program or resolve_gw_binary()
    log = str(paths.sync_launchd_log_file())
    return {
        "Label": LABEL,
        "ProgramArguments": [binary, "sync", "run"],
        "StartInterval": int(interval_seconds),
        # Passes are cheap and idempotent; skipping the load-time firing avoids
        # a burst of work every time the user logs in or reinstalls.
        "RunAtLoad": False,
        "StandardOutPath": log,
        "StandardErrorPath": log,
        "ProcessType": "Background",
        # launchd hands a job the bare `/usr/bin:/bin:/usr/sbin:/sbin`, which
        # does not include Homebrew. Without the installing shell's PATH baked
        # in, every scheduled pass silently loses `gh` (PR state + CI checks)
        # and `op` (1Password secret resolution) — they'd degrade to "no
        # signal" with no way to tell that from "nothing to report".
        "EnvironmentVariables": {"PATH": _install_path()},
    }


def _install_path() -> str:
    """PATH to bake into the plist: the installing shell's, plus the system default."""
    system = "/usr/bin:/bin:/usr/sbin:/sbin"
    current = os.environ.get("PATH", "")
    if not current:
        return system
    seen: list[str] = []
    for entry in [*current.split(os.pathsep), *system.split(os.pathsep)]:
        if entry and entry not in seen:
            seen.append(entry)
    return os.pathsep.join(seen)


def crontab_line(interval_seconds: int, program: str | None = None) -> str:
    """Equivalent cron entry, for platforms without launchd."""
    binary = program or shutil.which("gw") or "gw"
    minutes = max(1, round(interval_seconds / 60))
    schedule = "* * * * *" if minutes == 1 else f"*/{minutes} * * * *"
    return f"{schedule} {binary} sync run"


def installed_interval() -> int | None:
    """`StartInterval` from the installed plist, or None when it can't be read.

    `gw sync install --interval N` writes the plist without touching config, so
    the *scheduled* interval and `sync.interval_seconds` can legitimately
    differ. Anything reporting when the next pass fires has to read the plist.
    """
    target = plist_path()
    if not target.exists():
        return None
    try:
        with target.open("rb") as f:
            payload = plistlib.load(f)
    except OSError, plistlib.InvalidFileException, ValueError:
        return None
    value = payload.get("StartInterval") if isinstance(payload, dict) else None
    return value if isinstance(value, int) else None


# Intervals of slack before a scheduled job counts as "not firing". A single
# missed firing is normal (laptop asleep, a pass that overran its interval);
# three in a row is not.
_STALE_INTERVALS = 3


def last_pass_at(last: PassReport | None) -> datetime | None:
    """When the most recent pass reported in, tolerating naive stored timestamps."""
    if last is None:
        return None
    when = last.finished_at or last.started_at
    return when if when.tzinfo is not None else when.replace(tzinfo=UTC)


def staleness(*, last_finished: datetime | None, interval: int | None) -> str | None:
    """Why the scheduled job looks like it isn't firing, or None when it's on time.

    The one definition of "sync has quietly stopped", shared by `gw doctor`'s
    `background sync` row and `gw sync status`'s `last run` row so the two can't
    disagree about when to worry.

    It has to be time-based rather than status-based. A pass that dies *before*
    it can journal — a broken editable checkout, an interpreter the source no
    longer parses under — leaves `last_pass` frozen at the last good run while
    launchd goes on firing, so the recorded status stays `ok` forever. Age
    against the schedule is the only signal that survives that (gh-51).

    The plist's mtime stands in for "installed at" when no pass has ever run,
    which distinguishes "installed 20 seconds ago" (fine — `RunAtLoad` is off,
    so the first pass waits a full interval) from "installed last week and never
    ran" (broken).
    """
    if interval is None:
        return "the plist's StartInterval is unreadable, so gw can't tell when the next pass is due"
    since = last_finished
    if since is None:
        try:
            since = datetime.fromtimestamp(plist_path().stat().st_mtime, UTC)
        except OSError:
            return None
    age = int((datetime.now(UTC) - since).total_seconds())
    if age <= max(interval * _STALE_INTERVALS, 60):
        return None
    what = "no pass has run" if last_finished is None else "the last pass finished"
    return (
        f"{what} {age}s ago with a {interval}s interval — launchd is firing but gw is not "
        f"finishing. Check {paths.sync_launchd_log_file()}"
    )


def install(interval_seconds: int, program: str | None = None) -> Path:
    """Write the plist and load it. Returns the plist path."""
    target = plist_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    paths.logs_dir().mkdir(parents=True, exist_ok=True)
    payload = build_plist(interval_seconds, program=program)
    with target.open("wb") as f:
        plistlib.dump(payload, f)
    # Reinstall must be idempotent: drop any previous registration first.
    _launchctl(["bootout", _domain_target()], check=False)
    res = _launchctl(["bootstrap", _domain(), str(target)], check=False)
    if res.returncode != 0:
        # Older macOS (pre-10.11 semantics) and some sandboxes only support the
        # legacy verbs.
        legacy = _launchctl(["load", "-w", str(target)], check=False)
        if legacy.returncode != 0:
            raise GoblinError(
                "Wrote the launchd plist but could not load it.",
                hint=(res.stderr or legacy.stderr or "").strip()
                or f"Try: launchctl bootstrap {_domain()} {target}",
            )
    return target


def uninstall() -> bool:
    """Unload and delete the plist. Returns True if anything was removed."""
    target = plist_path()
    existed = target.exists()
    _launchctl(["bootout", _domain_target()], check=False)
    if existed:
        _launchctl(["unload", str(target)], check=False)
        target.unlink(missing_ok=True)
    return existed


def is_loaded() -> bool:
    """True when launchd currently has the job registered."""
    res = _launchctl(["print", _domain_target()], check=False)
    return res.returncode == 0


def _domain() -> str:
    return f"gui/{os.getuid()}"


def _domain_target() -> str:
    return f"{_domain()}/{LABEL}"


def _launchctl(args: list[str], *, check: bool) -> subprocess.CompletedProcess[str]:
    if shutil.which("launchctl") is None:
        raise GoblinError(
            "`launchctl` not found.",
            hint="Scheduling via launchd is only available on macOS.",
        )
    return subprocess.run(
        ["launchctl", *args],
        capture_output=True,
        text=True,
        check=check,
    )
