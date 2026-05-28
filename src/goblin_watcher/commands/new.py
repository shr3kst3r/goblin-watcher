import re
from datetime import UTC, datetime
from pathlib import Path

import click
import typer

from goblin_watcher import config, gh, git, paths, secrets, state
from goblin_watcher.agents import AGENT_NAMES, get_agent, validate_agent_for_project
from goblin_watcher.agents.launcher import Fresh, build_seed_prompt, launch
from goblin_watcher.completion_enumerators import complete_projects
from goblin_watcher.console import agent_badge, console, print_settings, print_success
from goblin_watcher.errors import GoblinError, ProjectNotFoundError, TaskNotFoundError
from goblin_watcher.linear import LinearClient, parse_identifier
from goblin_watcher.models import LinearIssue, Project, Task
from goblin_watcher.slug import branch_slug, random_branch_name, slugify
from goblin_watcher.task_resolver import resolve_project
from goblin_watcher.windowing import get_windower


def _now() -> datetime:
    return datetime.now(UTC)


def _find_project_containing(path: Path) -> Project:
    """Find the registered project that owns `path`.

    Matches if `path` is inside the project's root, OR if `path` is a worktree
    sharing the same main repository (via `git rev-parse --git-common-dir`).
    """
    resolved = path.resolve()
    # First try: path is inside a registered project root.
    candidates: list[tuple[int, Project]] = []
    for name in state.load_global().projects:
        try:
            proj = state.get_project(name)
        except ProjectNotFoundError:
            continue
        try:
            depth = len(resolved.relative_to(proj.root.resolve()).parts)
        except ValueError:
            continue
        candidates.append((depth, proj))
    if candidates:
        candidates.sort(key=lambda c: c[0])
        return candidates[0][1]

    # Second try: path is a worktree of a registered project's main repo.
    if git.is_git_repo(resolved):
        main_root = git.main_repo_root(resolved)
        for name in state.load_global().projects:
            try:
                proj = state.get_project(name)
            except ProjectNotFoundError:
                continue
            if proj.root.resolve() == main_root:
                return proj

    raise GoblinError(
        f"{path} is not inside any registered project.",
        hint="Register the project first via `gw project new`.",
    )


def _task_id_from_branch(branch: str) -> str:
    return slugify(branch.replace("/", "-"), max_len=60)


def _ensure_unique_branch(repo: Path, branch: str) -> str:
    """If `branch` exists, append -2, -3, ... until we find a free name."""
    if not git.branch_exists(repo, branch):
        return branch
    n = 2
    while git.branch_exists(repo, f"{branch}-{n}"):
        n += 1
    return f"{branch}-{n}"


def _refresh_base(repo: Path, base: str) -> git.PullBaseResult:
    """Fetch and fast-forward `base` from origin, surfacing the outcome.

    On `diverged`, `dirty`, or `fetch_failed`, prints a warning and continues:
    the user asked us to stay current, but we won't rewrite their work or
    block worktree creation when the network's flaky.
    """
    res = git.pull_base_from_remote(repo, base)
    if res.outcome in {"updated", "created"}:
        console.print(f"[success]{res.detail}[/]")
    elif res.outcome in {"diverged", "dirty", "fetch_failed"}:
        console.print(f"[hint]Warning:[/] {res.detail}")
    elif res.outcome in {"up_to_date", "no_remote_branch"}:
        console.print(f"[muted]{res.detail}[/]")
    return res


def _load_existing_task(proj: Project, task_id: str) -> Task | None:
    try:
        return state.load_task(proj, task_id)
    except TaskNotFoundError:
        return None


def _raise_task_exists(proj: Project, task_id: str) -> None:
    raise GoblinError(
        f"Task {task_id!r} already exists in project {proj.name!r}.",
        hint=(
            f"Use `gw run {task_id}` to resume it "
            f"(or `gw run {task_id} --new` to start a fresh session on the same task)."
        ),
    )


def _source_label(
    linear: str | None,
    branch: str | None,
    branch_name: str | None,
    branch_auto: bool,
    directory: Path | None,
    pr: str | None,
    task: Task,
) -> str:
    if linear is not None:
        return f"--linear {linear}"
    if pr is not None:
        return f"--pr {pr}"
    if branch is not None:
        return f"--branch {branch}"
    if branch_name is not None:
        return f"--branch-name {branch_name}"
    if branch_auto:
        return f"--branch-auto ({task.branch})"
    if directory is not None:
        return f"--dir {directory}"
    return "(unknown)"


def new(
    linear: str | None = typer.Option(None, "--linear", help="Linear issue id (e.g. ENG-123)."),
    branch: str | None = typer.Option(
        None, "--branch", help="Existing branch to base the task on."
    ),
    branch_name: str | None = typer.Option(
        None, "--branch-name", help="New branch name for a fresh task."
    ),
    branch_auto: bool = typer.Option(
        False,
        "--branch-auto",
        help="New branch with an auto-generated name (e.g. goblin-watcher-falcon).",
    ),
    pr: str | None = typer.Option(
        None,
        "--pr",
        help="GitHub PR number or URL to check out (head branch + its base).",
    ),
    from_: str | None = typer.Option(
        None,
        "--from",
        help=(
            "Base branch when creating a new branch. Applies to --linear, "
            "--branch-name, and --branch-auto. If the base only exists on "
            "origin, it is fetched automatically."
        ),
    ),
    dir: Path | None = typer.Option(None, "--dir", help="Existing directory to adopt as the task."),
    title: str | None = typer.Option(None, "--title", help="Task title (used for seed prompt)."),
    project: str | None = typer.Option(
        None,
        "--project",
        help="Project name (opens the project picker if omitted).",
        autocompletion=complete_projects,
    ),
    repo: str | None = typer.Option(None, "--repo", help="Repo URL to clone if missing."),
    agent: str | None = typer.Option(
        None,
        "--agent",
        help="Agent to launch.",
        click_type=click.Choice(list(AGENT_NAMES)),
    ),
    no_launch: bool = typer.Option(False, "--no-launch", help="Create the task but do not launch."),
    windowing: str | None = typer.Option(
        None,
        "--windowing",
        help="Overrides config.",
        click_type=click.Choice(["inline", "tmux"]),
    ),
    unsafe: bool | None = typer.Option(
        None,
        "--unsafe/--no-unsafe",
        help="Run the agent with its bypass-permission flag (e.g. claude's "
        "--dangerously-skip-permissions). Overrides defaults.unsafe in config.",
    ),
    prompt: str | None = typer.Option(
        None,
        "--prompt",
        help="Initial prompt to seed the fresh session with. The agent begins "
        "work on this immediately instead of waiting for the next message.",
    ),
    adversarial_review: bool = typer.Option(
        False,
        "--adversarial-review",
        help="Seed the session with `/codex:adversarial-review`. Forces --agent claude.",
    ),
) -> None:
    """Create a task from a source (Linear, GitHub PR, branch, new branch, or directory)."""
    if prompt is not None and no_launch:
        raise GoblinError(
            "--prompt has no effect with --no-launch (no session is started).",
            hint="Drop --no-launch, or drop --prompt.",
        )
    if adversarial_review:
        if no_launch:
            raise GoblinError(
                "--adversarial-review has no effect with --no-launch (no session is started).",
                hint="Drop --no-launch, or drop --adversarial-review.",
            )
        if prompt is not None:
            raise GoblinError(
                "--adversarial-review and --prompt are mutually exclusive.",
                hint="Pass one or the other.",
            )
        if agent is not None and agent != "claude":
            raise GoblinError(
                "--adversarial-review requires --agent claude "
                "(the skill is a Claude Code slash command).",
                hint=f"Drop --agent {agent}, or drop --adversarial-review.",
            )
        agent = "claude"
    sources: list[object] = [s for s in (linear, branch, branch_name, dir, pr) if s is not None]
    if branch_auto:
        sources.append("auto")
    if len(sources) != 1:
        raise GoblinError(
            "Specify exactly one source: --linear, --branch, --branch-name, "
            "--branch-auto, --dir, or --pr.",
            hint="e.g. `gw new --branch-auto` or `gw new --pr 42`.",
        )

    if linear is not None:
        task = _from_linear(linear, project, repo, title, from_)
    elif pr is not None:
        task = _from_pr(pr, project, repo, title)
    elif dir is not None:
        task = _from_existing_dir(dir, title)
    elif branch is not None:
        proj = resolve_project(project)
        task = _from_existing_branch(proj, branch, title)
    else:
        proj = resolve_project(project)
        generated = random_branch_name(proj.name) if branch_auto else None
        chosen = branch_name if branch_name is not None else generated
        assert chosen is not None
        task = _from_new_branch(proj, chosen, from_, title)

    proj = state.get_project(task.project)
    state.save_task(proj, task)

    cfg = config.load()
    agent_name = agent or (cfg.defaults.agent) or "claude"
    windowing_mode = windowing or cfg.defaults.windowing
    unsafe_mode = cfg.defaults.unsafe if unsafe is None else unsafe
    source_label = _source_label(linear, branch, branch_name, branch_auto, dir, pr, task)

    print_success(f"Created task {task.id!r} on branch {task.branch!r}")
    settings: list[tuple[str, str]] = [
        ("project", proj.name),
        ("source", source_label),
        ("branch", task.branch),
        ("base", task.base_branch),
        ("worktree", str(task.worktree_path)),
    ]
    if title:
        settings.append(("title", title))
    if task.linear is not None:
        settings.append(("linear", f"{task.linear.identifier}: {task.linear.title}"))
    if pr is not None and task.pr_url:
        settings.append(("pr", task.pr_url))
    settings += [
        ("agent", agent_name),
        ("windowing", windowing_mode),
        ("unsafe", str(unsafe_mode).lower()),
        ("no_launch", str(no_launch).lower()),
    ]
    print_settings(settings)

    if no_launch:
        return

    validate_agent_for_project(agent_name, proj)
    agent_obj = get_agent(agent_name)
    windower = get_windower(windowing_mode)

    # `/codex:adversarial-review` must be the entire user message — Claude
    # Code's slash-command parser ignores it if buried in the seed template.
    # `--wait` runs the review in the foreground (skips the skill's wait-vs-
    # background prompt).
    seed_prompt = (
        "/codex:adversarial-review --wait"
        if adversarial_review
        else build_seed_prompt(task, user_prompt=prompt)
    )
    choice = Fresh(prompt=seed_prompt)
    console.print(f"Launching {agent_badge(agent_name)} (fresh) in [muted]{windowing_mode}[/]…")
    exit_code, _ = launch(
        project=proj,
        task=task,
        agent=agent_obj,
        choice=choice,
        windower=windower,
        unsafe=unsafe_mode,
    )
    if exit_code != 0:
        raise typer.Exit(code=exit_code)


def _from_new_branch(proj: Project, branch_name: str, from_: str | None, title: str | None) -> Task:
    del title  # only relevant to seed prompt (Phase 5)
    base = from_ or proj.default_branch
    _refresh_base(proj.root, base)
    final_branch = _ensure_unique_branch(proj.root, f"{proj.branch_prefix}{branch_name}")
    task_id = _task_id_from_branch(final_branch)
    worktree_dir = paths.worktree_root(proj.root, proj.worktree_root) / task_id
    git.worktree_add(proj.root, worktree_dir, final_branch, base=base)
    return Task(
        id=task_id,
        project=proj.name,
        branch=final_branch,
        worktree_path=worktree_dir,
        base_branch=base,
        created_at=_now(),
    )


def _from_existing_branch(proj: Project, branch: str, title: str | None) -> Task:
    del title  # not used for branch naming; only relevant to prompt seed (Phase 5)
    repo = proj.root
    task_id = _task_id_from_branch(branch)
    if _load_existing_task(proj, task_id) is not None:
        _raise_task_exists(proj, task_id)

    _refresh_base(repo, branch)
    if not git.branch_exists(repo, branch):
        raise GoblinError(
            f"Branch {branch!r} does not exist locally or on origin.",
            hint="Check the name or push the branch first.",
        )

    worktree_dir = paths.worktree_root(proj.root, proj.worktree_root) / task_id
    if not worktree_dir.exists():
        git.worktree_add(repo, worktree_dir, branch)

    return Task(
        id=task_id,
        project=proj.name,
        branch=branch,
        worktree_path=worktree_dir,
        base_branch=proj.default_branch,
        created_at=_now(),
    )


_PR_URL_RE = re.compile(r"https://github\.com/(?P<repo>[^/\s]+/[^/\s]+)/pull/\d+")
_SSH_REMOTE_RE = re.compile(r"git@github\.com:(?P<repo>.+)")


def _parse_pr_url(pr: str) -> str | None:
    """Return the lowercased `owner/repo` from a GitHub PR URL, else None.

    None means `pr` is a bare number (or otherwise not a PR URL) and the
    project must be resolved via `--project` or the picker.
    """
    m = _PR_URL_RE.match(pr.strip())
    if m is None:
        return None
    return m.group("repo").removesuffix(".git").lower()


def _normalize_repo(url: str | None) -> str | None:
    """Reduce a git remote URL to its lowercased `owner/repo`, else None.

    Handles `https://github.com/owner/repo(.git)` and
    `git@github.com:owner/repo.git`.
    """
    if not url:
        return None
    url = url.strip()
    https = re.match(r"https://github\.com/(?P<repo>[^/\s]+/[^/\s]+?)(?:\.git)?/?$", url)
    if https is not None:
        return https.group("repo").lower()
    ssh = _SSH_REMOTE_RE.match(url)
    if ssh is not None:
        return ssh.group("repo").removesuffix(".git").lower()
    return None


def _project_for_repo(owner_repo: str) -> Project | None:
    """Find a registered project whose remote resolves to `owner/repo`."""
    needle = owner_repo.lower()
    for name in state.load_global().projects:
        try:
            proj = state.get_project(name)
        except ProjectNotFoundError:
            continue
        if _normalize_repo(proj.repo_url) == needle:
            return proj
    return None


def _resolve_pr_project(pr: str, project_override: str | None) -> Project:
    """Resolve the project for a PR-driven task.

    Precedence: --project → repo match from a PR URL → project picker (bare
    number).
    """
    if project_override:
        return state.get_project(project_override.strip().lower())

    owner_repo = _parse_pr_url(pr)
    if owner_repo is not None:
        matched = _project_for_repo(owner_repo)
        if matched is not None:
            return matched
        raise GoblinError(
            f"No registered project matches the PR's repo {owner_repo!r}.",
            hint=(
                "Register it first (`gw project new <name> --repo <url>`), or "
                "pass --project <name> to pick one."
            ),
        )

    # Bare PR number: fall back to --project / the picker.
    return resolve_project(project_override)


def _from_pr(
    pr: str,
    project_override: str | None,
    repo_url: str | None,
    title: str | None,
) -> Task:
    del repo_url, title  # PR title/branch supersede; auto-clone is out of scope.
    proj = _resolve_pr_project(pr, project_override)
    info = gh.pr_view(pr, cwd=proj.root)

    if info.is_cross_repository:
        raise GoblinError(
            f"PR #{info.number} is from a fork (cross-repository).",
            hint="Cross-repo PRs aren't supported yet; check the branch out manually.",
        )

    task_id = _task_id_from_branch(info.head_ref)
    if _load_existing_task(proj, task_id) is not None:
        _raise_task_exists(proj, task_id)

    _refresh_base(proj.root, info.head_ref)
    if not git.branch_exists(proj.root, info.head_ref):
        raise GoblinError(
            f"PR head branch {info.head_ref!r} does not exist locally or on origin.",
            hint="The branch may have been deleted, or origin needs a fetch.",
        )

    worktree_dir = paths.worktree_root(proj.root, proj.worktree_root) / task_id
    if not worktree_dir.exists():
        git.worktree_add(proj.root, worktree_dir, info.head_ref)

    return Task(
        id=task_id,
        project=proj.name,
        branch=info.head_ref,
        worktree_path=worktree_dir,
        base_branch=info.base_ref,
        pr_url=info.url,
        status="pr-open",
        created_at=_now(),
    )


def _project_for_linear_team(team_key: str) -> Project | None:
    """Find a registered project whose `linear_team_key` matches (case-insensitive)."""
    needle = team_key.upper()
    for name in state.load_global().projects:
        try:
            proj = state.get_project(name)
        except ProjectNotFoundError:
            continue
        if (proj.linear_team_key or "").upper() == needle:
            return proj
    return None


def _resolve_or_register_linear_project(
    team_key: str, project_override: str | None, repo_url: str | None
) -> Project:
    """Resolve the project to use for a Linear-driven task.

    Precedence: --project → linear_team_key match → --repo (clones + registers a new project).
    """
    if project_override:
        return state.get_project(project_override.strip().lower())

    matched = _project_for_linear_team(team_key)
    if matched is not None:
        return matched

    if repo_url is None:
        raise GoblinError(
            f"No registered project for Linear team {team_key!r}.",
            hint=(
                "Pass --project <name> to pick one, or --repo <url> to clone and register a "
                "new project for this team."
            ),
        )

    name = team_key.lower()
    if name in state.load_global().projects:
        raise GoblinError(
            f"A project named {name!r} is already registered, but isn't tagged for "
            f"Linear team {team_key!r}.",
            hint=f"Run `gw project info {name}` and set its --team, or pick another --project.",
        )

    projects_root = paths.projects_root()
    projects_root.mkdir(parents=True, exist_ok=True)
    dest = projects_root / name
    if dest.exists():
        raise GoblinError(
            f"{dest} already exists; refusing to clone over it.",
            hint=f"Pass --project to use an existing project, or move {dest} aside.",
        )
    console.print(f"Cloning [bold]{repo_url}[/] into {dest}…")
    root = git.clone(repo_url, dest)
    project = Project(
        name=name,
        root=root,
        repo_url=repo_url,
        default_branch=git.default_branch(root),
        branch_prefix="",
        linear_team_key=team_key.upper(),
        created_at=_now(),
    )
    state.register_project(project)
    git.add_to_local_exclude(root, ".goblin/")
    git.add_to_local_exclude(root, ".worktrees/")
    return project


def _from_linear(
    linear_id: str,
    project_override: str | None,
    repo_url: str | None,
    title: str | None,
    from_: str | None,
) -> Task:
    del title  # Linear's title supersedes
    parse_identifier(linear_id)  # validates the form; we use the fetched issue's team
    api_key = secrets.get_linear_api_key()
    with LinearClient(api_key) as client:
        issue = client.fetch_issue(linear_id)

    proj = _resolve_or_register_linear_project(issue.team_key, project_override, repo_url)

    task_id = issue.identifier.lower()
    if _load_existing_task(proj, task_id) is not None:
        _raise_task_exists(proj, task_id)

    base = from_ or proj.default_branch
    _refresh_base(proj.root, base)
    branch = _ensure_unique_branch(
        proj.root, branch_slug(issue.identifier, issue.title, prefix=proj.branch_prefix)
    )
    worktree_dir = paths.worktree_root(proj.root, proj.worktree_root) / task_id
    if not worktree_dir.exists():
        git.worktree_add(proj.root, worktree_dir, branch, base=base)
    return Task(
        id=task_id,
        project=proj.name,
        linear=_clone_linear_issue(issue),
        branch=branch,
        worktree_path=worktree_dir,
        base_branch=base,
        created_at=_now(),
    )


def _clone_linear_issue(issue: LinearIssue) -> LinearIssue:
    # We persist the issue as-of-creation; identical fields, but a fresh object so future
    # mutations of the source don't leak into the persisted snapshot.
    return LinearIssue.model_validate(issue.model_dump())


def _from_existing_dir(directory: Path, title: str | None) -> Task:
    del title  # only relevant to seed prompt (Phase 5)
    directory = directory.resolve()
    if not directory.exists():
        raise GoblinError(f"{directory} does not exist.")
    if not git.is_git_repo(directory):
        raise GoblinError(
            f"{directory} is not a git working tree.",
            hint="Use --branch or --branch-name on a registered project instead.",
        )
    proj = _find_project_containing(directory)
    branch_here = git.current_branch(directory)
    task_id = _task_id_from_branch(branch_here)
    if _load_existing_task(proj, task_id) is not None:
        _raise_task_exists(proj, task_id)
    return Task(
        id=task_id,
        project=proj.name,
        branch=branch_here,
        worktree_path=directory,
        base_branch=proj.default_branch,
        created_at=_now(),
    )
