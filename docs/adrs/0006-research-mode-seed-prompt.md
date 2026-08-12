# 0006. Work modes are alternate seed templates, not slash-command bypasses

- Status: accepted
- Date: 2026-08-12

## Context

`gw new` and `gw run` seed a fresh agent session with a prompt built by
`agents/launcher.build_seed_prompt`, which renders `templates/spawn_prompt.md`
with the task's tracking item, branch, and worktree. The template ends with a
standing instruction:

```
When this task is ready for review, open a PR via `gw pr open`.
```

That line sits outside every substitution slot, so it is in every repo-task seed
prompt regardless of what the caller passes.

Issue #11 asks for a `--research` mode: spawn the agent to investigate a Linear
ticket or GitHub issue and report back, without updating Slack, the ticket, or
GitHub. Read-only and local steps are explicitly allowed.

Two facts frame the decision:

**The existing `--prompt` flag cannot express it.** `--prompt` replaces the
trailer only. A `gw new --issue 11 --prompt "research this"` session still
receives the PR-opening instruction — the exact mutation the request forbids.

**There is already a competing precedent.** `--adversarial-review` (on both
`gw new` and `gw run`) bypasses `build_seed_prompt` entirely and seeds the
literal string `/codex:adversarial-review --wait`. It does that for a specific
reason recorded in the code: Claude Code ignores a slash command that is not the
whole user message. The cost is that the session starts with *no* ticket context
and the flag only works with `--agent claude`.

Without a decision here, the next mode-shaped flag picks one of these two
patterns at random, and a reader cannot tell why `--research` and
`--adversarial-review` are built differently.

Separately: `gw new` today mutates nothing outside the machine. Every external
write in the codebase sits behind `gw pr open` (`gh pr create`, `git push`,
Linear `create_comment` under `--notify-linear`) or the `gw sync` notifier
(ADR 0005), which is the only Slack-shaped surface gw has. So the request is not
asking us to restrain `gw`. It is asking us to change what the agent is told.

## Decision

A **work mode** that changes the agent's standing instructions is expressed as
an alternate seed template rendered through `build_seed_prompt`, sharing the
same task-context slots. It is not a slash-command bypass, and it is not a
canned `--prompt`.

Concretely, `--research` on `gw new` and `gw run` renders a new
`templates/research_prompt.md` instead of `spawn_prompt.md`. That template
carries the same `{ticket_id}` / `{title}` / `{repos_block}` / `{description}` /
`{addition_block}` context, replaces the PR-opening instruction with an explicit
read-only boundary, and directs the agent to report its findings **in the
session** rather than to a file.

Three constraints come with it:

1. **`--research` requires a tracking item.** It is rejected for sources with no
   Linear ticket or GitHub issue (`--branch`, `--branch-name`, `--branch-auto`,
   `--dir`, `--pr`, and scratch tasks) — a research brief about nothing is a
   silent no-op, and gw's house style is to refuse loudly.

2. **The boundary is a prompt-level contract, not enforcement.**
   `defaults.unsafe = true` is the documented default, so the agent runs with
   bypassed permissions and retains the ability to push or comment. We are
   buying "the agent is not instructed to mutate", not "the agent cannot
   mutate". gw does not gate `gw pr open` or any other command on research mode.

3. **The mode is a property of the session, not the task.** Nothing is persisted
   on `Task`. A research session routinely turns into an implementation session
   on the same task, and `gw run --research` re-derives the mode from the flag.

`--prompt` composes with `--research`, narrowing the investigation's focus
rather than conflicting with it.

## Consequences

**Easier.** Modes keep the ticket context that is the whole input to the work,
and stay agent-agnostic — `--research` works with codex and gemini, which
`--adversarial-review` cannot. The next mode has a pattern to follow and a
reason recorded for not copying `--adversarial-review`. Because the brief sends
findings into the session and forbids source edits and commits, a research
session leaves the working tree as it found it — though the commands it *is*
allowed to run (tests, linters, builds) still write their own caches and
artifacts, so "no trace on disk" would be too strong.

**Harder.** A second template duplicates the context header of
`spawn_prompt.md`; a change to the repos block or ticket-context format now
touches two files, and nothing detects the drift. Prompt text becomes product
surface — tests can assert a string is present, but not that the brief still
produces good research, so the quality of this feature degrades invisibly.

**Accepted.** The read-only guarantee is advisory. A user who reads
"should not update GitHub" as a hard boundary will be wrong, and the failure is
silent — an agent that opens a PR anyway looks like a normal PR. We accept this
because the alternative (gating commands on a persisted task flag) buys little:
the agent can shell out to `gh` directly regardless.

**Accepted.** `gw status` and the session picker cannot report that a session
was a research session. Partial mitigation for free: `launcher._label_from_prompt`
derives the session label from the prompt's first 80 characters, so a research
session's label opens with the research intro and is recognizable in the picker.

## Alternatives considered

- **Seed a slash command, as `--adversarial-review` does.** Rejected: no such
  skill exists to delegate to, it would pin `--research` to `--agent claude`,
  and it discards the ticket context that is the entire input to the research.

- **Make `--research` sugar for a canned `--prompt`.** Rejected: it cannot
  suppress `spawn_prompt.md`'s PR-opening line, which is the one instruction
  that must not appear. This is the option that looks obviously right until you
  read the template.

- **Add a `{closing}` slot to `spawn_prompt.md` and keep one template.** The
  closest alternative. Rejected because the two briefs differ in intro, body,
  and closing rather than just the last line, and because parameterizing the
  slot would move the default template's most consequential instruction out of
  the file a reader opens to find it.

- **Persist a research marker on `Task` and refuse mutating commands for
  research tasks.** Rejected as over-reach and as a false promise: the session
  is what is read-only, not the task, and blocking `gw pr open` does not stop an
  agent from running `gh pr create` itself.
