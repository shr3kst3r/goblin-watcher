"""Bootstrap a freshly materialized worktree.

A new worktree is a bare checkout: no `.env`, no `.venv`, no `node_modules`,
nothing `uv sync` would have built. Anything gitignored simply isn't there, so
without a hook the first thing a spawned agent does is rediscover the project's
bootstrap — or fail at it and start guessing.

This module owns that hook. Three declarative lists (`copy`, `link`, `run`) are
resolved from the user config, or from a per-project `<root>/.goblin/setup.toml`
when the project defines one, and applied to the new worktree in that order.

Two boundaries matter here:

- **Containment.** `copy`/`link` entries are relative paths that must resolve
  inside the project root. A `..` escape would walk straight out of the safety
  boundary in AGENTS.md, so `resolve_inside` refuses it — including via a
  symlink that points outside.
- **Visibility.** Every step's outcome, and the output of every failed command,
  is journaled to `logs/setup.jsonl` and printed. A failed setup must be loud;
  the failure mode this feature exists to prevent is a half-built worktree that
  looks fine.
"""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import time
import tomllib
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from rich.markup import escape

from goblin_watcher import config, paths, state
from goblin_watcher.console import console
from goblin_watcher.errors import GoblinError
from goblin_watcher.models import Project, Task, TaskRepo

StepKind = Literal["copy", "link", "run"]
StepStatus = Literal["ok", "skipped", "failed"]

# Captured stdout/stderr is journaled and echoed on failure. Cap it so one
# chatty `npm install` can't turn the journal into a log dump.
MAX_CAPTURED_CHARS = 8_000


@dataclass(frozen=True)
class Step:
    """One applied setup step and how it went."""

    kind: StepKind
    target: str
    status: StepStatus
    detail: str
    output: str = ""


@dataclass
class SetupResult:
    steps: list[Step] = field(default_factory=list)
    # Where the config came from, for the "nothing happened and here's why" path.
    source: Path | None = None

    @property
    def ok(self) -> bool:
        return all(s.status != "failed" for s in self.steps)

    @property
    def failed(self) -> list[Step]:
        return [s for s in self.steps if s.status == "failed"]

    @property
    def ran_anything(self) -> bool:
        return bool(self.steps)


def load_setup(project: Project) -> tuple[config.SetupConfig, Path]:
    """The setup config for `project`, plus the file it came from.

    A project's own `.goblin/setup.toml` replaces the user-wide `[setup]` table
    outright — the same "presence overrides" rule `prompt.md` uses. Merging was
    rejected because there is no sane way to spell "drop the global `copy` entry
    for this one project".

    The project file accepts either a bare table or one nested under `[setup]`,
    so a snippet can be moved between the two files unedited.
    """
    project_file = paths.project_setup_file(project.root)
    if not project_file.exists():
        return config.load().setup, paths.config_file()
    try:
        raw: Any = tomllib.loads(project_file.read_text())
    except (OSError, tomllib.TOMLDecodeError) as e:
        raise GoblinError(
            f"{project_file} is not valid TOML: {e}",
            hint="Fix the file, or delete it to fall back to the global [setup] table.",
        ) from e
    nested = raw.get("setup")
    table = nested if isinstance(nested, dict) else raw
    try:
        return config.SetupConfig.model_validate(table), project_file
    except Exception as e:  # pydantic ValidationError, plus anything odd in the TOML
        raise GoblinError(f"Invalid setup config in {project_file}: {e}") from e


def resolve_inside(root: Path, entry: str, *, key: str) -> Path:
    """Resolve `entry` against `root`, refusing anything that leaves it.

    Three ways out are closed: an absolute path, a `..` component, and a symlink
    whose target sits outside the root (which is why the check is made against
    the *resolved* path, not the lexical one). `.` is refused too — copying an
    entire project root into its own worktree is never what was meant.
    """
    cleaned = entry.strip()
    if not cleaned:
        raise GoblinError(
            f"Empty entry in setup.{key}.",
            hint="Remove it, or name a path relative to the project root.",
        )
    if Path(cleaned).is_absolute():
        raise GoblinError(
            f"setup.{key} entry {entry!r} is an absolute path.",
            hint="Setup paths are relative to the project root.",
        )
    normalized = os.path.normpath(cleaned)
    if normalized == "." or Path(normalized).parts[0] == "..":
        raise GoblinError(
            f"setup.{key} entry {entry!r} escapes the project root.",
            hint="Setup paths must name something inside the project.",
        )
    resolved_root = root.resolve()
    target = (resolved_root / normalized).resolve()
    if resolved_root not in target.parents:
        raise GoblinError(
            f"setup.{key} entry {entry!r} resolves outside the project root "
            f"({target} is not under {resolved_root}).",
            hint="Setup paths must stay inside the project, symlinks included.",
        )
    return target


def _relative_dest(dest_root: Path, entry: str) -> Path:
    """Where `entry` lands inside the worktree — the same relative path."""
    return dest_root / os.path.normpath(entry.strip())


def _reject_self_containment(src: Path, dest_root: Path) -> None:
    """Refuse a source that contains the worktree we're populating.

    `.worktrees/` lives under the project root, so a careless `copy = ["."]`-ish
    entry could ask us to copy the destination into itself.
    """
    resolved_dest = dest_root.resolve()
    if src == resolved_dest or src in resolved_dest.parents:
        raise GoblinError(
            f"Setup source {src} contains the worktree {resolved_dest}.",
            hint="Name a specific file or directory, not an ancestor of the worktree.",
        )


def _resolve_all(
    setup: config.SetupConfig, src_root: Path, dest_root: Path
) -> tuple[list[tuple[str, Path]], list[tuple[str, Path]]]:
    """Resolve and validate every `copy`/`link` entry before any of them is applied.

    Front-loading the containment checks means a bad entry raises with nothing
    half-copied, and the error names the offending entry rather than whatever
    the filesystem happened to complain about first.
    """
    copies = [(e, resolve_inside(src_root, e, key="copy")) for e in setup.copy_paths]
    links = [(e, resolve_inside(src_root, e, key="link")) for e in setup.link_paths]
    for _, src in (*copies, *links):
        _reject_self_containment(src, dest_root)
    return copies, links


def _copy_step(dest_root: Path, entry: str, src: Path) -> Step:
    if not src.exists():
        return Step("copy", entry, "skipped", "not present in the project root")
    dest = _relative_dest(dest_root, entry)
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        if src.is_dir():
            shutil.copytree(src, dest, dirs_exist_ok=True, symlinks=True)
        else:
            if dest.is_symlink():
                # copy2 would follow the link and write through it, which for a
                # link created by a previous `setup.link` pass means writing
                # back into the project root.
                dest.unlink()
            shutil.copy2(src, dest)
    except OSError as e:
        return Step("copy", entry, "failed", str(e))
    return Step("copy", entry, "ok", f"copied from {src}")


def _link_step(dest_root: Path, entry: str, src: Path) -> Step:
    if not src.exists():
        return Step("link", entry, "skipped", "not present in the project root")
    dest = _relative_dest(dest_root, entry)
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.is_symlink() or dest.is_file():
        dest.unlink()
    elif dest.is_dir():
        return Step("link", entry, "failed", f"{dest} is a real directory; refusing to replace it")
    try:
        dest.symlink_to(src, target_is_directory=src.is_dir())
    except OSError as e:
        return Step("link", entry, "failed", str(e))
    return Step("link", entry, "ok", f"→ {src}")


def render_command(command: config.SetupCommand) -> str:
    """A single-line, copy-pasteable rendering of a `run` entry."""
    return command if isinstance(command, str) else shlex.join(command)


def _argv(command: config.SetupCommand) -> list[str]:
    """A string runs through `sh -c` (so `&&` and `$VARS` work); a list is exec'd as-is."""
    return ["/bin/sh", "-c", command] if isinstance(command, str) else list(command)


def _step_env(project: Project, dest_root: Path, task_id: str | None) -> dict[str, str]:
    env = dict(os.environ)
    env["GW_PROJECT"] = project.name
    env["GW_PROJECT_ROOT"] = str(project.root)
    env["GW_WORKTREE"] = str(dest_root)
    if task_id is not None:
        env["GW_TASK_ID"] = task_id
    return env


def _truncate(text: str) -> str:
    if len(text) <= MAX_CAPTURED_CHARS:
        return text
    return text[:MAX_CAPTURED_CHARS] + f"\n… [truncated at {MAX_CAPTURED_CHARS} chars]"


def _run_step(
    project: Project,
    dest_root: Path,
    command: config.SetupCommand,
    *,
    timeout_seconds: int,
    task_id: str | None,
) -> Step:
    rendered = render_command(command)
    started = time.monotonic()
    try:
        proc = subprocess.run(
            _argv(command),
            cwd=dest_root,
            env=_step_env(project, dest_root, task_id),
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired:
        return Step("run", rendered, "failed", f"timed out after {timeout_seconds}s")
    except OSError as e:
        return Step("run", rendered, "failed", str(e))
    elapsed = time.monotonic() - started
    output = _truncate((proc.stdout or "") + (proc.stderr or ""))
    if proc.returncode != 0:
        return Step("run", rendered, "failed", f"exit {proc.returncode}", output)
    return Step("run", rendered, "ok", f"{elapsed:.1f}s", output)


def _journal(record: dict[str, Any]) -> None:
    """Append one JSONL record. Losing an observability record never fails setup."""
    try:
        path = paths.setup_journal_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a") as f:
            f.write(json.dumps(record, default=str) + "\n")
    except OSError:
        pass


def _journal_step(run_id: str, project: Project, dest_root: Path, step: Step) -> None:
    record: dict[str, Any] = {
        "ts": datetime.now(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z"),
        "run_id": run_id,
        "project": project.name,
        "worktree": str(dest_root),
        "kind": step.kind,
        "target": step.target,
        "status": step.status,
        "detail": step.detail,
    }
    if step.output:
        record["output"] = step.output
    _journal(record)


_STATUS_STYLE = {"ok": "success", "skipped": "muted", "failed": "error"}


def _print_step(step: Step) -> None:
    # `target` is a config-supplied path or command line and `output` is whatever
    # the command wrote, so both are escaped: a stray `[a-z]` glob would
    # otherwise be swallowed as Rich markup — or raise on an unclosed tag.
    style = _STATUS_STYLE[step.status]
    console.print(
        f"  [muted]{step.kind:<4}[/] {escape(step.target)}  [{style}]{step.status}[/] "
        f"[muted]({escape(step.detail)})[/]"
    )
    if step.status == "failed" and step.output:
        for line in step.output.rstrip().splitlines():
            console.print(f"        [muted]{escape(line)}[/]")


def run_setup(
    project: Project,
    dest_root: Path,
    *,
    task_id: str | None = None,
    source_root: Path | None = None,
) -> SetupResult:
    """Apply the project's setup steps to the worktree at `dest_root`.

    `copy` then `link` then `run`, in that order: commands see the files the
    first two stages put in place. A failed `run` step stops the remaining ones —
    a bootstrap is a sequence, and continuing past a broken `uv sync` only buries
    the real error under its consequences.

    Returns the result rather than raising on a failed step; the caller decides
    whether a half-built worktree should abort what it was doing.
    """
    setup, source = load_setup(project)
    result = SetupResult(source=source)
    if setup.is_empty:
        return result

    src_root = source_root if source_root is not None else project.root
    copies, links = _resolve_all(setup, src_root, dest_root)
    run_id = f"{task_id or dest_root.name}-{datetime.now(UTC):%Y%m%dT%H%M%S%f}"
    console.print(
        f"Setup [muted](from {escape(str(source))})[/] in [muted]{escape(str(dest_root))}[/]…"
    )

    for entry, src in copies:
        result.steps.append(_copy_step(dest_root, entry, src))
    for entry, src in links:
        result.steps.append(_link_step(dest_root, entry, src))
    for i, command in enumerate(setup.run):
        step = _run_step(
            project,
            dest_root,
            command,
            timeout_seconds=setup.timeout_seconds,
            task_id=task_id,
        )
        result.steps.append(step)
        if step.status == "failed":
            result.steps.extend(
                Step("run", render_command(later), "skipped", "earlier step failed")
                for later in setup.run[i + 1 :]
            )
            break

    for step in result.steps:
        _journal_step(run_id, project, dest_root, step)
        _print_step(step)
    return result


def setup_task_repos(task: Task, repos: Iterable[TaskRepo]) -> SetupResult:
    """Run setup for each of `repos`, each against its own project's config.

    A multi-repo task is several checkouts of several projects; each one gets
    the bootstrap its own project defines, not the primary's.
    """
    combined = SetupResult()
    for repo in repos:
        project = state.get_project(repo.project)
        result = run_setup(project, repo.worktree_path, task_id=task.id)
        combined.steps.extend(result.steps)
        combined.source = combined.source or result.source
    return combined


def setup_failure(task_id: str, result: SetupResult) -> GoblinError:
    """The error a caller raises when setup left a worktree half-built."""
    first = result.failed[0]
    return GoblinError(
        f"Worktree setup failed for task {task_id!r}: {first.kind} {first.target} "
        f"({first.detail}).",
        hint=(
            f"Fix it, then re-run `gw task setup {task_id}` (and `gw run {task_id}` to "
            f"start the agent). Full output: {paths.setup_journal_file()}"
        ),
    )
