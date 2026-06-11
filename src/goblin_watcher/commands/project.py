from datetime import UTC, datetime
from pathlib import Path

import typer
from rich.table import Table

from goblin_watcher import git, paths, state
from goblin_watcher.completion_enumerators import complete_projects
from goblin_watcher.console import console, print_settings, print_success
from goblin_watcher.errors import GoblinError, ProjectNotFoundError
from goblin_watcher.models import Project
from goblin_watcher.task_resolver import resolve_project

app = typer.Typer()


def _normalize_name(name: str) -> str:
    return name.strip().lower()


def _now() -> datetime:
    return datetime.now(UTC)


@app.command("new")
def new(
    name: str = typer.Argument(..., help="Project name (used in `gw <name>` references)."),
    repo: str | None = typer.Option(None, "--repo", help="Repo URL to clone."),
    dir: Path | None = typer.Option(
        None, "--dir", help="Existing directory to adopt as the project."
    ),
    default_branch: str | None = typer.Option(
        None, "--default-branch", help="Override the detected default branch."
    ),
    prefix: str = typer.Option("", "--prefix", help="Branch prefix for tasks (e.g. `goblin/`)."),
    team: str | None = typer.Option(
        None, "--team", help="Linear team key (e.g. ENG) for auto-resolution."
    ),
) -> None:
    """Register a project from a repo URL (--repo) or existing directory (--dir)."""
    if (repo is None) == (dir is None):
        raise GoblinError(
            "Specify exactly one of --repo or --dir.",
            hint="Use --repo <url> to clone, or --dir <path> to adopt an existing checkout.",
        )

    name_norm = _normalize_name(name)
    if not name_norm:
        raise GoblinError("Project name must not be empty.")
    if name_norm == "scratch":
        raise GoblinError(
            "'scratch' is a reserved project name (it backs `gw scratch` spaces).",
            hint="Pick another name.",
        )

    existing = state.load_global()
    if name_norm in existing.projects:
        raise GoblinError(
            f"A project named {name_norm!r} is already registered.",
            hint="Pick a different name or run `gw project rm` first.",
        )

    if repo is not None:
        projects_root = paths.projects_root()
        projects_root.mkdir(parents=True, exist_ok=True)
        dest = projects_root / name_norm
        if dest.exists():
            raise GoblinError(
                f"{dest} already exists; refusing to clone over it.",
                hint=f"Move it aside or pass --dir {dest} to adopt the existing directory.",
            )
        console.print(f"Cloning [bold]{repo}[/] into {dest}…")
        root = git.clone(repo, dest)
    else:
        assert dir is not None
        root = git.adopt(dir.resolve())

    detected_default = default_branch or git.default_branch(root)
    origin = git.origin_url(root) if repo is None else repo

    project = Project(
        name=name_norm,
        root=root,
        repo_url=origin,
        default_branch=detected_default,
        branch_prefix=prefix,
        linear_team_key=(team.upper() if team else None),
        created_at=_now(),
    )

    state.register_project(project)
    git.add_to_local_exclude(root, ".goblin/")
    git.add_to_local_exclude(root, ".worktrees/")

    print_success(f"Registered project {project.name!r}")
    print_settings(
        [
            ("root", str(project.root)),
            ("repo_url", project.repo_url or "(none)"),
            ("default_branch", project.default_branch),
            ("branch_prefix", project.branch_prefix or "(empty)"),
            ("linear_team", project.linear_team_key or "(none)"),
            ("created_at", project.created_at.isoformat()),
        ]
    )


@app.command("ls")
def ls() -> None:
    """List registered projects."""
    global_state = state.load_global()
    if not global_state.projects:
        console.print("[muted]No projects registered. Try `gw project new`.[/]")
        return
    table = Table(title="Projects", show_header=True, header_style="bold")
    table.add_column("Name")
    table.add_column("Root")
    table.add_column("Default branch")
    table.add_column("Team")
    for proj_name, root in sorted(global_state.projects.items()):
        try:
            proj = state.load_project(root)
        except ProjectNotFoundError:
            table.add_row(proj_name, str(root), "[red]missing[/]", "")
            continue
        table.add_row(
            proj.name,
            str(proj.root),
            proj.default_branch,
            proj.linear_team_key or "",
        )
    console.print(table)


@app.command("info")
def info(
    name: str | None = typer.Argument(
        None,
        help="Project name; omit to open the project picker.",
        autocompletion=complete_projects,
    ),
) -> None:
    """Show a project's metadata."""
    proj = resolve_project(name)

    console.print(f"[bold]{proj.name}[/]")
    console.print(f"  root            {proj.root}")
    console.print(f"  repo_url        {proj.repo_url or '(none)'}")
    console.print(f"  default_branch  {proj.default_branch}")
    console.print(f"  branch_prefix   {proj.branch_prefix or '(empty)'}")
    console.print(f"  linear_team     {proj.linear_team_key or '(none)'}")
    console.print(f"  created_at      {proj.created_at.isoformat()}")


_PULL_OUTCOME_STYLE: dict[git.PullBaseOutcome, str] = {
    "created": "success",
    "updated": "success",
    "up_to_date": "muted",
    "no_remote": "muted",
    "no_remote_branch": "hint",
    "fetch_failed": "error",
    "diverged": "hint",
    "dirty": "hint",
}


@app.command("pull")
def pull(
    name: str | None = typer.Argument(
        None,
        help="Project to pull; omit to pull every project with a remote.",
        autocompletion=complete_projects,
    ),
) -> None:
    """Fetch and fast-forward each project's default branch from its remote.

    Projects with no configured remote are skipped. A branch that has diverged
    from its remote, or that's checked out in a worktree with uncommitted
    changes, is left untouched and reported rather than forced.
    """
    global_state = state.load_global()
    if not global_state.projects:
        console.print("[muted]No projects registered. Try `gw project new`.[/]")
        return

    if name is not None:
        name_norm = _normalize_name(name)
        root = state.project_root_for(name_norm, global_state)
        targets = {name_norm: root}
    else:
        targets = dict(sorted(global_state.projects.items()))

    table = Table(title="Pull", show_header=True, header_style="bold")
    table.add_column("Project")
    table.add_column("Branch")
    table.add_column("Result")
    table.add_column("Last commit")

    for proj_name, root in targets.items():
        try:
            proj = state.load_project(root)
        except ProjectNotFoundError:
            table.add_row(proj_name, "", "[red]missing[/]", "")
            continue
        if proj.kind == "scratch":
            table.add_row(proj_name, "", "[muted]scratch — skipped[/]", "")
            continue
        if not git.has_remote(proj.root):
            title = git.last_commit_title(proj.root, proj.default_branch)
            table.add_row(
                proj_name,
                proj.default_branch,
                "[muted]no remote — skipped[/]",
                title or "",
            )
            continue
        result = git.pull_base_from_remote(proj.root, proj.default_branch)
        style = _PULL_OUTCOME_STYLE.get(result.outcome, "muted")
        title = git.last_commit_title(proj.root, proj.default_branch)
        table.add_row(
            proj_name,
            proj.default_branch,
            f"[{style}]{result.detail}[/]",
            title or "",
        )

    console.print(table)


@app.command("rm")
def rm(
    name: str = typer.Argument(
        ..., help="Project to unregister.", autocompletion=complete_projects
    ),
    force: bool = typer.Option(False, "--force", help="Skip the confirmation prompt."),
) -> None:
    """Unregister a project. Does NOT delete files on disk.

    Only the global registry entry (name → root) is required, so a project whose
    directory no longer exists on disk can still be removed.
    """
    name_norm = _normalize_name(name)
    root = state.project_root_for(name_norm)
    if not force:
        confirmed = typer.confirm(
            f"Unregister project {name_norm!r} at {root}? (Files on disk will NOT be deleted)",
            default=False,
        )
        if not confirmed:
            console.print("[muted]Cancelled.[/]")
            raise typer.Exit(code=1)
    state.unregister_project(name_norm)
    print_success(f"Unregistered project {name_norm!r}")
