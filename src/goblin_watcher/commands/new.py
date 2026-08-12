import re
from datetime import UTC, datetime
from pathlib import Path

import click
import typer

from goblin_watcher import config, gh, git, paths, secrets, state, workspace
from goblin_watcher.agents import AGENT_NAMES, get_agent, validate_agent_for_project
from goblin_watcher.agents.launcher import Fresh, build_seed_prompt, launch
from goblin_watcher.commands.task import destroy_task, dirty_worktrees
from goblin_watcher.completion_enumerators import complete_projects
from goblin_watcher.console import agent_badge, console, print_settings, print_success
from goblin_watcher.errors import GoblinError, ProjectNotFoundError, TaskNotFoundError
from goblin_watcher.linear import LinearClient, parse_identifier
from goblin_watcher.models import GhIssue, LinearIssue, Project, Task
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


def _reject_scratch_project(proj: Project) -> None:
    """Repo tasks need a git project; the reserved scratch container isn't one."""
    if proj.kind == "scratch":
        raise GoblinError(
            f"Project {proj.name!r} is the reserved scratch container and can't host repo tasks.",
            hint="Use `gw scratch [NAME]` to create a scratch space instead.",
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
            f"(or `gw run {task_id} --new` to start a fresh session on the same task). "
            f"Pass --rm to remove it and start over."
        ),
    )


def _handle_existing(
    proj: Project,
    task_id: str,
    *,
    rm: bool,
    force: bool,
    delete_branch: bool,
    delete_worktree: bool,
) -> None:
    """Resolve a task-id collision: with --rm remove the existing task, else raise.

    `delete_branch`/`delete_worktree` scope the removal to what gw owns for this
    source. Branch-creating sources (--linear/--branch-name/--branch-auto) own
    both. Sources that adopt an existing branch (--branch/--pr) keep the branch
    so --rm never destroys pre-existing work; --dir keeps its in-place checkout
    entirely (only the record is reset). When a worktree we'd remove has
    uncommitted changes we refuse unless `force` (--rm-force) is set, so plain
    --rm never discards in-flight work.
    """
    existing = _load_existing_task(proj, task_id)
    if existing is None:
        return
    if not rm:
        _raise_task_exists(proj, task_id)
    if delete_worktree and not force:
        dirty = dirty_worktrees(existing)
        if dirty:
            raise GoblinError(
                f"Existing task {task_id!r} has uncommitted changes in {dirty[0]}.",
                hint="Commit or stash there first, or pass --rm-force to discard it.",
            )
    destroy_task(
        proj,
        existing,
        force=force,
        delete_branches=delete_branch,
        delete_worktrees=delete_worktree,
    )
    console.print(
        f"[muted]Removed existing task {task_id!r} ({'--rm-force' if force else '--rm'}).[/]"
    )


def _source_label(
    linear: str | None,
    issue: str | None,
    branch: str | None,
    branch_name: str | None,
    branch_auto: bool,
    directory: Path | None,
    pr: str | None,
    task: Task,
) -> str:
    if linear is not None:
        return f"--linear {linear}"
    if issue is not None:
        return f"--issue {issue}"
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
    issue: str | None = typer.Option(
        None,
        "--issue",
        help="GitHub issue to work on: a number (42), owner/repo#42, or an issue URL.",
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
            "--issue, --branch-name, and --branch-auto. If the base only exists "
            "on origin, it is fetched automatically."
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
    with_project: list[str] = typer.Option(
        [],
        "--with-project",
        help="Additional registered project(s) to include in this task (repeatable). "
        "Creates a multi-repo workspace; only valid with --linear/--issue/"
        "--branch/--branch-name/--branch-auto.",
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
    rm: bool = typer.Option(
        False,
        "--rm",
        help="If a task with the same id already exists, remove it first instead of "
        "erroring. Refuses if its worktree has uncommitted changes. For --branch/--pr "
        "the existing branch is kept (only the gw worktree + record are removed); for "
        "--dir only the record is reset.",
    ),
    rm_force: bool = typer.Option(
        False,
        "--rm-force",
        help="Like --rm, but also remove an existing task whose worktree has "
        "uncommitted changes (discarding that work). Implies --rm.",
    ),
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
    research: bool = typer.Option(
        False,
        "--research",
        help="Seed a read-only research session on the ticket: investigate and "
        "report findings in the session, don't implement, don't touch "
        "GitHub/Linear/Slack. Requires --linear or --issue.",
    ),
) -> None:
    """Create a task from a source (Linear, GitHub issue or PR, branch, new branch, or dir)."""
    if prompt is not None and no_launch:
        raise GoblinError(
            "--prompt has no effect with --no-launch (no session is started).",
            hint="Drop --no-launch, or drop --prompt.",
        )
    # Hoisted above both mode blocks: when the caller asked for two modes at
    # once, that conflict is the operative error. Reporting --adversarial-review's
    # own checks first (--prompt, --agent) would send the user off changing a
    # flag that --research accepts perfectly well.
    if research and adversarial_review:
        raise GoblinError(
            "--research and --adversarial-review are mutually exclusive.",
            hint="Pass one or the other.",
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
    if research and no_launch:
        raise GoblinError(
            "--research has no effect with --no-launch (no session is started).",
            hint="Drop --no-launch, or drop --research.",
        )
    sources: list[object] = [
        s for s in (linear, issue, branch, branch_name, dir, pr) if s is not None
    ]
    if branch_auto:
        sources.append("auto")
    if len(sources) != 1:
        raise GoblinError(
            "Specify exactly one source: --linear, --issue, --branch, --branch-name, "
            "--branch-auto, --dir, or --pr.",
            hint="e.g. `gw new --branch-auto` or `gw new --issue 42`.",
        )

    # A research brief about nothing is a silent no-op, so refuse the sources
    # that attach no tracking item (ADR 0006).
    if research and linear is None and issue is None:
        raise GoblinError(
            "--research requires a tracking item to research.",
            hint="Pass --linear <ID> or --issue <ref>.",
        )

    if with_project and (dir is not None or pr is not None):
        raise GoblinError(
            "--with-project is not supported with --dir or --pr.",
            hint=(
                "Use --linear, --issue, --branch, --branch-name, or --branch-auto "
                "for multi-repo tasks."
            ),
        )

    # --rm-force implies --rm; `force` lets removal proceed past a dirty worktree.
    remove = rm or rm_force
    if linear is not None:
        task = _from_linear(linear, project, repo, title, from_, remove, rm_force)
    elif issue is not None:
        task = _from_issue(issue, project, repo, title, from_, remove, rm_force)
    elif pr is not None:
        task = _from_pr(pr, project, repo, title, remove, rm_force)
    elif dir is not None:
        task = _from_existing_dir(dir, title, remove, rm_force)
    elif branch is not None:
        proj = resolve_project(project)
        _reject_scratch_project(proj)
        task = _from_existing_branch(proj, branch, title, remove, rm_force)
    else:
        proj = resolve_project(project)
        _reject_scratch_project(proj)
        generated = random_branch_name(proj.name) if branch_auto else None
        chosen = branch_name if branch_name is not None else generated
        assert chosen is not None
        task = _from_new_branch(proj, chosen, from_, title, remove, rm_force)

    proj = state.get_project(task.project)
    state.save_task(proj, task)

    if with_project:
        task = _attach_secondaries(proj, task, with_project, from_)

    cfg = config.load()
    agent_name = agent or (cfg.defaults.agent) or "claude"
    windowing_mode = windowing or cfg.defaults.windowing
    unsafe_mode = cfg.defaults.unsafe if unsafe is None else unsafe
    source_label = _source_label(linear, issue, branch, branch_name, branch_auto, dir, pr, task)

    print_success(f"Created task {task.id!r} on branch {task.branch!r}")
    settings: list[tuple[str, str]] = [
        ("project", proj.name),
        ("source", source_label),
        ("branch", task.branch),
        ("base", task.base_branch),
        ("worktree", str(task.worktree_path)),
    ]
    if task.is_multi_repo:
        settings.append(("workspace", str(task.workspace_path)))
        for r in task.secondary_repos:
            settings.append((f"+ {r.project}", f"{r.branch} → {r.worktree_path}"))
    if title:
        settings.append(("title", title))
    if task.linear is not None:
        settings.append(("linear", f"{task.linear.identifier}: {task.linear.title}"))
    if task.github_issue is not None:
        settings.append(("issue", f"{task.github_issue.reference}: {task.github_issue.title}"))
        settings.append(("issue url", task.github_issue.url))
    if pr is not None and task.pr_url:
        settings.append(("pr", task.pr_url))
    settings += [
        ("agent", agent_name),
        ("windowing", windowing_mode),
        ("unsafe", str(unsafe_mode).lower()),
        ("no_launch", str(no_launch).lower()),
        ("research", str(research).lower()),
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
        else build_seed_prompt(task, user_prompt=prompt, research=research)
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


def _attach_secondaries(proj: Project, task: Task, names: list[str], from_: str | None) -> Task:
    """Add each `--with-project` repo to `task`, persisting after every step.

    Validates all names before touching the filesystem. Saves progressively so
    a mid-assembly failure leaves a consistent task (the repos attached so far)
    rather than a half-moved worktree; the failure propagates so no launch
    happens on an incomplete task.
    """
    secondaries: list[Project] = []
    for raw in names:
        sp = state.get_project(raw.strip().lower())
        _reject_scratch_project(sp)
        if sp.name == task.project or any(s.name == sp.name for s in secondaries):
            raise GoblinError(
                f"Project {sp.name!r} is listed more than once for this task.",
                hint="Each project can join a task at most once.",
            )
        secondaries.append(sp)

    task = workspace.promote_to_workspace(task)
    state.save_task(proj, task)
    for sp in secondaries:
        task = workspace.attach_repo(task, sp, from_=from_)
        state.save_task(proj, task)
    return task


def _from_new_branch(
    proj: Project, branch_name: str, from_: str | None, title: str | None, rm: bool, force: bool
) -> Task:
    del title  # only relevant to seed prompt (Phase 5)
    base = from_ or proj.default_branch
    _refresh_base(proj.root, base)
    natural_branch = f"{proj.branch_prefix}{branch_name}"
    if rm:
        # Free the natural branch name first so the recreated task reuses it
        # instead of bumping to -2/-3/... . Without --rm we keep the existing
        # behavior: a same-name re-run creates a sibling on the next free name.
        _handle_existing(
            proj,
            _task_id_from_branch(natural_branch),
            rm=True,
            force=force,
            delete_branch=True,
            delete_worktree=True,
        )
    final_branch = _ensure_unique_branch(proj.root, natural_branch)
    task_id = _task_id_from_branch(final_branch)
    # Branch uniqueness doesn't guarantee task-id uniqueness: ids are slugs
    # truncated to 60 chars, so two long branch names (or `a/b` vs `a-b`) can
    # collide. Without this check the new record would silently overwrite the
    # existing task.
    if _load_existing_task(proj, task_id) is not None:
        _raise_task_exists(proj, task_id)
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


def _from_existing_branch(
    proj: Project, branch: str, title: str | None, rm: bool, force: bool
) -> Task:
    del title  # not used for branch naming; only relevant to prompt seed (Phase 5)
    repo = proj.root
    task_id = _task_id_from_branch(branch)
    # Adopting an existing branch: --rm clears the worktree + record but keeps
    # the branch (we didn't create it).
    _handle_existing(proj, task_id, rm=rm, force=force, delete_branch=False, delete_worktree=True)

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


def _parse_pr_url(pr: str) -> str | None:
    """Return the lowercased `owner/repo` from a GitHub PR URL, else None.

    None means `pr` is a bare number (or otherwise not a PR URL) and the
    project must be resolved via `--project` or the picker.
    """
    m = _PR_URL_RE.match(pr.strip())
    if m is None:
        return None
    return m.group("repo").removesuffix(".git").lower()


def _project_for_repo(owner_repo: str) -> Project | None:
    """Find a registered project whose remote resolves to `owner/repo`."""
    needle = owner_repo.lower()
    for name in state.load_global().projects:
        try:
            proj = state.get_project(name)
        except ProjectNotFoundError:
            continue
        if gh.normalize_repo(proj.repo_url) == needle:
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
    rm: bool,
    force: bool,
) -> Task:
    del repo_url, title  # PR title/branch supersede; auto-clone is out of scope.
    proj = _resolve_pr_project(pr, project_override)
    _reject_scratch_project(proj)
    info = gh.pr_view(pr, cwd=proj.root)

    task_id = _task_id_from_branch(info.head_ref)
    # The PR head branch pre-exists (or was fetched): --rm keeps it, clearing
    # only the worktree + record.
    _handle_existing(proj, task_id, rm=rm, force=force, delete_branch=False, delete_worktree=True)

    if info.is_cross_repository:
        # A fork PR's head branch doesn't exist on origin, but GitHub exposes
        # it under `refs/pull/<N>/head` — fetch that into a local branch.
        # Pushing back to the fork is out of scope; this is a review checkout.
        console.print(
            f"[muted]PR #{info.number} is from a fork; fetching pull/{info.number}/head…[/]"
        )
        git.fetch_pr_head(proj.root, info.number, info.head_ref)
    else:
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
    return _clone_and_register_project(name, repo_url, team_key=team_key.upper())


def _from_linear(
    linear_id: str,
    project_override: str | None,
    repo_url: str | None,
    title: str | None,
    from_: str | None,
    rm: bool,
    force: bool,
) -> Task:
    del title  # Linear's title supersedes
    parse_identifier(linear_id)  # validates the form; we use the fetched issue's team
    api_key = secrets.get_linear_api_key()
    with LinearClient(api_key) as client:
        issue = client.fetch_issue(linear_id)

    proj = _resolve_or_register_linear_project(issue.team_key, project_override, repo_url)
    _reject_scratch_project(proj)

    task_id = issue.identifier.lower()
    _handle_existing(proj, task_id, rm=rm, force=force, delete_branch=True, delete_worktree=True)

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


def _project_for_cwd() -> Project | None:
    """The registered project containing the cwd, or None when outside one."""
    try:
        return _find_project_containing(Path.cwd())
    except GoblinError:
        return None


def _clone_and_register_project(name: str, repo_url: str, team_key: str | None = None) -> Project:
    """Clone `repo_url` into `~/goblin/<name>/` and register it as a project."""
    if name in state.load_global().projects:
        raise GoblinError(
            f"A project named {name!r} is already registered.",
            hint=f"Pass --project {name} to use it, or --project <other> to pick another.",
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
        linear_team_key=team_key,
        created_at=_now(),
    )
    state.register_project(project)
    git.add_to_local_exclude(root, ".goblin/")
    git.add_to_local_exclude(root, ".worktrees/")
    return project


def _project_for_repo_url(repo_url: str) -> Project:
    """The registered project for `repo_url`, cloning and registering it if new.

    The project name comes from the *URL's* repo, never the issue's — with a
    cross-repo tracking issue those differ, and `--repo` names where the work
    happens.
    """
    normalized = gh.normalize_repo(repo_url)
    if normalized is not None:
        existing = _project_for_repo(normalized)
        if existing is not None:
            return existing
        name = slugify(normalized.split("/")[-1])
    else:
        # Not a GitHub URL (a local path, another host): fall back to the last
        # path segment, the same shape `git clone` would give the directory.
        name = slugify(repo_url.rstrip("/").split("/")[-1].removesuffix(".git"))
    return _clone_and_register_project(name, repo_url)


def _resolve_issue_project(
    ref: gh.IssueRef, project_override: str | None, repo_url: str | None
) -> Project:
    """Resolve the project a `--issue` task works in.

    Precedence: `--project` → the issue's own repo matched against registered
    projects → the project containing the cwd → `--repo` (reuses a registered
    project for that URL, else clones + registers).

    The cwd fallback is what makes the cross-repo form usable: when the tracking
    issue lives in another repository there is nothing in the reference that
    names the repo to *work* in, so the surrounding worktree decides. A bare
    number has no repo of its own at all, so it resolves through the cwd, then
    `--repo`, then the project picker — all before the issue is even fetched,
    since `gh issue view 42` is meaningless without a repo.
    """
    if project_override:
        return state.get_project(project_override.strip().lower())

    if ref.repo is None:
        here = _project_for_cwd()
        if here is not None:
            return here
        if repo_url is not None:
            return _project_for_repo_url(repo_url)
        return resolve_project(None)

    matched = _project_for_repo(ref.repo)
    if matched is not None:
        return matched

    here = _project_for_cwd()
    if here is not None:
        console.print(
            f"[muted]Issue {ref.repo}#{ref.number} lives outside this project; "
            f"working in {here.name} (from the current directory).[/]"
        )
        return here

    if repo_url is not None:
        return _project_for_repo_url(repo_url)

    raise GoblinError(
        f"No registered project matches the issue's repo {ref.repo!r}.",
        hint=(
            "Pass --project <name> to say which repo the work happens in, run from "
            "inside that project's worktree, or pass --repo <url> to clone and "
            "register it."
        ),
    )


def _from_issue(
    issue_ref: str,
    project_override: str | None,
    repo_url: str | None,
    title: str | None,
    from_: str | None,
    rm: bool,
    force: bool,
) -> Task:
    del title  # the issue's title supersedes
    ref = gh.parse_issue_ref(issue_ref)
    proj = _resolve_issue_project(ref, project_override, repo_url)
    _reject_scratch_project(proj)
    info = gh.issue_view(ref, cwd=proj.root)

    # `gh-<number>` is unique per repo, not globally: a cross-repo issue #42
    # can collide with the same-repo #42 already tracked here. We let the
    # existing "task already exists" path refuse rather than inventing a
    # disambiguating id for a collision that hasn't shown up in practice.
    task_id = f"gh-{info.number}"
    _handle_existing(proj, task_id, rm=rm, force=force, delete_branch=True, delete_worktree=True)

    base = from_ or proj.default_branch
    _refresh_base(proj.root, base)
    branch = _ensure_unique_branch(
        proj.root, branch_slug(task_id, info.title, prefix=proj.branch_prefix)
    )
    worktree_dir = paths.worktree_root(proj.root, proj.worktree_root) / task_id
    if not worktree_dir.exists():
        git.worktree_add(proj.root, worktree_dir, branch, base=base)
    return Task(
        id=task_id,
        project=proj.name,
        github_issue=GhIssue(
            number=info.number,
            repo=info.repo,
            title=info.title,
            body=info.body or None,
            state=info.state,
            url=info.url,
            labels=list(info.labels),
            assignees=list(info.assignees),
        ),
        github_issue_state_updated_at=_now(),
        branch=branch,
        worktree_path=worktree_dir,
        base_branch=base,
        created_at=_now(),
    )


def _clone_linear_issue(issue: LinearIssue) -> LinearIssue:
    # We persist the issue as-of-creation; identical fields, but a fresh object so future
    # mutations of the source don't leak into the persisted snapshot.
    return LinearIssue.model_validate(issue.model_dump())


def _from_existing_dir(directory: Path, title: str | None, rm: bool, force: bool) -> Task:
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
    _reject_scratch_project(proj)
    branch_here = git.current_branch(directory)
    task_id = _task_id_from_branch(branch_here)
    # The directory is adopted in place — --rm resets only the record, leaving
    # the user's checkout and branch untouched.
    _handle_existing(proj, task_id, rm=rm, force=force, delete_branch=False, delete_worktree=False)
    return Task(
        id=task_id,
        project=proj.name,
        branch=branch_here,
        worktree_path=directory,
        base_branch=proj.default_branch,
        created_at=_now(),
    )
