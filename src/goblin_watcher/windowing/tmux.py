"""Tmux windower: places agent processes inside a long-running tmux session.

Layout:
  - One tmux session named `goblin` (configurable).
  - One window per task, named after `task.id` (e.g. `eng-123`).
  - One pane per agent session. Second+ sessions on the same task `split-window`
    in the orientation set by `tmux.split` ("vertical" → top/bottom, the default;
    "horizontal" → side-by-side).

We pass the agent command directly to `new-window`/`split-window` as the pane's
command, wrapped in the user's login-interactive shell (`$SHELL -lic`) so the
shell environment matches a manual `tmux new-window` (PATH and friends from the
user's rc are sourced). We deliberately do *not* use `send-keys`: injecting the
command as keystrokes races the new pane's shell startup, and any rc prompt that
reads from the tty during that window (notably oh-my-zsh's auto-update
`Would you like to update? [Y/n]`, which does a `read -k 1`) swallows the first
keystroke — turning `claude …` into `laude …` → "command not found". Running the
command as the pane process can't lose characters because nothing is typed.

We also export `DISABLE_AUTO_UPDATE=true` for that shell. With keystrokes gone,
oh-my-zsh's update prompt would otherwise *block* the pane waiting for a keypress
(its `read` never returns), stalling the agent launch; suppressing it lets the
shell proceed straight to the agent.

`send` is the one place we *do* type into a pane — that's the whole point of
`gw session send`, which delivers a follow-up instruction to an agent already at
its prompt. Panes are addressed by the `@gw_session` pane option we stamp on
them at creation: tmux holds the session-id → pane mapping for exactly as long
as the pane lives, which is the correct lifetime and needs no state file.
"""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from goblin_watcher import config
from goblin_watcher.console import console
from goblin_watcher.errors import GoblinError, MissingDependencyError
from goblin_watcher.models import Task

# Pane-scoped user option carrying the gw session id a pane was launched for.
# tmux keeps user options (`@name`) on the pane until it dies, and exposes them
# to `-F` formats as `#{@name}` — so the mapping is queryable without gw
# recording pane ids anywhere.
_SESSION_OPTION = "@gw_session"


@dataclass(frozen=True)
class _Pane:
    """A live pane, and the gw session id it was stamped with (if any)."""

    pane_id: str
    session_id: str | None


def _ensure_tmux() -> str:
    found = shutil.which("tmux")
    if not found:
        raise MissingDependencyError(
            "`tmux` is not on PATH.",
            hint="Install tmux, or switch to windowing = 'inline'.",
        )
    return found


def _run_tmux(*args: str) -> subprocess.CompletedProcess[str]:
    _ensure_tmux()
    return subprocess.run(
        ["tmux", *args],
        capture_output=True,
        text=True,
        check=False,
    )


def _describe(panes: list[_Pane]) -> str:
    return ", ".join(p.session_id or f"{p.pane_id} (untagged)" for p in panes)


class TmuxWindower:
    name = "tmux"
    # Hands the agent off to a pane and returns while it is still starting up.
    # The pane is a real terminal, so agents run in their interactive mode.
    detaches = True
    headless = False

    def _session(self) -> str:
        return config.load().tmux.session_name

    def _attach_on_spawn(self) -> bool:
        return config.load().tmux.attach_on_spawn

    def _ensure_session(self) -> None:
        s = self._session()
        res = _run_tmux("has-session", "-t", s)
        if res.returncode != 0:
            _run_tmux("new-session", "-d", "-s", s, "-n", "intro")
        # Clear any stale `alert-silence` hook left over from the old
        # `bell_on_idle` code path. That hook ran `printf \a > /dev/tty`,
        # which fails inside tmux's run-shell context and surfaces an ugly
        # banner across the session. Idempotent — unsetting an absent hook
        # is a no-op.
        _run_tmux("set-hook", "-u", "-t", s, "alert-silence")

    @staticmethod
    def _pane_command(cmd: list[str], extra_env: dict[str, str] | None = None) -> str:
        """Build the shell-command tmux runs as the pane's process.

        Returns a single string suitable as `new-window`/`split-window`'s
        trailing command argument. tmux hands it to `/bin/sh -c`, which sets
        `DISABLE_AUTO_UPDATE` and `exec`s the user's login-interactive shell to
        run the agent — so the agent inherits the same environment a manual
        `tmux new-window` would give it, with no keystroke injection. After the
        agent exits we drop back to a fresh login shell in the pane (matching
        the previous send-keys behavior) rather than letting the pane close.
        """
        shell = os.environ.get("SHELL") or "/bin/zsh"
        agent_cmd = " ".join(shlex.quote(arg) for arg in cmd)
        # After the agent exits, leave an interactive shell in the pane.
        inner = f"{agent_cmd}; exec {shlex.quote(shell)} -li"
        # The pane's shell inherits the tmux server env, not gw's, so the
        # agent's extra vars (`Agent.env()`) ride along on the `env` prefix.
        env_args = "".join(
            f" {shlex.quote(f'{k}={v}')}" for k, v in sorted((extra_env or {}).items())
        )
        return (
            f"exec env DISABLE_AUTO_UPDATE=true{env_args} "
            f"{shlex.quote(shell)} -lic {shlex.quote(inner)}"
        )

    def _window_exists(self, task_id: str) -> bool:
        s = self._session()
        res = _run_tmux("list-windows", "-t", s, "-F", "#W")
        if res.returncode != 0:
            return False
        return any(line.strip() == task_id for line in res.stdout.splitlines())

    def run(
        self,
        *,
        task: Task,
        cmd: list[str],
        cwd: Path,
        env: dict[str, str],
        session_id: str | None = None,
    ) -> int:
        _ensure_tmux()
        self._ensure_session()
        s = self._session()
        target = f"{s}:{task.id}"
        pane_cmd = self._pane_command(cmd, extra_env=env)
        # `-P -F '#{pane_id}'` makes tmux print the new pane's id (`%12`) so we
        # can stamp the session id on it below.
        print_pane = ("-P", "-F", "#{pane_id}")
        if self._window_exists(task.id):
            # Add a pane to the existing window for this additional session.
            # `-v` stacks top/bottom, `-h` places side-by-side. `vertical` ==
            # panes-stacked-vertically matches tmux's `-v` flag letter.
            split_flag = "-h" if config.load().tmux.split == "horizontal" else "-v"
            res = _run_tmux(
                "split-window", split_flag, "-t", target, "-c", str(cwd), *print_pane, pane_cmd
            )
        else:
            # `-a` inserts the window *after* the session's current window and
            # shifts the rest up. Without it, `new-window -t <session>` targets
            # the current window's index and fails with "index N in use"
            # whenever that slot is occupied (the common case once the session
            # has windows) — silently leaving the agent unspawned.
            res = _run_tmux(
                "new-window", "-a", "-t", s, "-n", task.id, "-c", str(cwd), *print_pane, pane_cmd
            )
        if res.returncode != 0:
            raise GoblinError(
                f"tmux failed to open a window/pane for task '{task.id}': "
                f"{res.stderr.strip() or 'unknown error'}",
                hint="Run `tmux ls` to inspect the goblin session, "
                "or switch to windowing = 'inline'.",
            )
        self._tag_pane(res.stdout.strip(), session_id)

        cfg = config.load()
        if cfg.tmux.mark_idle:
            # `monitor-silence N` flags the window with a `~` in the status
            # bar when its pane sees no output for N seconds. No hook — the
            # visual marker is enough and never steals focus.
            _run_tmux(
                "set-window-option",
                "-t",
                target,
                "monitor-silence",
                str(cfg.tmux.mark_idle_seconds),
            )

        # Attach behavior depends on where gw was invoked from.
        if self._attach_on_spawn():
            if os.environ.get("TMUX"):
                # Already inside tmux. `select-window` activates the window
                # within the goblin session; `switch-client` then moves this
                # client there — without it, a user attached to a *different*
                # session would see nothing happen.
                _run_tmux("select-window", "-t", target)
                _run_tmux("switch-client", "-t", target)
            else:
                console.print(
                    f"[muted]Agent launched in tmux window {target}. "
                    f"Attaching: `tmux attach -t {s}`...[/]"
                )
                # Replace this process with the attach (best UX).
                tmux = _ensure_tmux()
                os.execvp(tmux, [tmux, "attach", "-t", s])

        return 0

    @staticmethod
    def _tag_pane(pane_id: str, session_id: str | None) -> None:
        """Stamp `session_id` on the pane so `send` can find it again.

        Best-effort on purpose: a failed tag (tmux older than 3.0 has no
        pane-scoped options) costs `gw session send` its precision on a
        multi-pane window, which is not worth failing a spawn over.
        """
        if not pane_id or not session_id:
            return
        _run_tmux("set-option", "-p", "-t", pane_id, _SESSION_OPTION, session_id)

    def _panes(self, task_id: str) -> list[_Pane]:
        """Live panes in `task_id`'s window, in tmux's own (index) order."""
        s = self._session()
        res = _run_tmux(
            "list-panes", "-t", f"{s}:{task_id}", "-F", f"#{{pane_id}}\t#{{{_SESSION_OPTION}}}"
        )
        if res.returncode != 0:
            return []
        panes: list[_Pane] = []
        for line in res.stdout.splitlines():
            pane_id, _, sid = line.partition("\t")
            if pane_id.strip():
                panes.append(_Pane(pane_id=pane_id.strip(), session_id=sid.strip() or None))
        return panes

    def send(
        self,
        *,
        task: Task,
        text: str,
        session_id: str | None = None,
        enter: bool = True,
    ) -> str:
        _ensure_tmux()
        s = self._session()
        panes = self._panes(task.id)
        if not panes:
            raise GoblinError(
                f"No live tmux pane for task {task.id!r} in session {s!r}.",
                hint=f"Spawn an agent first (`gw run {task.id}`), "
                f"or check `tmux list-windows -t {s}`.",
            )
        pane = self._resolve_pane(panes, session_id, task.id)
        # Two calls, deliberately: `-l` sends the argument literally (so a
        # message that looks like a key name — "Enter", "C-c" — is typed, not
        # interpreted), while Enter has to be sent *as* a key name to submit.
        # `--` keeps a message starting with `-` out of tmux's option parser.
        res = _run_tmux("send-keys", "-t", pane.pane_id, "-l", "--", text)
        if res.returncode != 0:
            raise GoblinError(
                f"tmux failed to send input to pane {pane.pane_id}: "
                f"{res.stderr.strip() or 'unknown error'}",
                hint=f"The pane may have just closed. Check `tmux list-panes -t {s}:{task.id}`.",
            )
        if enter:
            res = _run_tmux("send-keys", "-t", pane.pane_id, "Enter")
            if res.returncode != 0:
                raise GoblinError(
                    f"tmux delivered the text to pane {pane.pane_id} but failed to submit it: "
                    f"{res.stderr.strip() or 'unknown error'}",
                    hint="The text is sitting in the agent's input box; "
                    "press Enter there, or re-send.",
                )
        return f"{s}:{task.id} pane {pane.pane_id}"

    @staticmethod
    def _resolve_pane(panes: list[_Pane], session_id: str | None, task_id: str) -> _Pane:
        if session_id is not None:
            # Last match wins: resuming a session opens a *second* pane carrying
            # the same tag, and the newer one is the live conversation.
            tagged = [p for p in panes if p.session_id == session_id]
            if tagged:
                return tagged[-1]
            if len(panes) == 1:
                # Panes spawned before gw started tagging (or whose record was
                # re-keyed to the agent's real id afterwards) carry no usable
                # tag — but with a single pane on the window there is still
                # only one place the text could go.
                return panes[0]
            raise GoblinError(
                f"No pane on task {task_id!r} is tagged with session {session_id!r}.",
                hint=f"Live panes: {_describe(panes)}.",
            )
        if len(panes) == 1:
            return panes[0]
        raise GoblinError(
            f"Task {task_id!r} has {len(panes)} live panes — say which one.",
            hint=f"Pass --session <id>. Live panes: {_describe(panes)}.",
        )

    def is_live(self, task: Task) -> bool:
        s = self._session()
        res = _run_tmux("list-windows", "-t", s, "-F", "#W")
        if res.returncode != 0:
            return False
        return any(line.strip() == task.id for line in res.stdout.splitlines())

    def rename_window(self, old_task_id: str, new_task_id: str) -> bool:
        """Rename a task's window, if one is live. Returns whether a rename happened.

        Best-effort by design: callers (`gw task rename`) invoke this without
        knowing whether the task was ever spawned in tmux, so a missing binary,
        session, or window is a normal outcome, not an error.
        """
        if shutil.which("tmux") is None:
            return False
        if not self._window_exists(old_task_id):
            return False
        s = self._session()
        res = _run_tmux("rename-window", "-t", f"{s}:{old_task_id}", new_task_id)
        return res.returncode == 0
