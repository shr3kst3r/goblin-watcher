# 0007. Address-review mode fetches the feedback rather than telling the agent to

- Status: accepted
- Date: 2026-08-13

## Context

`gw pr` has `open` and `status`. The loop that stays entirely manual is taking
review feedback back to the agent: read the comments, read the failing check's
output, paste both into a session. Issue #17 asks for
`gw run <task-id> --address-review` to close it.

ADR 0006 already settled the *shape*: a work mode that changes the agent's
standing instructions is an alternate seed template rendered through
`build_seed_prompt`, sharing the same task-context slots. That decision is not
re-litigated here. What it does not settle is where the feedback comes from,
and that is the whole question for this mode.

Three facts frame it:

**The agent could fetch it itself.** Every agent gw launches runs with `gh` on
PATH and `defaults.unsafe = true`. A three-line addition to `spawn_prompt.md`
saying "run `gh pr view --comments` and work through what you find" would cost
nothing to build. It also produces a session whose first minutes are spent
rediscovering a thing gw already knows, with no guarantee the agent reaches
`reviewThreads` at all — `gh pr view --json` does not expose it, so the agent
has to know to reach for `gh api graphql`.

**gw already holds both ends.** The PR URL is on the task record (`Task.pr_url`,
and per-repo on `TaskRepo`), and `gh.pr_checks` already talks to the checks API.
The missing piece is the *content* behind a failing check, which needs a second
call (`gh run view --log-failed`) against a job id parsed out of the check's
details URL.

**CI logs have no natural ceiling.** A failing matrix job can emit tens of
thousands of lines. Whatever we embed competes with the agent's own context for
the rest of the session, so "include the failing output" is not a decision until
it comes with a bound.

Separately: gw's house style keeps external writes behind explicit flags — the
Linear API is read-only unless `--notify-linear` is passed, and `gw pr open` is
the only command that pushes. A mode about *responding to reviewers* invites the
obvious next step of replying to them, and that would be gw's first unflagged
external write.

## Decision

`gw run --address-review` **fetches the PR's outstanding feedback at seed time
and embeds it in the brief**, rendered from a new
`templates/address_review_prompt.md` through `build_seed_prompt` (ADR 0006).
Gathering lives in a new `review_feed` module; rendering lives in
`agents/launcher` next to the other two briefs.

What "outstanding feedback" means, concretely:

- Every **unresolved review thread** — its anchor, its diff hunk, and every
  comment in the chain. Resolved threads are dropped; outdated ones are kept and
  labelled, because a stale hunk is still a claim about the code.
- The body of every review in **`CHANGES_REQUESTED` or `COMMENTED`** state. An
  `APPROVED` body is congratulation and a `DISMISSED` one was explicitly
  overruled.
- Every **failing check**, with the tail of its failing steps' log when the
  check is a GitHub Actions run and gw can fetch it.

Five constraints come with it:

1. **One GraphQL round-trip for the PR, plus one log fetch per failing check,
   capped.** `reviewThreads` is not reachable through `gh pr view --json` at all,
   so the query is hand-written. Logs cost a subprocess each, so only the first
   `MAX_LOGGED_CHECKS` get one; the rest keep their URL and lose the inline log.

2. **Everything embedded is bounded.** Logs are clipped to their last
   `MAX_LOG_LINES` / `MAX_LOG_CHARS` (a job's error is at the end), comment
   bodies to their first `MAX_COMMENT_CHARS` (a reviewer's point leads), diff
   hunks to their header plus their last lines. Every clip leaves a visible
   marker so the agent knows to open the URL for the rest.

3. **The mode requires a PR with something outstanding, and refuses loudly
   otherwise.** No PR, an unreadable PR, and a PR with every thread resolved and
   every check green are three distinct refusals with three distinct messages. A
   brief about nothing is a silent no-op. Pending checks are not failing checks.

4. **The brief forbids writing to GitHub.** The agent reports what it changed —
   and what it deliberately did not — in the session, and pushes with
   `gw pr open`, which is idempotent for an already-open PR. Replying to threads
   and resolving them stays the user's.

5. **The mode is a property of the session, not the task**, exactly as
   ADR 0006 has it. Nothing is persisted on `Task`.

Unlike `--research`, `--address-review` does **not** require a tracking item:
the input is the PR, so a `--branch`- or `--pr`-sourced task is a valid target.
`--prompt` composes, narrowing the focus rather than replacing the trailer.

## Consequences

**Easier.** The session opens already knowing what alice said on line 120 and
what the test runner printed, so the agent's first move is adjudication rather
than reconnaissance. It is agent-agnostic — codex and gemini get the same brief.
Because gw does the fetching, the shape of the feedback is stable: the agent
cannot decide to skip the checks, and a bot finding is presented identically to
a human one.

**Harder.** gw now owns a hand-written GraphQL query against a schema it does
not control; a field rename shows up as an empty section rather than an error,
because every level of the response is written to survive being null. The seed
prompt is now variable-size and can be genuinely large — a PR with twenty
threads and five failing jobs produces a wall of text, and there is no feedback
loop telling us when that stopped helping. Two subprocess round-trips also mean
`gw run --address-review` is the first `gw run` invocation that can be slow for
reasons outside the machine; it is deliberately the last thing before dispatch
so every cheap validation fails first.

**Accepted.** The feedback is a point-in-time snapshot. A comment posted while
the agent works is invisible to it, and a check that fails later never reaches
the brief — the user re-runs the command. We are not building a live feed.

**Accepted.** The "don't write to GitHub" boundary is instruction-level, like
ADR 0006's read-only boundary and for the same reason: the agent runs with
bypassed permissions and can shell out to `gh` regardless. What the mode buys is
that it is not *told* to reply.

**Accepted.** Plain PR conversation comments are excluded. Only review threads
carry resolved/unresolved state, and without it there is no way to tell feedback
still standing from feedback already handled — a "fixed in 3a4f2" from last week
would be seeded forever. Feedback that lands as a plain comment is therefore
missed, which is a real gap and the most likely thing to revisit.

**Accepted.** This is the third mode flag on `gw run`, and the mode-conflict
check is now a list rather than a pair. Issue #24 proposes collapsing all three
into a `--mode` registry; this lands ahead of that work and adds one more case
for it to absorb rather than reducing the pressure for it.

## Alternatives considered

- **Tell the agent to fetch the feedback itself.** The cheapest option, and the
  one that looks right until you check what `gh pr view --json` exposes: not
  `reviewThreads`, so an agent that does the obvious thing sees review *bodies*
  and misses every inline comment. Rejected for that, and because it makes the
  content of the brief depend on how the agent chooses to explore.

- **Seed only the review comments and leave CI to the agent.** Rejected as the
  worse half of the split: the failing log is the input with the highest
  paste-into-the-session cost today, which is exactly what issue #17 names.

- **Reply to and resolve threads as gw's own writes.** Rejected as over-reach
  and out of step with `--notify-linear`: an unflagged write to a reviewer's
  thread is not something a task-runner should do on its own initiative.

- **Cache the feed on `Task` so repeated runs skip the fetch.** Rejected: review
  feedback is the fastest-moving state gw touches, a stale thread list is worse
  than a slow one, and ADR 0006 already puts modes on the session rather than
  the task.

- **Wait for issue #24's `--mode` registry and land this as config.** Rejected
  on sequencing: the registry is a refactor of three call sites that do not
  exist yet in final form, and this mode is the third data point that tells us
  what the registry actually needs to express (a template *plus* a data-gathering
  step, which neither existing mode has).
