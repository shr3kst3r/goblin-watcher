# 0010. Classify agent state from the transcript, not file mtime

- Status: accepted
- Date: 2026-08-13

## Context

`gw` decided whether an agent was busy by looking at its transcript file's
mtime: written within `defaults.activity_active_seconds` → `● active`, older →
`idle <age>`. Two places consumed that — the `gw status` session badge and the
sync pass's `agent-idle` notification (ADR 0005) — and both inherited its
limits.

mtime answers exactly one question: is the file moving? The question someone
running six agents in parallel actually has is a different one, and it has
three answers:

- **done** — finished, nothing pending;
- **blocked on you** — asked something and is waiting;
- **still working** — mid tool call.

mtime cannot separate them, and gets each of them wrong in its own way. An agent
twenty minutes into one long tool call writes nothing and reads as idle,
precisely when it is busiest — the same blind spot AGENTS.md already admits for
tmux `mark_idle`, from the other direction (an agent streaming a spinner never
goes quiet). And an `agent-idle` notification that fires for both "your six
agents each finished a chore" and "one of them needs an answer" trains you to
ignore all of them. Notification fatigue is what makes running six agents
intolerable, and an event that can't say why it fired is what causes it.

The transcript has the answer already. The shape of its last records says which
state the agent is in: a tool call with no matching result is working, an
assistant turn that ended on a question is blocked, a completed turn with
nothing outstanding is done.

## Decision

**Classify agent state from the shape of the transcript's tail. Name the states,
and drive both the badge and the notifications off them.**

1. **A state vocabulary**, in `goblin_watcher.activity`:
   `working` · `needs-you` · `done` · `idle` · `unknown`. `idle` and `unknown`
   carry the cases the transcript can't answer — quiet-for-unknown-reasons, and
   no-transcript-at-all — so no caller has to invent a fourth answer.

2. **Agents report shape, not meaning.** `Agent.read_tail(path)` returns a
   `TranscriptTail` — is a tool call outstanding, who spoke last, what did the
   last assistant turn say — and `activity.classify` turns that into a state.
   The question-detection heuristic is agent-independent and is written once,
   not once per agent module. `claude` and `codex` implement it; `gemini`,
   `antigravity`, and `managed` return None and keep the mtime reading.

3. **Bounded tail reads.** `read_tail` takes a path and parses a fixed window
   from the *end* of the file (`agents/_tail.py`, 256 KiB), not the whole
   transcript and not a `(session_id, cwd)` lookup. This is called per session
   per render and `gw status --watch` renders every two seconds; transcripts run
   to tens of megabytes and codex's lookup re-globs `~/.codex/sessions`.

4. **Nothing is persisted.** Classification is a pure function of the file on
   disk. `gw status` and a sync pass call the same function and cannot disagree,
   and no `SessionRecord` field can go stale against the transcript it describes.

5. **`agent-idle` splits into three events**: `agent-needs-you` (the one worth
   interrupting for — its body carries the question), `agent-done`, and
   `agent-idle`, now narrowed to the agents that still can't do better. All
   three stay edge-triggered per ADR 0005, but on a token that folds a digest of
   the classifying evidence in with the state name — otherwise a second question
   asked after the first was answered reads as "still needs-you" and is silently
   swallowed. A config listing `agent-idle` alone predates the split and is read
   as asking for all three.

6. **Silence outranks a stale shape.** A session the transcript calls `working`
   that has written nothing for `defaults.activity_grace_seconds` is reported
   `idle`: an agent killed mid-tool-call leaves its call unmatched forever, and
   would otherwise sit in `gw status --active` permanently.

## Consequences

- `gw status` distinguishes `● working`, `◆ needs you`, and `✓ done` per
  session. The `● active` badge is gone; `idle <age>` survives as the fallback.
- `gw status --active` keeps a task on screen while its agent is mid tool call
  regardless of mtime, which is the case the grace window was papering over.
- A notification now says which of the three happened, so `agent-needs-you` can
  be left on and `agent-done` turned off (or the reverse) instead of the
  all-or-nothing `agent-idle` switch.
- Issue #26's action layer has named edges to hang off, and one place —
  `activity.classify` — to read them from.
- Question detection is a heuristic and will be wrong sometimes. It is wrong in
  the cheap direction by construction: it reads only the last line of the final
  turn plus a short list of explicit hand-back phrases, so a missed question
  degrades to `done` (a badge that undersells) rather than a `needs-you`
  notification you didn't need.
- Two more transcript parsers to keep current as claude's and codex's on-disk
  formats drift. Both fail closed: an unrecognised record shape returns None and
  falls back to mtime, so drift costs precision, never a crash.
- The first pass after upgrading sees a token that doesn't match the old
  `active`/`idle` values and fires once per session. A one-time cost of moving
  the edge memory to a richer token; the alternative was clearing it, which
  costs a *missed* notification instead.

## Alternatives considered

- **Persist the state on `SessionRecord`.** Rejected: a state this time-sensitive
  cached behind a TTL is wrong more often than it's right, it needs a writer on
  every path that renders it, and two readers with different refresh timing
  would disagree on screen. The read is cheap enough not to need a cache.
- **Poll the agent's process instead** (is it running, what is it blocked on).
  Rejected: it says nothing about *why* an agent stopped, doesn't survive tmux
  or a headless detach, and ADR 0005 already declined a resident process.
- **Ask an LLM to classify the tail.** Rejected: a per-session API call on every
  `gw status` render, to answer a question that structure already answers
  deterministically.
- **Parse the whole transcript.** Rejected on cost — `--watch` re-renders every
  two seconds, and the answer only ever lives at the end of the file.
- **Keep `agent-idle` as the single event and put the state in the body.**
  Rejected: per-event toggles are the control users have, and leaving them one
  switch for "finished" and "blocked" is the fatigue this set out to fix.
