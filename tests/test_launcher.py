import subprocess
from datetime import UTC, datetime
from pathlib import Path

from typer.testing import CliRunner

from goblin_watcher import state
from goblin_watcher.agents.base import RawSession, TranscriptSummary
from goblin_watcher.agents.launcher import Fresh, Resume, launch
from goblin_watcher.cli import app
from goblin_watcher.models import Task
from goblin_watcher.review_feed import ReviewFeed


class _StubAgent:
    name = "claude"
    binary = "stub"

    def __init__(self, captured_id: str | None = None, preassign_id: str | None = None) -> None:
        self._captured = captured_id
        self._preassign = preassign_id
        self.capture_calls = 0
        self.spawn_session_ids: list[str | None] = []

    def spawn_command(
        self, *, prompt: str, cwd: Path, unsafe: bool = False, session_id: str | None = None
    ) -> list[str]:
        del prompt, cwd, unsafe
        self.spawn_session_ids.append(session_id)
        return ["stub", "spawn"]

    def new_session_id(self) -> str | None:
        return self._preassign

    def resume_command(
        self, *, session_id: str | None, cwd: Path, unsafe: bool = False
    ) -> list[str]:
        del session_id, cwd, unsafe
        return ["stub", "resume"]

    def env(self) -> dict[str, str]:
        return {}

    def capture_session_id(self, cwd: Path) -> str | None:
        del cwd
        self.capture_calls += 1
        return self._captured

    def list_sessions(self, cwd: Path) -> list[RawSession]:
        del cwd
        return []

    def read_transcript(self, session_id: str, cwd: Path) -> TranscriptSummary:
        del session_id, cwd
        return TranscriptSummary()


class _AsyncWindower:
    """Mimics tmux: returns immediately after dispatch, without running the agent."""

    name = "tmux"

    def __init__(self) -> None:
        self.observed_task: Task | None = None
        self.observed_session_id: str | None = None

    def run(
        self,
        *,
        task: Task,
        cmd: list[str],
        cwd: Path,
        env: dict[str, str],
        session_id: str | None = None,
    ) -> int:
        del cmd, cwd, env
        self.observed_task = task
        self.observed_session_id = session_id
        return 0

    def is_live(self, task: Task) -> bool:
        del task
        return False


class _InlineWindower:
    name = "inline"

    def run(
        self,
        *,
        task: Task,
        cmd: list[str],
        cwd: Path,
        env: dict[str, str],
        session_id: str | None = None,
    ) -> int:
        del task, cmd, cwd, env, session_id
        return 0

    def is_live(self, task: Task) -> bool:
        del task
        return False


def _init_repo(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", "-b", "main", str(path)], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.name", "tester"], check=True)
    (path / "README.md").write_text("hi")
    subprocess.run(["git", "-C", str(path), "add", "."], check=True)
    subprocess.run(["git", "-C", str(path), "commit", "-qm", "init"], check=True)


def _bootstrap(tmp_path: Path) -> Task:
    repo = tmp_path / "alpha"
    _init_repo(repo)
    runner = CliRunner()
    runner.invoke(app, ["project", "new", "alpha", "--dir", str(repo)])
    runner.invoke(app, ["new", "--branch-name", "spike/foo", "--no-launch"])
    proj = state.get_project("alpha")
    [task] = state.list_tasks(proj)
    return task


def test_async_windower_persists_session_before_dispatch(
    isolated_xdg: Path, tmp_path: Path
) -> None:
    """Tmux mode can `execvp` away before `capture_session_id` runs. The record
    must therefore be saved before the windower hands off the agent."""
    task = _bootstrap(tmp_path)
    proj = state.get_project("alpha")
    assert task.sessions == []

    windower = _AsyncWindower()
    _, returned = launch(
        project=proj,
        task=task,
        agent=_StubAgent(captured_id=None),
        choice=Fresh(prompt="kick off"),
        windower=windower,
    )
    # The windower observed the task with the record already attached.
    assert windower.observed_task is not None
    assert len(windower.observed_task.sessions) == 1
    # And the record survived to the persisted state.
    [persisted] = state.list_tasks(proj)
    assert len(persisted.sessions) == 1
    record = persisted.sessions[0]
    assert record.agent == "claude"
    assert record.session_id  # synthesized; just must be non-empty
    assert record.label == "kick off"
    # The returned task matches what we persisted.
    assert returned.sessions == persisted.sessions
    # And the windower was told which session it is hosting, so a pane-based
    # windower can label it for `gw session send`.
    assert windower.observed_session_id == record.session_id


def test_preassigned_id_survives_async_windower(isolated_xdg: Path, tmp_path: Path) -> None:
    """Agents that accept a caller-chosen session id (claude --session-id) must
    have that exact id recorded even when the windower (tmux) detaches before
    the transcript exists — this was the bug that left placeholder ids in
    state, making every later `gw run` resume fail."""
    task = _bootstrap(tmp_path)
    proj = state.get_project("alpha")

    agent = _StubAgent(preassign_id="11111111-2222-4333-8444-555555555555")
    launch(
        project=proj,
        task=task,
        agent=agent,
        choice=Fresh(prompt="kick off"),
        windower=_AsyncWindower(),
    )
    # The id was handed to spawn_command and stored verbatim.
    assert agent.spawn_session_ids == ["11111111-2222-4333-8444-555555555555"]
    [persisted] = state.list_tasks(proj)
    assert [s.session_id for s in persisted.sessions] == ["11111111-2222-4333-8444-555555555555"]


def test_preassigned_id_skips_capture_on_inline(isolated_xdg: Path, tmp_path: Path) -> None:
    """With a preassigned id there is nothing to capture; capturing anyway
    could grab an older transcript when the agent exits without writing one."""
    task = _bootstrap(tmp_path)
    proj = state.get_project("alpha")

    agent = _StubAgent(captured_id="stale-other-session", preassign_id="pre-id")
    _, returned = launch(
        project=proj,
        task=task,
        agent=agent,
        choice=Fresh(prompt="kick off"),
        windower=_InlineWindower(),
    )
    assert agent.capture_calls == 0
    assert [s.session_id for s in returned.sessions] == ["pre-id"]


def test_inline_replaces_synthetic_id_with_captured_id(isolated_xdg: Path, tmp_path: Path) -> None:
    """Inline mode lets us read the agent's real session id post-launch; the
    synthetic placeholder we pre-saved should be swapped out."""
    task = _bootstrap(tmp_path)
    proj = state.get_project("alpha")

    real_id = "real-id-from-disk"
    _, returned = launch(
        project=proj,
        task=task,
        agent=_StubAgent(captured_id=real_id),
        choice=Fresh(prompt="kick off"),
        windower=_InlineWindower(),
    )
    assert [s.session_id for s in returned.sessions] == [real_id]
    [persisted] = state.list_tasks(proj)
    assert [s.session_id for s in persisted.sessions] == [real_id]


def test_inline_resume_adds_record_when_capture_diverges(
    isolated_xdg: Path, tmp_path: Path
) -> None:
    """Resuming an existing session is unaffected when capture returns the same
    id; if the agent forks into a new transcript, we add the new record
    alongside the resumed one rather than dropping either."""
    task = _bootstrap(tmp_path)
    proj = state.get_project("alpha")
    seed = task.model_copy(
        update={
            "sessions": [
                _stub_record("resumed-id", "earlier"),
            ]
        }
    )
    state.save_task(proj, seed)

    # Capture returns a different id → new record appended.
    _, returned = launch(
        project=proj,
        task=seed,
        agent=_StubAgent(captured_id="forked-id"),
        choice=Resume(session_id="resumed-id"),
        windower=_InlineWindower(),
    )
    ids = sorted(s.session_id for s in returned.sessions)
    assert ids == ["forked-id", "resumed-id"]


def test_build_seed_prompt_no_user_prompt_uses_default_trailer(
    isolated_xdg: Path, tmp_path: Path
) -> None:
    from goblin_watcher.agents.launcher import build_seed_prompt

    task = _bootstrap(tmp_path)
    seed = build_seed_prompt(task)
    assert "Wait for my next message before taking any action." in seed
    assert "do not begin working" in seed


def test_build_seed_prompt_with_user_prompt_replaces_trailer(
    isolated_xdg: Path, tmp_path: Path
) -> None:
    from goblin_watcher.agents.launcher import build_seed_prompt

    task = _bootstrap(tmp_path)
    seed = build_seed_prompt(task, user_prompt="Refactor the foo module.")
    assert "Wait for my next message" not in seed
    assert "do not begin working" not in seed
    assert "Refactor the foo module." in seed
    # User prompt appears after the PR-open instruction.
    assert seed.index("Refactor the foo module.") > seed.index("open a PR via")


def test_build_seed_prompt_blank_user_prompt_treated_as_none(
    isolated_xdg: Path, tmp_path: Path
) -> None:
    from goblin_watcher.agents.launcher import build_seed_prompt

    task = _bootstrap(tmp_path)
    seed = build_seed_prompt(task, user_prompt="   ")
    assert "Wait for my next message before taking any action." in seed


def test_build_seed_prompt_single_repo_unchanged(isolated_xdg: Path, tmp_path: Path) -> None:
    from goblin_watcher.agents.launcher import build_seed_prompt

    task = _bootstrap(tmp_path)
    seed = build_seed_prompt(task)
    assert f"Branch: {task.branch} (off {task.base_branch})" in seed
    assert f"Worktree: {task.worktree_path}" in seed
    assert "spans" not in seed


def test_build_seed_prompt_multi_repo_lists_every_repo(isolated_xdg: Path, tmp_path: Path) -> None:
    from goblin_watcher.agents.launcher import build_seed_prompt
    from goblin_watcher.models import TaskRepo

    task = _bootstrap(tmp_path)
    multi = task.model_copy(
        update={
            "workspace_path": Path("/ws"),
            "worktree_path": Path("/ws/alpha"),
            "secondary_repos": [
                TaskRepo(
                    project="beta",
                    branch=task.branch,
                    worktree_path=Path("/ws/beta"),
                    base_branch="main",
                )
            ],
        }
    )
    seed = build_seed_prompt(multi)
    assert "spans 2 repositories" in seed
    assert "Workspace: /ws" in seed
    assert "- alpha: /ws/alpha" in seed
    assert "- beta: /ws/beta" in seed


def _stub_record(session_id: str, label: str | None):
    from goblin_watcher.models import SessionRecord

    return SessionRecord(
        agent="claude",
        session_id=session_id,
        created_at=datetime.now(UTC),
        last_used_at=datetime.now(UTC),
        label=label,
    )


class _DeletingWindower:
    """Mimics the task being removed while the agent runs (`gw task rm`, sync prune)."""

    name = "inline"

    def run(
        self,
        *,
        task: Task,
        cmd: list[str],
        cwd: Path,
        env: dict[str, str],
        session_id: str | None = None,
    ) -> int:
        del cmd, cwd, env, session_id
        state.delete_task_record(state.get_project(task.project), task.id)
        return 0

    def is_live(self, task: Task) -> bool:
        del task
        return False


def test_post_run_write_does_not_resurrect_a_deleted_task(
    isolated_xdg: Path, tmp_path: Path
) -> None:
    """A task removed mid-session must stay removed.

    `gw sync` prunes merged-and-clean tasks unattended (ADR 0005), so a
    long-running agent finishing after its task was pruned would otherwise write
    the record back — pointing at a worktree and branch that no longer exist.
    """
    task = _bootstrap(tmp_path)
    proj = state.get_project("alpha")

    exit_code, returned = launch(
        project=proj,
        task=task,
        agent=_StubAgent(captured_id="real-id"),
        choice=Fresh(prompt="kick off"),
        windower=_DeletingWindower(),
    )

    assert exit_code == 0
    # Caller still gets a usable task to render...
    assert [s.session_id for s in returned.sessions] == ["real-id"]
    # ...but nothing was written back to disk.
    assert state.list_tasks(proj) == []


def test_build_seed_prompt_uses_github_issue_context(isolated_xdg: Path, tmp_path: Path) -> None:
    from goblin_watcher.agents.launcher import build_seed_prompt
    from goblin_watcher.models import GhIssue

    task = _bootstrap(tmp_path).model_copy(
        update={
            "github_issue": GhIssue(
                number=42,
                repo="org/repo",
                title="Add rate limit",
                body="We need a token bucket on the ingest path.",
                state="OPEN",
                url="https://github.com/org/repo/issues/42",
                labels=["enhancement"],
            )
        }
    )
    seed = build_seed_prompt(task)
    # Headline is the qualified reference plus the issue title.
    assert "org/repo#42: Add rate limit" in seed
    assert "GitHub issue org/repo#42 (open): https://github.com/org/repo/issues/42" in seed
    assert "Labels: enhancement" in seed
    assert "We need a token bucket on the ingest path." in seed
    assert "no Linear issue" not in seed


def test_build_seed_prompt_github_issue_without_body(isolated_xdg: Path, tmp_path: Path) -> None:
    from goblin_watcher.agents.launcher import build_seed_prompt
    from goblin_watcher.models import GhIssue

    task = _bootstrap(tmp_path).model_copy(
        update={
            "github_issue": GhIssue(
                number=7,
                repo="org/repo",
                title="Fix it",
                state="CLOSED",
                url="https://github.com/org/repo/issues/7",
            )
        }
    )
    seed = build_seed_prompt(task)
    assert "(The issue has no description.)" in seed


def _flat(text: str) -> str:
    """Collapse the brief's hard wrapping so assertions read as sentences.

    Without this a test fails whenever a paragraph is re-wrapped, which says
    nothing about whether the instruction is still there.
    """
    return " ".join(text.split())


def _issue_backed_task(tmp_path: Path) -> Task:
    from goblin_watcher.models import GhIssue

    return _bootstrap(tmp_path).model_copy(
        update={
            "github_issue": GhIssue(
                number=11,
                repo="org/repo",
                title="Add a research option",
                body="Spawn the agent to investigate the ticket and report back.",
                state="OPEN",
                url="https://github.com/org/repo/issues/11",
            )
        }
    )


def test_research_prompt_keeps_the_ticket_context(isolated_xdg: Path, tmp_path: Path) -> None:
    """A research brief is the work brief's context with different standing
    instructions — the ticket is the whole input, so it must survive (ADR 0006)."""
    from goblin_watcher.agents.launcher import build_seed_prompt

    task = _issue_backed_task(tmp_path)
    seed = build_seed_prompt(task, research=True)
    assert "org/repo#11: Add a research option" in seed
    assert f"Branch: {task.branch} (off {task.base_branch})" in seed
    assert f"Worktree: {task.worktree_path}" in seed
    assert "Spawn the agent to investigate the ticket and report back." in seed
    assert "{focus}" not in seed


def test_research_prompt_does_not_instruct_to_open_a_pr(isolated_xdg: Path, tmp_path: Path) -> None:
    """The one instruction that must not appear: the work template's standing
    "open a PR" line, which no --prompt value can suppress."""
    from goblin_watcher.agents.launcher import build_seed_prompt

    task = _issue_backed_task(tmp_path)
    seed = build_seed_prompt(task, research=True)
    assert "open a PR via" not in seed
    assert "When this task is ready for review" not in seed
    # Sanity: the default template still carries it.
    assert "open a PR via `gw pr open`" in build_seed_prompt(task)


def test_research_prompt_names_the_prohibitions(isolated_xdg: Path, tmp_path: Path) -> None:
    """Every boundary ADR 0006 asks the brief to draw, named explicitly rather
    than by category — dropping any one of them is a silent regression."""
    from goblin_watcher.agents.launcher import build_seed_prompt

    seed = _flat(build_seed_prompt(_issue_backed_task(tmp_path), research=True))
    assert "Do not:" in seed
    # Scoped to the prohibition sentence, so a phrase that happens to occur in
    # the ticket body can't satisfy the assertion.
    prohibited = seed.split("Do not:", 1)[1].split("Report your findings", 1)[0]
    for phrase in (
        "push",
        "commit",
        "open or comment on a pull request",
        "run `gw pr open`",
        "comment on, assign, or transition the Linear ticket or the GitHub issue",
        "post to Slack or any other external service",
        "modify this project's source",
    ):
        assert phrase in prohibited, phrase


def test_research_prompt_reports_in_session_not_to_a_file(
    isolated_xdg: Path, tmp_path: Path
) -> None:
    from goblin_watcher.agents.launcher import build_seed_prompt

    seed = _flat(build_seed_prompt(_issue_backed_task(tmp_path), research=True))
    assert "Report your findings here, in this session" in seed
    assert "do not write them to a file" in seed


def test_research_prompt_opens_with_the_research_marker(isolated_xdg: Path, tmp_path: Path) -> None:
    """`_label_from_prompt` takes the first 80 chars for the session label, so a
    research session is recognizable in the picker (ADR 0006's mitigation)."""
    from goblin_watcher.agents.launcher import _label_from_prompt, build_seed_prompt

    seed = build_seed_prompt(_issue_backed_task(tmp_path), research=True)
    assert seed.startswith("Research task —")
    assert _label_from_prompt(seed).startswith("Research task — investigate")


def test_research_prompt_user_prompt_becomes_a_focus(isolated_xdg: Path, tmp_path: Path) -> None:
    from goblin_watcher.agents.launcher import build_seed_prompt

    task = _issue_backed_task(tmp_path)
    seed = build_seed_prompt(task, user_prompt="Only the sync path.", research=True)
    assert "Focus this research on the following" in seed
    assert "Only the sync path." in seed
    # The focus narrows the brief; it doesn't replace the constraints.
    assert seed.index("Only the sync path.") > seed.index("run `gw pr open`")


def test_research_prompt_without_user_prompt_has_no_focus(
    isolated_xdg: Path, tmp_path: Path
) -> None:
    from goblin_watcher.agents.launcher import build_seed_prompt

    task = _issue_backed_task(tmp_path)
    assert "Focus this research on" not in build_seed_prompt(task, research=True)
    # Whitespace-only is treated as absent, as in the default template.
    assert "Focus this research on" not in build_seed_prompt(task, user_prompt="  ", research=True)


def test_research_prompt_keeps_the_prompt_addition(isolated_xdg: Path, tmp_path: Path) -> None:
    from goblin_watcher import prompt_addition
    from goblin_watcher.agents.launcher import build_seed_prompt

    task = _issue_backed_task(tmp_path)
    prompt_addition.save_global("Always run `just verify`.")
    seed = build_seed_prompt(task, research=True)
    assert "Always run `just verify`." in seed
    # Sits before the constraints, so those read as the operative instruction.
    assert seed.index("Always run") < seed.index("Do not: push")


def test_build_seed_prompt_without_any_ticket_says_so(isolated_xdg: Path, tmp_path: Path) -> None:
    from goblin_watcher.agents.launcher import build_seed_prompt

    seed = build_seed_prompt(_bootstrap(tmp_path))
    assert "(no Linear issue or GitHub issue attached — fresh task)" in seed
    assert "SPIKE-FOO" in seed


def _feed(**over: object) -> ReviewFeed:
    """A one-repo review feed with one unresolved thread and one failing check."""
    from goblin_watcher import gh
    from goblin_watcher.review_feed import RepoReview, ReviewFeed

    defaults: dict[str, object] = {
        "number": 34,
        "title": "Add --address-review",
        "state": "OPEN",
        "url": "https://github.com/org/repo/pull/34",
        "threads": (
            gh.ReviewThread(
                path="src/goblin_watcher/gh.py",
                line=120,
                is_outdated=False,
                diff_hunk="@@ -118,3 +118,3 @@\n-    return None\n+    return out",
                comments=(
                    gh.ReviewComment(
                        author="alice",
                        body="This swallows the error.",
                        created_at="2026-08-13T10:00:00Z",
                    ),
                ),
            ),
        ),
        "summaries": (
            gh.ReviewSummary(
                author="bob",
                state="CHANGES_REQUESTED",
                body="Two things before this lands.",
                submitted_at="2026-08-13T11:00:00Z",
            ),
        ),
        "failing": (
            gh.FailingCheck(
                name="verify",
                conclusion="FAILURE",
                details_url="https://github.com/org/repo/actions/runs/1/job/2",
                log="E   assert 1 == 2",
            ),
        ),
    }
    review = gh.PrReview(**{**defaults, **over})  # type: ignore[arg-type]
    return ReviewFeed(repos=(RepoReview(project="alpha", review=review),))


def test_address_review_prompt_carries_the_feedback(isolated_xdg: Path, tmp_path: Path) -> None:
    """The whole point of the mode: the agent starts from what was said, not
    from an instruction to go find it (ADR 0007)."""
    from goblin_watcher.agents.launcher import build_seed_prompt

    seed = build_seed_prompt(_issue_backed_task(tmp_path), review=_feed())
    assert seed.startswith("Address the review feedback on this pull request.")
    # Task context survives, exactly as it does for the other two briefs.
    assert "org/repo#11: Add a research option" in seed
    assert "PR #34 (OPEN): https://github.com/org/repo/pull/34" in seed
    assert "Unresolved review threads (1):" in seed
    assert "[1] src/goblin_watcher/gh.py:120" in seed
    assert "This swallows the error." in seed
    assert "Review summaries:" in seed
    assert "Two things before this lands." in seed
    assert "Failing checks (1):" in seed
    assert "E   assert 1 == 2" in seed
    assert "{" not in seed.split("```")[0]  # every template slot was filled


def test_address_review_prompt_marks_outdated_threads(isolated_xdg: Path, tmp_path: Path) -> None:
    """An outdated hunk is stale evidence — the agent has to re-read the code
    rather than trust what the thread quotes."""
    from goblin_watcher import gh
    from goblin_watcher.agents.launcher import build_seed_prompt

    stale = gh.ReviewThread(
        path="src/gh.py",
        line=None,
        is_outdated=True,
        diff_hunk="",
        comments=(gh.ReviewComment(author="alice", body="Still wrong.", created_at=""),),
    )
    seed = build_seed_prompt(_issue_backed_task(tmp_path), review=_feed(threads=(stale,)))
    assert "[1] src/gh.py  (outdated — the diff has moved since this was written)" in seed
    # A thread GitHub reports without a line renders the bare path, never
    # `src/gh.py:None`.
    assert "src/gh.py:" not in seed
    # An empty hunk contributes no fence at all rather than an empty one.
    assert "```diff" not in seed


def test_address_review_prompt_shows_a_url_when_it_has_no_log(
    isolated_xdg: Path, tmp_path: Path
) -> None:
    """A third-party status context has no fetchable log; saying so beats
    rendering an empty code fence that reads as "CI printed nothing"."""
    from goblin_watcher import gh
    from goblin_watcher.agents.launcher import build_seed_prompt

    check = gh.FailingCheck(
        name="circleci", conclusion="FAILURE", details_url="https://circleci.com/b/1"
    )
    seed = build_seed_prompt(_issue_backed_task(tmp_path), review=_feed(failing=(check,)))
    assert "https://circleci.com/b/1" in seed
    assert "(no log available — open the URL above to read it)" in seed


def test_address_review_prompt_names_the_repo_only_when_multi_repo(
    isolated_xdg: Path, tmp_path: Path
) -> None:
    """The same comment text against two checkouts is ambiguous without a label,
    and noise with one."""
    from goblin_watcher.agents.launcher import build_seed_prompt
    from goblin_watcher.review_feed import RepoReview, ReviewFeed

    task = _issue_backed_task(tmp_path)
    single = _feed()
    assert "[alpha] PR #34" not in build_seed_prompt(task, review=single)

    pair = ReviewFeed(
        repos=(*single.repos, RepoReview(project="beta", review=single.repos[0].review))
    )
    seed = build_seed_prompt(task, review=pair)
    assert "[alpha] PR #34" in seed
    assert "[beta] PR #34" in seed


def test_address_review_prompt_does_not_ask_for_github_writes(
    isolated_xdg: Path, tmp_path: Path
) -> None:
    """gw's house style keeps external writes behind explicit flags; the agent
    reports in-session and pushes code, nothing more (ADR 0007)."""
    from goblin_watcher.agents.launcher import build_seed_prompt

    seed = _flat(build_seed_prompt(_issue_backed_task(tmp_path), review=_feed()))
    assert "Do not reply to, resolve, or otherwise write to the review threads on GitHub" in seed
    assert "do not comment on the Linear ticket or the GitHub issue" in seed
    assert "Report here, in this session, what you changed" in seed
    # Pushing the fix *is* the point, so the PR instruction stays — as a push,
    # not as "open a second PR".
    assert "push them with `gw pr open`" in seed


def test_address_review_prompt_narrows_with_a_user_prompt(
    isolated_xdg: Path, tmp_path: Path
) -> None:
    """--prompt composes here as it does with --research: it focuses the work
    instead of replacing the brief's trailer."""
    from goblin_watcher.agents.launcher import build_seed_prompt

    task = _issue_backed_task(tmp_path)
    seed = build_seed_prompt(task, "Just the gh.py thread.", review=_feed())
    assert "Focus on the following in particular:" in seed
    assert "Just the gh.py thread." in seed
    # Absent prompt renders nothing at all, not an empty heading.
    assert "Focus on the following" not in build_seed_prompt(task, review=_feed())


def test_address_review_session_is_recognizable_in_the_picker(
    isolated_xdg: Path, tmp_path: Path
) -> None:
    """`_label_from_prompt` takes the first 80 chars, so the mode shows up in
    the session list without anything being persisted on the Task."""
    from goblin_watcher.agents.launcher import _label_from_prompt, build_seed_prompt

    seed = build_seed_prompt(_issue_backed_task(tmp_path), review=_feed())
    assert _label_from_prompt(seed).startswith("Address the review feedback")


def test_address_review_prompt_survives_backticks_in_the_evidence(
    isolated_xdg: Path, tmp_path: Path
) -> None:
    """A diff of a markdown file — or a CI log echoing one — carries its own ```
    run. A fixed fence would close there and turn the rest into prose."""
    from goblin_watcher import gh
    from goblin_watcher.agents.launcher import build_seed_prompt

    hunk = "@@ -1,4 +1,4 @@\n+```python\n+x = 1\n+```"
    thread = gh.ReviewThread(
        path="README.md",
        line=4,
        is_outdated=False,
        diff_hunk=hunk,
        comments=(gh.ReviewComment(author="alice", body="Wrong language tag.", created_at=""),),
    )
    check = gh.FailingCheck(
        name="docs",
        conclusion="FAILURE",
        details_url="https://x/1",
        log="expected:\n```\nfoo\n```",
    )
    seed = build_seed_prompt(
        _issue_backed_task(tmp_path), review=_feed(threads=(thread,), failing=(check,))
    )
    assert "````diff" in seed
    assert "+```python" in seed
    # The comment after the hunk is still outside any fence.
    assert "Wrong language tag." in seed
    assert "````\nexpected:" in seed
