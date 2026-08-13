# 0007. Unattended runs are a third windower, not a supervisor

- Status: accepted
- Date: 2026-08-13

## Context

`gw` is built for parallel autonomous agents, but until now every spawn needed a
human at a terminal. `InlineWindower` blocks the calling process for the life of
the agent; `TmuxWindower` detaches, but only into a pane that exists to be
attached to. There was no way to say "start ENG-123 and tell me when it's done"
— which means `gw` could not be driven from cron, from a queue, or by another
`gw`-spawned agent. Issue #15 asks for that.

Three facts frame the decision:

**The agents already support it.** Every registered CLI has a non-interactive
mode: `claude -p`, `codex exec`, `agy -p`, `gemini -p` (the gemini agent's
interactive spawn already uses `-p`, and `agents/antigravity.py` comments
explicitly on `-p` being headless). Nothing new has to be built inside the agent
layer — only named.

**Completion is already observable.** ADR 0005's `gw sync` fires an
edge-triggered `agent-idle` notification when a session's transcript goes quiet.
`claude -p` and `codex exec` write the same transcripts their interactive modes
do, so an unattended run's completion is already a signal gw knows how to
deliver. "Tell me when it's done" needs no new mechanism.

**gw has no resident process, on purpose.** ADR 0005 chose a short-lived
scheduled command over a daemon. Anything that waits on an agent for hours to
record its exit status would be that daemon.

## Decision

Unattended execution is a third `Windower` — `HeadlessWindower`, selected by
`windowing = "headless"` or `--windowing headless` — and nothing more. It
`Popen`s the agent's print mode with `start_new_session=True`, stdin on
`/dev/null`, stdout and stderr appended to
`<project>/.goblin/logs/<task>-<session>.log`, records the child pid in a
sibling `.pid` sidecar, and returns 0 immediately.

Two protocol changes make it fit:

- `Agent` gains `headless_command(prompt, cwd, unsafe, session_id)`, the
  print-mode counterpart to `spawn_command`. `managed` raises, like its other
  interactive methods.
- `Windower` gains two declared booleans, `detaches` and `headless`. The
  launcher had been branching on `windower.name == "tmux"` to skip post-run
  reconciliation; that string check becomes `windower.detaches`, and
  `windower.headless` is what selects `headless_command` over `spawn_command`.

Four constraints come with it:

1. **Fresh sessions only.** A `Resume` choice under headless windowing is
   refused before anything is persisted. Print mode means "run this prompt to
   completion"; resuming a conversation with nothing to say would either block
   on stdin or burn a turn to no effect. The honest equivalent is a fresh
   headless run whose prompt is the follow-up.

2. **The agent's exit status is not recorded.** `run` reports 0 for "the spawn
   succeeded" and returns. Capturing the real status would require something to
   wait on the child — the daemon ADR 0005 ruled out. Completion is observed via
   `gw sync`'s `agent-idle` edge; failure is diagnosed from the log.

3. **`send` refuses.** The child's stdin is `/dev/null` and it is running a
   single turn, so there is no prompt to type into. `gw session send` says that
   rather than failing obscurely, exactly as inline mode does.

4. **The log is a diagnostic, not a transcript.** Print mode emits its final
   result, so the file's main value is capturing a failed start (bad flag,
   expired auth) — the conversation itself stays in the agent's own transcript,
   which is what `gw status`, the picker, and the session description already
   read.

## Consequences

**Easier.** `gw new --issue 42 --windowing headless --prompt "..."` is a
complete unattended job: it can be a cron entry, a queue worker's payload, or a
command an agent runs to fan work out to another agent. Existing machinery
carries the rest — `gw sync` notifies on completion, `gw status` shows the
session, the transcript feeds the same summary and description paths. The two new
`Windower` booleans also retire a name-string check the launcher had been
carrying.

**Harder.** Three windowers is three spawn paths to reason about, and the
`Agent` protocol is one method wider — a new agent now owes a `headless_command`
as well (the checklist in root `AGENTS.md` and a test over the registry both
enforce it).

**Accepted.** Unattended runs want `unsafe = true` (the documented default). A
headless run without it will stall at its first permission prompt with nobody to
answer, which looks like a hang; the launcher prints a warning and does not
refuse, because some agents' print mode is genuinely useful read-only.

**Accepted.** Agents that cannot preassign a session id (codex, gemini,
antigravity) keep the synthesized placeholder, exactly as they do in tmux mode —
the log filename carries the placeholder, and codex's transcript is found by
falling back to the newest rollout for the worktree. Correct while one such
session per worktree is active.

**Accepted.** `agent-idle` only fires for agents whose transcripts gw can read.
Gemini and antigravity transcripts are stubs, so an unattended run on those two
completes silently and has to be checked by hand (`is_live`, or the log). That's
a pre-existing gap in transcript support, surfaced rather than caused here.

**Accepted.** Pid reuse can make a stale `.pid` sidecar read as live. The cost
is a cosmetically wrong `is_live`, which doesn't earn a heavier mechanism.

## Alternatives considered

- **A `--detach` flag on the existing windowers.** Rejected: detaching is
  exactly the placement decision `Windower` exists to own, and `inline
  --detach` would have to swap the agent's interactive mode for its print mode
  anyway — which is the whole of the new windower.

- **Wait on the child and record its exit status.** The obvious ask, and
  rejected as the daemon ADR 0005 already declined. A `gw` invocation that
  blocks for the life of an agent is `inline` with the output redirected, and it
  cannot survive the cron slot or the shell that started it.

- **Notify directly on completion instead of via `gw sync`.** Rejected for the
  same reason: something has to be alive at completion time to send it. The
  `agent-idle` edge already covers this from a scheduled pass.

- **Reuse `tmux` with a detached session nobody attaches to.** Rejected: it
  makes tmux a hard dependency for unattended runs, keeps the agent in TUI mode
  (so its output is a scrollback buffer with escape sequences, not a log), and
  leaves panes to garbage-collect.

- **Stream structured output (`claude -p --output-format stream-json`) into the
  log.** Tempting, since plain print mode writes nothing until the turn ends.
  Rejected for now: it is claude-specific, and it duplicates the transcript the
  agent already writes. The log's job is capturing failures to start.
