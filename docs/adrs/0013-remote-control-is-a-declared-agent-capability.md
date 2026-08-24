# 0013. Remote control is a declared agent capability, named after the task

- Status: accepted
- Date: 2026-08-24

## Context

Claude Code has a Remote Control mode: a session running locally can be driven
from claude.ai/code or the Claude mobile app. Execution stays on this machine —
same worktree, same MCP servers, same plugin and skill config — and the phone is
only a window onto it. That is the only shape that could work for `gw`, whose
whole premise is a git worktree on local disk.

Issue #65 asks for it as a launch option. It is a launch-time flag on the claude
CLI, so `gw` is where it has to be turned on:

```
--remote-control [name]    Start an interactive session with Remote
                           Control enabled (optionally named)
```

Three facts about that signature and about `gw` frame the decision.

**The optional value is a hazard, not a convenience.** `gw`'s spawn argv ends
with the seed prompt as a positional (`claude --session-id <uuid> <prompt>`). A
bare `--remote-control` in front of it would be handed the seed prompt as the
session name — several hundred characters of ticket context, and no session name
anyone would recognise. The failure is silent: the session starts, and it works.

**Only one of the four registered agents has the feature.** codex, gemini, and
antigravity have no equivalent. `gw`'s existing per-agent flag machinery
(`unsafe_flags`) assumes every agent has *some* form of the thing being asked
for, which is true of bypass-permission mode and false here.

**`defaults.*` is global while the agent is per-task.** A user who turns remote
control on in config will still run codex on some tasks. Whatever this does when
the agent can't oblige has to be decided, not left to fall out.

Separately, the headless windower (ADR 0007) runs the agent's print mode, which
exits as soon as the turn is done. Remote-controlling that is remote-controlling
a process that has already gone.

## Decision

**Remote control is a capability an agent declares, and the flag names the
session after the task.**

Three parts.

1. **`Agent.supports_remote_control` is a declared class attribute**, alongside
   `transcripts: TranscriptCapability` (ADR 0010) and for the same reason: the
   degradation is real and must not be silent. `spawn_command` and
   `resume_command` gain a `remote_control: str | None` keyword carrying the
   session *name*; agents that don't support it accept and ignore it, exactly as
   they already do for `session_id`.

2. **`launcher.resolve_remote_control` is the single decision point**, called
   by the command layer *before* it creates a task, worktree, or scratch
   directory. Both of its refusals are decidable from the flags alone, and a
   command that builds a checkout and then rejects its own arguments is the
   failure `--research`'s ticket check already refuses to be (ADR 0006).

   It splits on where the request came from. An explicit `--remote-control`
   against an agent without the feature — or against a headless windower — is a
   `GoblinError`: the point of the flag is reaching the session from a phone,
   and quietly launching one you can't reach is worse than not launching. The
   same value inherited from `defaults.remote_control` is not an error, because
   that value is global while the agent and the windower vary per task. An
   unsupported agent costs one muted line; a headless run turns it off silently,
   since a headless fleet is exactly what someone with the default set also runs
   all day and nobody reads an unattended log to be told a flag they never typed
   didn't apply.

3. **The name is always passed, and it is `task.id`.** Never omitted, because of
   the prompt-swallowing hazard above; `task.id` because claude's own default is
   derived from the hostname, which distinguishes nothing when six agents share
   one laptop. The Claude app's session list then reads `eng-123`, `gh-42` — the
   same identifiers `gw status` prints. `launch` composes it, from the task it
   already holds, so the three spawn commands pass a bool and cannot drift on
   the naming.

Remote control is **interactive only**, and that holds in two places. The
command layer refuses it up front, as above. `launch` refuses it again next to
the existing headless + resume check — a backstop for programmatic callers, and
what makes it safe that `Agent.headless_command` does not take a
`remote_control` parameter at all. The missing parameter is the structural half:
the combination cannot be expressed, not merely rejected.

Resume carries the flag too: "keep going on this from the couch" is the case
resume exists for.

`defaults.remote_control` is `false`. Most `gw` sessions are headless fleet runs
that cannot use it, so on-by-default would mean a warning line on the majority of
launches.

## Consequences

**Easier.** The long-running-agent workflow gains an exit from the desk: start
six agents, walk away, answer the one that asks a question. Pairing the flag with
`--windowing tmux` — which already survives a closed terminal and an SSH
disconnect — is the combination the docs point at, and it needed no new code.
A future agent that grows the feature declares it and inherits the whole path.

**Harder.** The `Agent` protocol grew a keyword that four of five
implementations exist only to discard, and a fifth attribute that is `False`
four times out of five. That is the price of refusing a `agent.name == "claude"`
check in the launcher, which this repo has already paid twice (`Windower.detaches`
/ `.headless`, `TranscriptCapability`) and which stays cheaper than the
alternative.

**Accepted.** `gw` verifies nothing about whether remote control will actually
work: it does not check for a claude.ai login, an API-key-only setup, or the
Team/Enterprise admin toggle that gates the feature. The claude CLI reports all
three perfectly well itself, and duplicating that check would mean `gw` tracking
someone else's entitlement model.

**Accepted.** Two sessions on the same task get the same remote-control name.
They are both that task, the app shows both, and a per-session suffix would trade
a recognizable name for a unique one.

**Accepted.** `gw status` cannot say a session is remote-controlled. Nothing is
persisted about it — it is a property of the running process, and asking claude
would mean a round trip per session on every render.

## Alternatives considered

- **Pass a bare `--remote-control` and let claude name the session.** Rejected on
  the argv hazard alone: the seed prompt becomes the name. Even on the resume
  path, where there is no trailing positional to swallow, a hostname-derived name
  is useless with several agents on one machine.

- **Check `agent.name == "claude"` in the launcher.** Rejected; this is the
  name-string check `AGENTS.md` forbids for windowers, for the same reason. A
  capability that lives on the agent moves with it.

- **Ignore the flag silently for agents without it.** Rejected. It is exactly the
  silent degradation `TranscriptCapability` exists to prevent, and here the user
  finds out by picking up their phone and finding nothing there.

- **Error for an unsupported agent or windower regardless of where the request
  came from.** Rejected as hostile to the config path:
  `defaults.remote_control = true` would then break every codex task and every
  headless fleet run, and a global default that breaks a subset of launches is a
  default nobody can set.

- **Refuse inside `launch` only, and skip the command-layer check.** This is
  what the first implementation did, and a live trial found it: `gw scratch
  --remote-control --agent codex` created the directory and the task record,
  then errored. Both refusals read only flags, so there is no reason for either
  to wait until there is something on disk to orphan.

- **Make it work headless by keeping the process alive.** Rejected — that is the
  resident supervisor ADR 0005 and ADR 0007 both declined, arrived at from a new
  direction.

- **Persist a `remote_control` flag on `SessionRecord` so `gw status` can show
  it.** Rejected: the same reasoning as ADR 0010's refusal to cache activity
  state. It is a fact about a live process, and a stored copy would be wrong the
  moment the process ends.
