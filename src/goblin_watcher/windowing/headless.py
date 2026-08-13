"""Headless windower: runs an agent's print/exec mode as a detached process.

`inline` blocks on the agent and `tmux` needs a session to attach to — both
assume a human is present. This windower assumes nobody is: it starts the
agent in the non-interactive mode every registered CLI already has
(`Agent.headless_command` — `claude -p`, `codex exec`, `agy -p`, `gemini -p`),
points its output at a log file, and returns immediately. That's what makes
`gw` callable from cron, a queue, or another agent.

Mechanics:

- `start_new_session=True` puts the child in its own session and process
  group, so a `Ctrl-C` in — or the exit of — the `gw` process that launched it
  doesn't take the agent down with it.
- stdin is `/dev/null`. An agent that tries to read input gets EOF straight
  away instead of blocking forever on a terminal that isn't there.
- stdout and stderr are appended to
  `<project>/.goblin/logs/<task>-<session>.log`. Errors from a failed start
  (bad flag, expired auth) land there too — which is the log's main diagnostic
  value, since the conversation itself is in the agent's own transcript.
- A `<task>-<session>.pid` sidecar records the child's pid, so `is_live` can
  answer honestly and the user has something to `kill` when an unattended run
  goes wrong.

Completion is *not* observed here — nothing waits, so there is no exit status
to report. `gw sync` already fires an edge-triggered `agent-idle` notification
when a session's transcript goes quiet, which is what "tell me when it's done"
rides on. See ADR 0007.
"""

from __future__ import annotations

import contextlib
import os
import subprocess
from pathlib import Path

from goblin_watcher import paths, state
from goblin_watcher.console import console
from goblin_watcher.errors import GoblinError
from goblin_watcher.models import Task


class HeadlessWindower:
    name = "headless"
    # Returns as soon as the child is spawned, and gives it no terminal.
    detaches = True
    headless = True

    def run(
        self,
        *,
        task: Task,
        cmd: list[str],
        cwd: Path,
        env: dict[str, str],
        session_id: str | None = None,
    ) -> int:
        log_path = log_file(task, session_id)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        # Append, not truncate: a task can be run headlessly more than once,
        # and silently dropping the previous run's output would erase the only
        # record of why it failed.
        with log_path.open("ab") as log:
            try:
                proc = subprocess.Popen(
                    cmd,
                    cwd=str(cwd),
                    # `env` is the agent's extras only; merge over the full
                    # environment, exactly as inline does.
                    env={**os.environ, **env},
                    stdin=subprocess.DEVNULL,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                )
            except OSError as e:
                raise GoblinError(
                    f"Failed to start {cmd[0]!r} headlessly for task {task.id!r}: {e}",
                    hint=f"Check that {cmd[0]!r} is on PATH (`gw doctor`).",
                ) from e
        _write_pid(_pid_file(task, session_id), proc.pid)
        console.print(
            f"[muted]Detached (pid {proc.pid}). Output → {log_path}[/]\n"
            f"[muted]Follow it with `tail -f {log_path}`.[/]"
        )
        # The spawn succeeded; the agent's own exit status is nobody's to
        # collect here. Reporting 0 keeps `gw new`/`gw run` from failing a
        # launch that worked.
        return 0

    def is_live(self, task: Task) -> bool:
        """True while any detached run for this task still has a live process."""
        return any(_alive(_read_pid(p)) for p in _own_files(task, ".pid"))

    def send(
        self,
        *,
        task: Task,
        text: str,
        session_id: str | None = None,
        enter: bool = True,
    ) -> str:
        """Always raises: a headless agent has no input to type into.

        Its stdin is `/dev/null` and it is running a single turn to completion,
        so there is no prompt waiting for a follow-up instruction.
        """
        del text, session_id, enter
        raise GoblinError(
            f"Headless runs take no input (task {task.id!r}): stdin is /dev/null "
            "and the agent is running one turn to completion.",
            hint="Spawn agents in tmux (`gw config set defaults.windowing tmux`) "
            "for sessions you want to steer, or start another headless run with "
            "the follow-up as its prompt.",
        )


# --- log + pid file layout --------------------------------------------------
#
# Both files are named after the task and the session, so a task with several
# headless runs keeps them apart, and the pair is discoverable by globbing.


def _stem(task: Task, session_id: str | None) -> str:
    return f"{task.id}-{session_id}" if session_id else task.id


def _logs_dir(task: Task) -> Path:
    return paths.project_logs_dir(state.get_project(task.project).root)


def log_file(task: Task, session_id: str | None) -> Path:
    """Where a headless run's stdout/stderr is appended."""
    return _logs_dir(task) / f"{_stem(task, session_id)}.log"


def _pid_file(task: Task, session_id: str | None) -> Path:
    return _logs_dir(task) / f"{_stem(task, session_id)}.pid"


def _own_files(task: Task, suffix: str) -> list[Path]:
    """Every `suffix` file belonging to `task`, in the project's logs dir.

    Task ids can be prefixes of one another (`gh-1`, `gh-15`), so the glob
    requires the `-` separator the session suffix always carries.
    """
    try:
        d = _logs_dir(task)
    except GoblinError:
        # Project gone from the registry — no files we can claim.
        return []
    if not d.is_dir():
        return []
    return sorted([*d.glob(f"{task.id}-*{suffix}"), *d.glob(f"{task.id}{suffix}")])


def remove_run_files(task: Task) -> None:
    """Delete `task`'s headless logs and pid sidecars.

    Called when the task itself is destroyed (`gw task rm`, `gw new --rm`, a
    `gw sync` prune). The record that gave these files their names is gone, so
    nothing would ever reference or clean them again — and a task pruned by
    sync is one that merged cleanly, whose log has no post-mortem value left.
    Best-effort: removing a task must not fail over a leftover log.
    """
    for path in [*_own_files(task, ".log"), *_own_files(task, ".pid")]:
        with contextlib.suppress(OSError):
            path.unlink()


def _write_pid(path: Path, pid: int) -> None:
    """Record `pid` best-effort: losing the sidecar costs liveness reporting,
    which is not worth failing a launch that already succeeded over."""
    with contextlib.suppress(OSError):
        path.write_text(f"{pid}\n")


def _read_pid(path: Path) -> int | None:
    try:
        return int(path.read_text().strip())
    except OSError, ValueError:
        return None


def _alive(pid: int | None) -> bool:
    """Whether `pid` names a running process.

    Signal 0 checks for existence without delivering anything. `EPERM` means
    the process exists but belongs to someone else, which still counts as
    alive. Pid reuse can in principle make a stale sidecar read as live; the
    consequence is a cosmetically wrong liveness answer, so it doesn't earn a
    heavier mechanism.
    """
    if pid is None or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except PermissionError:
        return True
    except OSError:
        return False
    return True
