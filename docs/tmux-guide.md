# Using `gw` with tmux

A beginner-friendly guide to the tmux windowing mode in goblin-watcher.

## What tmux buys you

The main reason to opt into tmux mode is **persistence**. A tmux session keeps
running even if you close every terminal window. Reopen the terminal, run
`tmux attach -t goblin`, and every agent you spawned is still right where you
left it — still running, still mid-conversation. Native terminal splits die
when you close the window; tmux sessions don't.

If you don't need that, the default `inline` mode (one agent per terminal pane)
is simpler. The rest of this guide is for when you do want the persistence.

## What `gw` does with tmux

When `windowing = "tmux"`, `gw` doesn't spawn agents in your current terminal.
Instead it sets up a layout inside a long-running tmux session:

```
tmux session: "goblin"
├── window "eng-123"        ← one window per task
│   ├── pane 0              ← claude session
│   └── pane 1              ← second claude session, same task
├── window "eng-124"        ← another task
│   └── pane 0              ← codex session
└── window "eng-125"
    └── pane 0
```

- **Session**: the outer container. There's exactly one, named `goblin` by default.
- **Window**: like a browser tab. One per task (named after the task ID).
- **Pane**: a split inside a window. One per agent session. A second session
  on the same task adds a pane via `split-window`.

You'll see this exact structure as you spawn more agents.

## Enabling tmux mode

Two ways:

**Per invocation** — pass `--windowing tmux`:

```sh
gw new --linear ENG-123 --windowing tmux
```

**Persistent** — set it in `~/.config/goblin-watcher/config.toml`:

```toml
[defaults]
windowing = "tmux"

[tmux]
session_name = "goblin"      # the tmux session name (default: "goblin")
attach_on_spawn = true       # auto-attach after spawning (default: true)
split = "vertical"           # second+ sessions on a task: "vertical" stacks top/bottom (default), "horizontal" is side-by-side
```

With `attach_on_spawn = true` (the default), `gw` will drop you into tmux
automatically — you don't need to remember to run `tmux attach`. If you'd
rather stay in your shell after spawning, set it to `false` and attach
manually later.

## Connecting to the goblin session

If `gw` attached you automatically, you can skip this section — you're already
there. This is for when you've opened a fresh terminal and want to reconnect
to agents that are still running in the background.

### Is it running?

`tmux ls` lists every tmux session on the machine:

```sh
$ tmux ls
goblin: 4 windows (created Wed May 20 09:14:02 2026)
```

If the line starts with `goblin:`, the session exists and you can attach. If
`tmux ls` prints nothing (or `no server running on /tmp/tmux-...`), there's
no session yet — the next `gw new ...` will create one.

The number of windows tells you how many tasks are currently active in there.

### Attach

```sh
tmux attach -t goblin
```

`-t goblin` means "the session named `goblin`". If you renamed the session in
`config.toml`, use that name instead. `tmux attach` with no `-t` flag attaches
to the most recently used session — usually fine if you only have the one.

You'll land in whatever window/pane was focused when you last detached. The
**status bar at the bottom** lists every window — that's your map of all
running tasks.

### What the status bar tells you

A typical status bar looks like this:

```
[goblin] 0:intro  1:eng-123*  2:eng-124-  3:eng-125
```

- `[goblin]` — the session name on the left.
- `0:intro`, `1:eng-123`, ... — each window's index and name. The name is the
  task ID. `intro` is the placeholder window `gw` creates the very first time
  it spins up the session; you can ignore or close it.
- `*` after a name marks the **current** window.
- `-` marks the **previous** window (where `ctrl-b l` will take you).

### Finding a specific task

Three options, increasingly useful as you have more windows:

1. **Cycle**: `ctrl-b n` (next) / `ctrl-b p` (previous) walk through windows
   in order. Fine for two or three.
2. **Jump by index**: `ctrl-b 2` jumps directly to window `2`. The indexes are
   shown in the status bar.
3. **Pick by name** (best when you have many): `ctrl-b w` opens the
   interactive window list. See below.

### The interactive window list (`ctrl-b w`)

This is the most useful navigator once you have more than three or four tasks
in flight. Press `ctrl-b w` and tmux opens a tree view of every session,
window, and pane on the machine:

```
(0) + goblin: 4 windows (attached)
│     0: intro
│     1: eng-123 (2 panes)
│  →  2: eng-124*
│     3: eng-125
```

The right half of the screen shows a **live preview** of the highlighted
window or pane — you can see what an agent is doing without jumping to it.

Navigation inside the picker:

| Action                                  | Key                  |
| --------------------------------------- | -------------------- |
| Move up / down                          | `↑` `↓` (or `k` `j`) |
| Expand / collapse a session or window   | `→` `←` (or `tab`)   |
| Jump to the highlighted window / pane   | `enter`              |
| Filter — start typing to narrow         | `f`, then type       |
| Search the *contents* of panes          | `/`, then type       |
| Close (kill) the highlighted window     | `x`, confirm with `y`|
| Quit the picker without doing anything  | `q` or `esc`         |

A few tips:

- **`f` (filter) vs `/` (search)**: `f` filters by *name* — type `eng-124` and
  only the matching window stays visible. `/` searches the *visible text in
  the pane* — useful for "which agent was talking about X?". Press `n` /
  `shift-n` to walk through search matches.
- **Closing windows**: `x` then `y` is the fastest way to clean up a window
  whose agent has already exited. The pane will show `[exited]` in the
  preview when this is safe to do.
- **Just looking**: arrowing around with the preview pane open is a great way
  to check on every agent's status without disturbing any of them. Press `q`
  when you're done and tmux returns you to wherever you were.

If you only ever learn one tmux command beyond detach, make it this one.

### Listing windows from outside tmux

If you don't remember a task's ID, you can also list windows from the shell
without attaching:

```sh
tmux list-windows -t goblin -F '#I #W'
# 0 intro
# 1 eng-123
# 2 eng-124
# 3 eng-125
```

### Multiple terminals attached to the same session

It's fine — and sometimes useful — to attach to `goblin` from more than one
terminal at once (e.g. one on each monitor). Both views are live: keystrokes
in one show up in the other. If they look cramped, the smaller terminal is
forcing the window size; close it or detach from it (`ctrl-b d`) and the other
will resize.

## The five tmux things to learn first

Tmux's defaults are weird if you're not used to it. The most important thing
to know is the **prefix key**: by default it's `ctrl-b`. You press `ctrl-b`,
release, *then* press a second key.

Here are the five that get you 90% of the way:

| Action                       | Keys                  |
| ---------------------------- | --------------------- |
| Next window (task)           | `ctrl-b  n`           |
| Previous window              | `ctrl-b  p`           |
| Pick a window by name        | `ctrl-b  w`           |
| Cycle panes in this window   | `ctrl-b  o`           |
| Detach (leave tmux running)  | `ctrl-b  d`           |

Detach is the one that surprises people: closing the terminal kills your
ssh-style sessions, but `ctrl-b d` *intentionally* leaves everything running
in the background. That's the whole point of tmux. Run `tmux attach -t goblin`
to come back.

Two more that are nice once you're comfortable:

| Action                              | Keys             |
| ----------------------------------- | ---------------- |
| Zoom into the current pane (toggle) | `ctrl-b  z`      |
| Scroll back in the pane (copy mode) | `ctrl-b  [`      |

In copy mode, arrow keys / page-up work. Press `q` to exit copy mode.

## A typical session

```sh
# Outside tmux. Spawn a fresh task — gw attaches you to tmux automatically.
$ gw new --linear ENG-123
# (now inside tmux, looking at claude running in pane 0 of window "eng-123")

# Spawn a second session on the same task in parallel. You're already in tmux,
# so gw uses `select-window` to bring you to the right place — no re-attach.
$ gw new --linear ENG-123 --new
# (window "eng-123" now has two panes; you're focused on the new one)

# Switch back to the first session within the task.
ctrl-b o

# Spawn a totally different task; gw creates a new window.
$ gw new --linear ENG-124

# Hop between tasks.
ctrl-b n           # next window
ctrl-b w           # pick from a list

# Done for the day, but want everything to keep running.
ctrl-b d           # detach. Close the terminal whenever.

# Tomorrow:
$ tmux attach -t goblin
# Everything is still there.
```

## Common questions

**"I'm inside tmux and ran `gw new ...` — why didn't it re-attach me?"**
That's correct behavior. `gw` detects `$TMUX` and uses `select-window` to bring
you to the right window without disturbing the rest of your layout. Look for
the new window in your bottom status bar.

**"I killed my terminal but I think the agent is still running."**
It is. `tmux attach -t goblin` to reconnect. If you're not sure, `tmux ls`
lists running sessions.

**"My status bar says `goblin: 4 windows (attached)`. What does `attached` mean?"**
It means *some* terminal somewhere is currently displaying this tmux session.
Detaching (`ctrl-b d`) drops the "attached" status without killing anything.

**"I want a different prefix key — `ctrl-b` is awkward."**
Tmux's most common rebind is to `ctrl-a`. Add this to `~/.tmux.conf`:

```tmux
unbind C-b
set-option -g prefix C-a
bind-key C-a send-prefix
```

Then `tmux kill-server` (or just kill the `goblin` session) and start fresh.
This is a tmux config, not a `gw` config — it applies to every tmux session
on your machine.

**"How do I close a single agent without killing the whole tmux session?"**
Inside the pane, exit the agent normally (e.g. `/exit` in claude, or `ctrl-d`).
The pane closes; the tmux session lives on.

**"How do I nuke everything and start clean?"**
`tmux kill-session -t goblin`. The next `gw new ...` will recreate it.

## When to use it

If your workflow is "one or two agents at a time, in this terminal window,
right now" — the default `inline` mode is simpler.

If your workflow is "I have five tasks in flight, I want them all running
overnight, and I want to come back to them tomorrow from a fresh terminal" —
tmux is the right tool. That's what its persistence buys you.
