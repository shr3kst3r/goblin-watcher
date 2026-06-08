from __future__ import annotations

from pathlib import Path

import typer

from goblin_watcher import gh, git, state
from goblin_watcher.commands.task import _find_task
from goblin_watcher.completion_enumerators import complete_projects, complete_tasks
from goblin_watcher.console import console, print_success
from goblin_watcher.errors import GoblinError
from goblin_watcher.linear import LinearClient, parse_identifier
from goblin_watcher.models import LinearComment, Project, Task
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


def _pr_body(task: Task, repo_root: Path) -> str:
    """Assemble a structured PR body: issue context + commits + diffstat."""
    sections: list[str] = []

    if task.linear:
        sections.append(
            f"Resolves [{task.linear.identifier}]({task.linear.url}): {task.linear.title}"
        )

    if task.linear and (task.linear.description or task.linear.comments):
        issue_parts = ["## Issue"]
        if task.linear.description:
            issue_parts.append(task.linear.description.strip())
        if task.linear.comments:
            issue_parts.append("### Discussion from Linear")
            issue_parts.append(_format_comments(task.linear.comments))
        sections.append("\n\n".join(issue_parts))

    commits = git.commits_between(repo_root, task.base_branch, task.branch)
    stat = git.diffstat(repo_root, task.base_branch, task.branch)

    if commits or stat:
        change_parts = ["## What changed"]
        if commits:
            change_parts.append(_commit_section(commits))
        if stat:
            change_parts.append("### Files\n\n```\n" + stat + "\n```")
        sections.append("\n\n".join(change_parts))

    sections.append(
        f"---\n_Branch `{task.branch}` off `{task.base_branch}` · opened via `gw pr open`._"
    )

    return "\n\n".join(sections)


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
    draft: bool = typer.Option(False, "--draft", help="Open as a draft PR."),
    notify_linear: bool = typer.Option(
        False, "--notify-linear", help="Post a comment on the Linear issue with the PR URL."
    ),
) -> None:
    """Open a PR for a task via `gh`."""
    proj, task = _resolve_task(task_id, project)

    # Make sure the branch is pushed so `gh pr create` has a head to point at.
    try:
        git.push(task.worktree_path, task.branch, set_upstream=True)
    except GoblinError as e:
        raise GoblinError(
            f"Failed to push {task.branch} to origin.",
            hint=e.hint,
        ) from e

    title = task.linear.title if task.linear else task.branch
    body = _pr_body(task, repo_root=proj.root)
    url = gh.create_pr(
        cwd=task.worktree_path,
        title=title,
        body=body,
        base=task.base_branch,
        head=task.branch,
        draft=draft,
    )
    updated = task.model_copy(update={"pr_url": url, "status": "pr-open"})
    state.save_task(proj, updated)
    print_success(f"Opened PR: {url}")

    if notify_linear and task.linear:
        try:
            parse_identifier(task.linear.identifier)
            api_key = get_linear_api_key()
            with LinearClient(api_key):
                # Comment posting is out of scope for MVP; placeholder.
                console.print(
                    "[muted]--notify-linear: Linear comment posting "
                    "lands in a follow-up; PR URL stored on task.[/]"
                )
        except GoblinError as e:
            console.print(f"[muted]Skipped Linear notification: {e.message}[/]")


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
    """Show PR status for a task."""
    proj, task = _resolve_task(task_id, project)
    try:
        data = gh.pr_status(cwd=task.worktree_path)
    except GoblinError as e:
        console.print(f"[muted]{e.message}[/]")
        raise typer.Exit(code=1) from e

    if data["url"] and task.pr_url != data["url"]:
        updated = task.model_copy(update={"pr_url": data["url"]})
        state.save_task(proj, updated)

    console.print(f"[bold]{task.id}[/]  PR #{data['number']} · {data['state']}")
    console.print(f"  {data['title']}")
    console.print(f"  {data['url']}")
