# 0012. `gw sync` acts on the edges it detects

- Status: accepted
- Date: 2026-08-13

## Context

`gw sync` already detects every transition worth knowing about — `pr-merged`,
`checks-failed`, `checks-passed`, the three transcript-derived agent states from
ADR 0010, `prunable` — and the only thing it could do about any of them was send
a notification. For someone running six agents in parallel, that leaves the
loop open at exactly the point where it costs the most: you get a banner saying
CI went red on a branch, and you still have to go and start something.

The machinery for closing it was already in place and correct. `_edge` records
the last observed value per signal in `SyncState.last_seen`, so an event fires
once per transition and not once per pass; `_fire` is the single choke point
every event routes through. What was missing was permission to do anything.

The obvious hazard is the one the issue names: an unattended job that can spawn
processes is one bad rule away from a runaway loop. And ADR 0007's headless
windower had just made a terminal-free agent run possible, which is the thing
that makes an action worth having at all — an action that needs a TTY is useless
to a launchd job.

## Decision

A sync pass may take **actions** on the edges it detects, configured per event
in `[sync.on]` and empty by default.

```toml
[sync.on]
checks-failed = ["spawn-fix-session"]
pr-merged     = ["prune"]
```

Four constraints define the shape:

1. **The action vocabulary is closed.** `sync/actions.py` holds a static
   registry — `spawn-fix-session`, `prune`, `archive`. `[sync.on]` *names* an
   action; it never supplies one. Letting config name a command to run would
   make this the plugin system AGENTS.md forbids, and would hand a scheduled
   launchd job an arbitrary `execve`. Both sides of the arrow are typed
   `Literal`s, so a typo in `config.toml` is a validation error rather than a
   silently dead rule.
2. **Actions ride the existing edge trigger.** They are queued from `_fire`, so
   once-per-transition comes for free and no second mechanism can drift from the
   first. Firing an action and sending a notification are, however, independent
   switches: `sync.on` is not gated on `notify_events`, because wanting a red
   branch fixed without also wanting a desktop banner is ordinary.
3. **Two rate limits, behind the edge trigger rather than instead of it.**
   `action_rate_limit_seconds` (default 3600) is a per task+event+action
   cooldown, which catches a signal that genuinely *flaps* — CI retried, a PR
   reopened — where the edge trigger only bounds a steady one.
   `max_actions_per_pass` (default 4) caps one pass's total fan-out, because
   twenty branches going red at once is one CI outage and not twenty agents'
   worth of work. Overflow is journaled, never silently dropped.
4. **A spawn action is always headless.** `HeadlessWindower` unconditionally,
   not `defaults.windowing`: nobody is at the terminal when launchd fires a
   pass, so `inline` would block the pass on the agent and `tmux` would either
   fail or hijack the user's session.

Two structural choices follow from wanting this safe rather than clever:

- **Actions are deferred to the end of the pass, not run at the edge.** A
  `prune` action deletes the record the task loop is iterating. Queueing during
  the walk and draining afterwards — re-reading each task at execution time —
  means an action always sees what the pass just wrote, and "step 7 already
  pruned this" is a clean skip instead of an action against a deleted checkout.
- **Handlers may decline, and a decline costs nothing.** Every handler returns
  `ActionResult(ran=…, detail=…)`; a decline is journaled as `action-skipped`,
  starts no cooldown, and spends none of the pass budget. That is what lets the
  guards be conservative — a task whose agent is still `working` (ADR 0010),
  an archived task with no worktree, a branch that isn't actually merged — with-
  out any of them being able to wedge the rule permanently.

`prune` shares `engine.prune_blocker` and `merge_detection` with the periodic
step-7 prune rather than reimplementing them, so the `[sync.on]` prune cannot
be weaker than the automatic one no matter which event it is wired to. A user
who writes `checks-passed = ["prune"]` gets a decline, not a deleted branch.

## Consequences

- `gw sync` can be a supervisor, not just a reporter. The canonical setup —
  `checks-failed = ["spawn-fix-session"]` — closes the CI loop without a human
  in it.
- Anyone who never edits `[sync.on]` sees no behaviour change at all. Empty by
  default is the whole safety story for existing users.
- The blast radius of a misconfiguration is bounded by construction, not by
  care: edge trigger × cooldown × per-pass cap × handler guards, each of which
  independently caps the damage.
- `SyncState` grows an `action_runs` map. It shares `last_seen`'s
  `<project>/<task_id>:…` key prefix, so the self-healing sweep and
  `_forget_task` drop it the same way — a recreated task reusing an id must not
  inherit a dead one's cooldown and sit out its first action.
- A spawned session is an ordinary session: it appears in `gw status`, in the
  picker, and in the token/cost rollups. It is not a special kind of run, which
  is what keeps it debuggable.
- Adding a fourth action means editing the registry, which is deliberate
  friction. Actions that want a data-gathering step (the way `--address-review`
  wants a review feed) belong in gw, not in `config.toml`.

## Alternatives considered

- **A `command` action running user-supplied argv**, mirroring
  `sync.notify_command`. Rejected: a notifier gets a title and a body and its
  failure mode is a missing banner, while an action runs against a live
  worktree with `--dangerously-skip-permissions` in the neighbourhood. The
  asymmetry is large enough that the same shape isn't justified by precedent.
- **Running each action inline at its edge.** Simpler to read, but a prune
  mutating the collection being iterated is a real bug, and an action would act
  on a record that later steps in the same pass then revise.
- **Gating actions on `notify_events`.** Would have saved a config key, at the
  cost of forcing a desktop banner on anyone who wanted automation and quiet.
- **Letting `[sync.on]` name a work mode (ADR 0009) for the spawn brief.**
  Attractive — modes already are "prompt text and policy flags, never code" —
  but a mode's brief is written for a human starting a task, not for a
  supervisor explaining an event. Deferred until there is a second caller that
  wants it.
- **Doing nothing and leaving `gw sync` a reporter.** The status quo. Rejected
  because the detection was already the hard half, and leaving it unused makes
  every edge cost a context switch that the machinery could have absorbed.
