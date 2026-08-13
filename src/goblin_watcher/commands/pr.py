from __future__ import annotations

from pathlib import Path

import typer

from goblin_watcher import gh, git, linear_transitions, state
from goblin_watcher.commands.task import _find_task
from goblin_watcher.completion_enumerators import complete_projects, complete_tasks
from goblin_watcher.console import console, print_success
from goblin_watcher.errors import GoblinError
from goblin_watcher.linear import LinearClient, parse_identifier
from goblin_watcher.models import LinearComment, Project, Task, TaskRepo
from goblin_watcher.secrets import get_linear_api_key
from goblin_watcher.task_resolver import _task_for_path

app = typer.Typer()


def _format_comments(comments: list[LinearComment]) -> str:
    return "\n\n".join(
        f"> **{c.author or 'unknown'}** · {c.created_at.strftime('%Y-%m-%d')}\n>\n"
        + "\n".join(f"> {line}" for line in c.body.splitlines())
        for c in comments
    )


def _commit_section(commits: list[tuple[str, str, str]]) -> str:
    """Render the commit list as bullets, with body paragraphs indented underneath."""
    lines: list[str] = []
    for _sha, subject, body in commits:
        lines.append(f"- **{subject}**")
        if body:
            for paragraph in body.split("\n\n"):
                wrapped = paragraph.strip()
                if wrapped:
                    lines.append(f"  {wrapped}")
    return "\n".join(lines)


def _closes_line(task: Task, repo_root: Path, project_repo_url: str | None) -> str | None:
    """The `Closes #42` line for a GitHub-issue task, or None when there isn't one.

    Uses the bare `#42` form when the issue lives in the repo the PR targets and
    the fully-qualified `owner/repo#42` form otherwise. GitHub only auto-closes
    across repositories when the PR author can write to the issue's repo, so the
    cross-repo form may land as a plain reference — the link is still correct
    either way.
    """
    issue = task.github_issue
    if issue is None:
        return None
    pr_repo = gh.normalize_repo(git.origin_url(repo_root)) or gh.normalize_repo(project_repo_url)
    target = f"#{issue.number}" if pr_repo == issue.repo else issue.reference
    return f"Closes {target} — {issue.title}"


def _stacked_section(task: Task, repo: TaskRepo, parent: Task) -> str | None:
    """The "stacked on" note for a PR cut from another task's branch, else None.

    Primary repo only: `Task.parent_task` describes what the task's own branch
    sits on, and a secondary repo's base is its own project's default branch.
    """
    if repo.project != task.project:
        return None
    link = f" — {parent.pr_url}" if parent.pr_url else ""
    return (
        f"## Stacked on `{parent.branch}`\n\n"
        f"This PR targets `{repo.base_branch}`, the branch of task `{parent.id}`{link}. "
        "Land that one first — the diff here shrinks to just this task's commits once it does."
    )


def _pr_body(
    task: Task,
    repo: TaskRepo,
    repo_root: Path,
    siblings: list[TaskRepo],
    project_repo_url: str | None = None,
    parent: Task | None = None,
) -> str:
    """Assemble a structured PR body for one repo: issue context + commits + diffstat.

    `siblings` are the task's other repos; when non-empty a "multi-repo change"
    note cross-references them so reviewers know the PR is one half of a set.
    `parent` is the task this one is stacked on, when its record still exists.
    """
    sections: list[str] = []

    if task.linear:
        sections.append(
            f"Resolves [{task.linear.identifier}]({task.linear.url}): {task.linear.title}"
        )

    closes = _closes_line(task, repo_root, project_repo_url)
    if closes is not None:
        sections.append(closes)

    if parent is not None:
        stacked = _stacked_section(task, repo, parent)
        if stacked is not None:
            sections.append(stacked)

    if siblings:
        sibling_lines = "\n".join(f"- `{s.project}` — branch `{s.branch}`" for s in siblings)
        sections.append(
            f"## Part of a multi-repo change (task `{task.id}`)\n\n"
            f"This PR is one of several for the same task. Sibling repos:\n{sibling_lines}"
        )

    if task.linear and (task.linear.description or task.linear.comments):
        issue_parts = ["## Issue"]
        if task.linear.description:
            issue_parts.append(task.linear.description.strip())
        if task.linear.comments:
            issue_parts.append("### Discussion from Linear")
            issue_parts.append(_format_comments(task.linear.comments))
        sections.append("\n\n".join(issue_parts))

    commits = git.commits_between(repo_root, repo.base_branch, repo.branch)
    stat = git.diffstat(repo_root, repo.base_branch, repo.branch)

    if commits or stat:
        change_parts = ["## What changed"]
        if commits:
            change_parts.append(_commit_section(commits))
        if stat:
            change_parts.append("### Files\n\n```\n" + stat + "\n```")
        sections.append("\n\n".join(change_parts))

    sections.append(
        f"---\n_Branch `{repo.branch}` off `{repo.base_branch}` · opened via `gw pr open`._"
    )

    return "\n\n".join(sections)


def _reject_scratch_task(task: Task) -> None:
    if task.kind == "scratch":
        raise GoblinError(
            f"Task {task.id!r} is a scratch space — there's no git repo or branch "
            "to open a PR from.",
        )


def _resolve_task(task_id: str | None, project: str | None) -> tuple[Project, Task]:
    if task_id is not None:
        return _find_task(task_id, project)
    try:
        task = _task_for_path(Path.cwd())
    except GoblinError as e:
        raise GoblinError(
            "Please specify a task id (e.g. `gw pr open eng-123`).",
            hint="Run from inside a task's worktree, or run `gw task ls` to see them.",
        ) from e
    return state.get_project(task.project), task


def _repo_root(proj: Project, repo: TaskRepo) -> Path:
    """Project root for `repo` (the already-loaded primary, or a lookup)."""
    if repo.project == proj.name:
        return proj.root
    return state.get_project(repo.project).root


def _repo_url(proj: Project, repo: TaskRepo) -> str | None:
    """Registered remote URL for `repo`, used as a fallback when git has none."""
    if repo.project == proj.name:
        return proj.repo_url
    return state.get_project(repo.project).repo_url


def _set_pr_url(task: Task, project_name: str, url: str) -> Task:
    """Return a copy of `task` with `url` stored on the matching repo + status bumped."""
    if project_name == task.project:
        return task.model_copy(update={"pr_url": url, "status": "pr-open"})
    secondaries = [
        r.model_copy(update={"pr_url": url}) if r.project == project_name else r
        for r in task.secondary_repos
    ]
    return task.model_copy(update={"secondary_repos": secondaries, "status": "pr-open"})


def _linear_comment_body(task: Task, opened: list[tuple[str, str]]) -> str:
    """Markdown comment posted on the Linear issue by `--notify-linear`."""
    if len(opened) == 1:
        return f"PR opened for this issue: {opened[0][1]}\n\n_via `gw pr open`_"
    lines = ["PRs opened for this issue:"]
    lines += [f"- **{project_name}**: {url}" for project_name, url in opened]
    lines.append("")
    lines.append("_via `gw pr open`_")
    return "\n".join(lines)


@app.command("open")
def open_(
    task_id: str | None = typer.Argument(
        None, help="Task id; defaults to the cwd's task.", autocompletion=complete_tasks
    ),
    project: str | None = typer.Option(
        None,
        "--project",
        help="Limit the search to one project (disambiguates a task id shared across projects).",
        autocompletion=complete_projects,
    ),
    repo: str | None = typer.Option(
        None,
        "--repo",
        help="For a multi-repo task, open a PR for only this repo's project.",
        autocompletion=complete_projects,
    ),
    draft: bool = typer.Option(False, "--draft", help="Open as a draft PR."),
    notify_linear: bool = typer.Option(
        False, "--notify-linear", help="Post a comment on the Linear issue with the PR URL."
    ),
) -> None:
    """Open a PR for a task via `gh` (one per repo for a multi-repo task)."""
    proj, task = _resolve_task(task_id, project)
    _reject_scratch_task(task)

    repos = task.all_repos()
    if repo is not None:
        wanted = repo.strip().lower()
        repos = [r for r in repos if r.project == wanted]
        if not repos:
            raise GoblinError(
                f"Task {task.id!r} has no repo for project {wanted!r}.",
                hint="Run `gw task show` to see the task's repos.",
            )

    # Resolved once: every repo's body cites the same parent, and the record is
    # unaffected by the pushes below.
    parent = state.find_parent_task(proj, task)

    opened: list[tuple[str, str]] = []
    for r in repos:
        repo_root = _repo_root(proj, r)
        siblings = [s for s in task.all_repos() if s.project != r.project]
        try:
            git.push(r.worktree_path, r.branch, set_upstream=True)
        except GoblinError as e:
            raise GoblinError(f"Failed to push {r.branch} to origin.", hint=e.hint) from e

        # Idempotency: an open PR for this head branch means the push above
        # already updated it — re-running `gw pr open` shouldn't fail inside
        # `gh pr create`. (A closed/merged PR doesn't block a fresh one.)
        existing = gh.pr_for_branch(r.worktree_path, r.branch)
        if existing and existing.get("state") == "OPEN" and existing.get("url"):
            url = existing["url"]
            console.print(
                f"[muted]PR already open for {r.branch!r} "
                f"(#{existing.get('number', '?')}); branch pushed, skipping create.[/]"
            )
        else:
            title = task.ticket_title or r.branch
            body = _pr_body(
                task,
                r,
                repo_root,
                siblings,
                project_repo_url=_repo_url(proj, r),
                parent=parent,
            )
            url = gh.create_pr(
                cwd=r.worktree_path,
                title=title,
                body=body,
                base=r.base_branch,
                head=r.branch,
                draft=draft,
            )
        # Narrow patch under the task lock (ADR 0004): `task` predates the push
        # and PR creation, so writing it back wholesale would revert concurrent
        # updates (e.g. a sync pass refreshing Linear state).
        task = state.update_task(
            proj, task.id, lambda latest, p=r.project, u=url: _set_pr_url(latest, p, u)
        )
        opened.append((r.project, url))

    if len(opened) == 1:
        print_success(f"Opened PR: {opened[0][1]}")
    else:
        print_success(f"Opened {len(opened)} PRs:")
        for project_name, url in opened:
            console.print(f"  [bold]{project_name}[/]  {url}")

    if notify_linear and task.linear:
        try:
            parse_identifier(task.linear.identifier)
            api_key = get_linear_api_key()
            with LinearClient(api_key) as client:
                client.create_comment(task.linear.id, _linear_comment_body(task, opened))
            print_success(f"Posted PR link to Linear issue {task.linear.identifier}")
        except GoblinError as e:
            console.print(f"[muted]Skipped Linear notification: {e.message}[/]")

    # Last, and after the PR URLs are already on screen: the ticket move is opt-in
    # (ADR 0012) and fails open, so a Linear that is down or slow costs one muted
    # line and nothing else. The PR exists either way.
    linear_transitions.apply(proj, task, "on_pr_open")


_CHECK_GLYPHS = {"passing": "[green]✓[/]", "failing": "[red]✗[/]", "pending": "[hint]●[/]"}
_CHECK_STYLES = {"passing": "green", "failing": "red", "pending": "hint"}
# Broken first, then still-running, then the ones that already passed: the check
# you opened this command to find should be the first row you read.
_CHECK_ORDER = {"failing": 0, "pending": 1, "passing": 2}


def _print_check_runs(runs: list[gh.CheckRun], indent: str) -> None:
    """Print one aligned row per check: glyph, name, state, details URL."""
    ordered = sorted(runs, key=lambda r: _CHECK_ORDER.get(r.state, 3))
    name_width = max(len(r.label) for r in ordered)
    detail_width = max(len(r.detail) for r in ordered)
    for run in ordered:
        glyph = _CHECK_GLYPHS.get(run.state, "[muted]?[/]")
        style = _CHECK_STYLES.get(run.state, "muted")
        detail = (run.detail or run.state).ljust(detail_width)
        row = f"{indent}{glyph} {run.label.ljust(name_width)}  [{style}]{detail}[/]"
        if run.url:
            row += f"  [muted]{run.url}[/]"
        console.print(row)


@app.command("checks")
def checks(
    task_id: str | None = typer.Argument(
        None, help="Task id; defaults to the cwd's task.", autocompletion=complete_tasks
    ),
    project: str | None = typer.Option(
        None,
        "--project",
        help="Limit the search to one project (disambiguates a task id shared across projects).",
        autocompletion=complete_projects,
    ),
) -> None:
    """Show each CI check on a task's PR: name, state, and details URL."""
    _, task = _resolve_task(task_id, project)
    _reject_scratch_task(task)

    console.print(f"[bold]{task.id}[/]")
    any_found = False
    for r in task.all_repos():
        indent = "    " if task.is_multi_repo else "  "
        if task.is_multi_repo:
            console.print(f"  [bold]{r.project}[/]")
        try:
            data = gh.pr_status(cwd=r.worktree_path)
        except GoblinError as e:
            console.print(f"{indent}[muted]{e.message}[/]")
            continue
        any_found = True
        runs = gh.pr_check_runs(data["url"])
        console.print(f"{indent}PR #{data['number']} · {data['state']} · {data['url']}")
        if not runs:
            console.print(f"{indent}  [muted]No checks reported for this PR.[/]")
            continue
        _print_check_runs(runs, indent + "  ")

    if not any_found:
        raise typer.Exit(code=1)


@app.command("status")
def status(
    task_id: str | None = typer.Argument(
        None, help="Task id; defaults to the cwd's task.", autocompletion=complete_tasks
    ),
    project: str | None = typer.Option(
        None,
        "--project",
        help="Limit the search to one project (disambiguates a task id shared across projects).",
        autocompletion=complete_projects,
    ),
) -> None:
    """Show PR status for a task (per repo for a multi-repo task)."""
    proj, task = _resolve_task(task_id, project)
    _reject_scratch_task(task)

    console.print(f"[bold]{task.id}[/]")
    any_found = False
    for r in task.all_repos():
        prefix = f"  [bold]{r.project}[/]  " if task.is_multi_repo else "  "
        try:
            data = gh.pr_status(cwd=r.worktree_path)
        except GoblinError as e:
            console.print(f"{prefix}[muted]{e.message}[/]")
            continue
        any_found = True
        if data["url"] and r.pr_url != data["url"]:
            task = state.update_task(
                proj,
                task.id,
                lambda latest, p=r.project, u=data["url"]: _set_pr_url(latest, p, u),
            )
        console.print(f"{prefix}PR #{data['number']} · {data['state']}")
        console.print(f"    {data['title']}")
        console.print(f"    {data['url']}")

    if not any_found:
        raise typer.Exit(code=1)
