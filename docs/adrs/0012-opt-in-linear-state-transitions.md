# 0012. Linear state transitions are opt-in config, not a flag

- Status: accepted
- Date: 2026-08-13

## Context

Until now gw performed exactly one write against Linear: `gw pr open
--notify-linear` posts a comment with the PR URL. Everything else was read-only,
deliberately — AGENTS.md states the boundary, and ADR-era design notes recorded
that issue mutation was on hold.

The cost of that posture shows up as manual work. gw already knows the two
moments a ticket's state is wrong: a session starts and the ticket still says
Todo, a PR opens and it still says In Progress. The user moves it by hand every
time, for every ticket.

The constraints that framed the decision:

- **The read-only default is load-bearing.** gw runs many agents unattended.
  A version that starts writing to a shared team's tracker because it was
  upgraded is a bad surprise, and the tracker is the one surface other people
  watch.
- **Workflow-state names are per-team.** There is no universal "in progress":
  a team can call it Started, Doing, or nothing at all, and the API wants a
  state id, not a name.
- **Neither moment can afford to block.** A session start is the user waiting
  on their agent; a PR open has already pushed and created the PR by the time
  the ticket would move. A Linear outage must not cost either of them.

## Decision

Two config keys, unset by default:

```toml
[linear.transitions]
on_session_start = "In Progress"
on_pr_open       = "In Review"
```

- **Config, not a CLI flag.** The move should happen on *every* session and
  *every* PR once the user wants it; a flag they must remember to pass is the
  manual work this replaces. `--notify-linear` stays a flag because a comment is
  a per-PR editorial choice, not a standing policy.
- **Unset means no write.** A key that isn't set is a code path that never
  reaches the network. The read-only default is unchanged for anyone who does
  nothing.
- **A state *name*, resolved against the ticket's own team.** One query
  (`FETCH_ISSUE_WORKFLOW`) returns the issue's internal id, its current state,
  and its team's workflow states; the name is matched case-insensitively against
  that list. A name the team doesn't define is reported with the states it does,
  and nothing is written.
- **Fail open, always.** `linear_transitions.apply` never raises. Missing API
  key, unreachable Linear, timeout, unknown state, an `issueUpdate` that comes
  back unconfirmed — each prints one muted line and returns the task untouched.
  The session launches; the PR is already open.
- **Idempotent.** A ticket already in the target state costs one read and no
  write, so resuming a session repeatedly doesn't churn the ticket's activity
  feed.
- **The transition timeout is its own knob** (`timeout_seconds`, default 8s),
  shorter than the client's 15s default: nothing downstream is waiting on the
  answer.

`gw run` applies `on_session_start` on resume as well as on a fresh session.
Resuming *is* working on the ticket, and the idempotence above makes the repeat
free.

## Consequences

- gw performs two writes against Linear now, both opt-in, and AGENTS.md's safety
  section says so.
- A successful move is written back to the task's cached Linear state
  (`Task.linear.state` + `linear_state_updated_at`), so `gw status` doesn't keep
  rendering the state gw just moved away from until the TTL expires.
- Every caller of the hook has to hold a `Project`, because the cache write goes
  through `state.update_task`'s narrow patch under the task lock (ADR 0004).
  `commands/new.py`, `commands/run.py`, and `commands/pr.py` all already do.
- The failure mode is now "the ticket silently didn't move" rather than "the
  command failed". That is the intended trade — a muted line names it — but it
  does mean a persistently misconfigured state name goes unnoticed by anyone not
  reading the output.
- A future third trigger (on merge, on prune) is a new key plus a call site, not
  a new mechanism. `Trigger` is a `Literal` of config-key names precisely so a
  trigger is looked up rather than branched on.

## Alternatives considered

- **A `--move-ticket <state>` flag on each command.** Rejected: it makes the
  common case — always move it — the thing the user must remember every time,
  which is the manual work the issue is about.
- **A state *type* (`started` / `completed`) instead of a name.** Linear's
  `WorkflowState.type` is a small closed enum, so it would work across teams
  with no per-team config. Rejected because a team with three states of type
  `started` gives gw no way to pick, and the name is what the user already
  says out loud.
- **Deriving the state from the task's status field.** gw's `Task.status`
  already tracks the PR; mapping it onto a team's workflow would be gw
  inventing a lifecycle the team never agreed to.
- **Making the write fatal on failure.** Rejected outright: the session and the
  PR are the work, and the ticket move is bookkeeping about them. Bookkeeping
  does not get to veto the work.
- **Closing the ticket when the PR merges.** Out of scope; the issue asks for
  two triggers and both are moments gw is already synchronously present for.
  Merge is `gw sync`'s territory, and a background pass writing to the tracker
  deserves its own decision.
