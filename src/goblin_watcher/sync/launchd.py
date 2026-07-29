"""launchd scheduling for `gw sync run` (ADR 0005).

Each firing executes whatever `gw` is currently installed, so an upgrade takes
effect on the next tick — the version-skew problem a resident daemon has simply
does not arise here. A crashed pass is retried by the next interval.

Non-darwin platforms get the equivalent crontab line printed rather than any
file written; gw does not edit a user's crontab behind their back.
"""

from __future__ import annotations

import os
import plistlib
import shutil
import subprocess
import sys
from pathlib import Path

from goblin_watcher import paths
from goblin_watcher.errors import GoblinError

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
    except (OSError, plistlib.InvalidFileException, ValueError):
        return None
    value = payload.get("StartInterval") if isinstance(payload, dict) else None
    return value if isinstance(value, int) else None


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
