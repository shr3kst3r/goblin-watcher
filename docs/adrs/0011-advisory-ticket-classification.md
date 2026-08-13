# 0011. The ticket check is advisory, and its mode suggestions come from the registry

- Status: accepted
- Date: 2026-08-13

## Context

`gw new` decides three things before a single byte of the ticket has been read:
which agent runs, which work mode it runs in, and what the first message says.
All three come from the command line, typed by someone who may have skimmed the
ticket in a browser tab and may not have. Two failures follow from that, and both
are cheap to catch and expensive to discover late:

- A **question-shaped ticket** ("should we shard the events table?") gets an
  implementation session, and the agent starts changing code to answer a question
  nobody had decided to act on. `--mode research` existed for this since ADR 0006;
  the problem is that nothing suggests it.
- An **underspecified ticket** gets an agent that picks one reading of it and
  spends an hour on that reading. The ambiguity was visible in the ticket text the
  whole time.

The forcing function is that reading the ticket is now nearly free. Session
descriptions already make Haiku calls on a TTL (`description_model =
"claude-haiku-4-5"`), so the machinery — binary discovery, timeout, failure
swallowing, an "off" switch — exists and is proven. One more call at task-creation
time costs a fraction of a cent and a few seconds.

Two constraints frame what may be built with it. First, `gw new` is the command
that creates branches and worktrees, and it must not become less reliable because
a model was unavailable. Second, ADR 0009 made mode selection a registry and
named this work as the thing that would hang off it: whatever suggests a mode has
to suggest it *by name from the registry*, or the flag matrix that ADR removed
comes back as a classifier full of `if shape == "question"` branches.

## Decision

`gw new` runs an **advisory ticket check** — `classify.advise(task, mode=…,
enabled=…)` — after the task exists and before the agent launches. It prints a
suggested mode, up to three ambiguities, or a single line saying it found
neither. Four commitments define it.

**1. Advisory, without exception.** Nothing in `classify` writes state or changes
what the command does. The mode it names is a suggestion for the reader's next
invocation, not an override of `--mode`, `--agent`, or `--prompt`. A run whose
check says "suggests `--mode research`" still launches the default work brief,
and a test asserts exactly that. The alternative — acting on the suggestion —
would make `gw new` non-deterministic in the one place users need it predictable.

**2. Fail-open, always.** `advise` catches every exception and returns `None`.
Missing binary, timeout, banner-wrapped output, malformed JSON, an invented mode
name: each prints nothing. This is not defensive habit, it is the ordering — the
check runs *after* the branch and worktree are on disk, so there is no state a
refusal could protect. Three off switches, widening in scope:
`--no-classify` (one run), `defaults.classify_tickets = false` (config), and
`GW_CLASSIFY=off` (environment, for scripts, cron, and the test suite).
`description_agent = "off"` disables it too, since it disables every model call
gw makes.

**3. Suggestability is a `ModeSpec` field, not a name check.** A mode is
suggestable iff it sets `suggest_when` — one sentence describing the ticket shape
that should trigger it, which goes into the prompt as that mode's condition. The
built-in `research` mode sets one; `adversarial-review` does not, because it
answers to how you want to work rather than to anything readable in a ticket. A
user's mode in `[modes.<name>]` becomes suggestable by writing that sentence, and
the mode already named on the command line is filtered out of the candidate list.
The classifier can therefore only return a name the registry offered it; anything
else is dropped at parse time.

**4. One cheap-model surface, not two.** `description.run_llm` is promoted to the
shared call. Classification reuses the `description_agent` / `description_model`
pair rather than adding a second agent, a second model setting, and a second set
of failure modes. It gets its own timeout (`classify_timeout_seconds`, 20s
default) because this call is synchronous with a person waiting on it, where the
description refresh runs detached.

The ticket goes into the prompt through `launcher.format_ticket_context` — the
same block the agent's brief renders — so the advice is about the document the
agent will actually read, not a paraphrase of it. Output is parsed by scanning for
the first decodable JSON object, since agent CLIs prepend banners and wrap
answers in fences.

## Consequences

**Easier.** The first cheap read of the ticket happens before an hour of agent
time is committed to one interpretation of it. `--mode research` becomes
discoverable at the moment it is relevant, which is the only moment anyone would
act on it. Users get suggestions for their *own* modes for the price of one
sentence in `config.toml`.

**Harder.** `gw new` now makes a model call by default on ticket-backed sources,
which costs a few seconds of wall clock and needs an escape hatch for scripts —
hence the env switch. Ticket bodies go to whichever model `description_agent`
names; that is the same exposure descriptions already have, but it now happens at
creation time on the ticket text rather than later on the transcript.

**Accepted.** The advice can be wrong. A misfired "suggests `--mode research`" or
a nitpick dressed as an ambiguity costs a reader two seconds of reading, which is
the budget this feature is built to. What is *not* accepted is advice that looks
authoritative: the output is labelled advisory, capped at three items, and any
mode name the registry doesn't know is discarded rather than shown.

**Accepted.** `gw run` has no ticket check. Its tasks were classified when they
were created, and re-classifying on every resume would pay the call repeatedly
for advice that no longer has a decision in front of it.

## Alternatives considered

- **Act on the classification** — auto-select `--mode research` for a
  question-shaped ticket. Rejected. The user typed a command; a model's read of a
  ticket is not grounds for running a different one. It would also make the
  failure mode of a wrong classification silent, where an advisory line's failure
  mode is a sentence you ignore.

- **Classify in a detached subprocess, the way descriptions refresh.** Rejected:
  the output's whole value is being in front of the reader before the agent
  starts. Detached, it would land in a log nobody opens, or race the tmux
  `execvp` that replaces this process.

- **Hard-code the two outputs** ("is it question-shaped?" as a boolean, plus an
  ambiguity list). Rejected as the thing ADR 0009 just removed: it would name
  `research` in code, leave user modes unsuggestable forever, and make the third
  mode a second branch. `suggest_when` costs one field and generalizes.

- **Persist the classification on `Task`.** Rejected for ADR 0006's reason about
  modes: the reading is of a ticket at a moment, tickets get edited, and nothing
  downstream needs it. It is printed and forgotten by design.

- **A separate `[classify]` config table with its own agent and model.** Rejected
  as premature: two knobs (`classify_tickets`, `classify_timeout_seconds`) express
  every choice anyone has asked for, and a second agent setting would mean a
  second binary-discovery path to keep working.

- **Skip the check when stdout isn't a TTY**, which would make scripts and tests
  hermetic for free. Rejected as implicit magic: someone whose headless run
  stopped classifying would have no way to see why. The explicit `GW_CLASSIFY`
  switch says the same thing out loud.
