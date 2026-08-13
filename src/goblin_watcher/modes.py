"""Named work modes — the registry behind `gw new --mode <name>`.

A **work mode** changes the agent's standing instructions without changing the
task (ADR 0006: modes are alternate seed templates, and a property of the
session, not the task). Before this module each mode was a boolean flag on
`gw new` plus a hand-written conflict check against every other one, so the
validation matrix grew quadratically with the number of modes (ADR 0009).

A mode is now data. Two shapes, and a mode is exactly one of them:

- **Template mode** — `template` names a brief rendered through
  `agents.launcher.build_seed_prompt` with the same task-context slots as the
  default work brief. `--prompt` composes, narrowing the focus.
- **Seed mode** — `seed` is a literal first message, used verbatim with no task
  context at all. That exists for Claude Code slash commands, whose parser only
  fires when the command is the *entire* user message. A seed mode therefore
  refuses `--prompt`: there is nowhere to put it.

Either shape may pin `agent`, and a template mode may set `requires_ticket` to
refuse sources that carry no Linear ticket or GitHub issue.

Users add their own under `[modes.<name>]` in `config.toml`; a user entry with a
built-in's name replaces it whole, mirroring how a project's `setup.toml`
replaces the global `[setup]` table. Nothing here is discovered from the
filesystem or from entry points — `AGENTS.md` rules that out for agents and the
same reasoning holds here. A mode is a prompt, not a plugin.

Extending the registry: add an entry to `BUILTIN_MODES`, and a field to
`ModeSpec` only if the new behaviour cannot be expressed by the existing ones.
Every consumer reads the spec's fields; none of them branch on a mode's name.
`suggest_when` is the field ticket classification (`classify`, ADR 0011) hangs
off: it says when gw should *suggest* the mode, so a user's own mode becomes
suggestable by writing one sentence rather than by being special-cased.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from pydantic import BaseModel

from goblin_watcher import paths
from goblin_watcher.errors import GoblinError

TEMPLATES_DIR = Path(__file__).parent / "templates"

# The context slots every template mode's brief may reference. A template that
# names anything outside this set is a config error, reported as one.
TEMPLATE_SLOTS = ("ticket_id", "title", "repos_block", "description", "addition_block", "focus")

_DEFAULT_FOCUS_LEAD = "Focus on the following in particular"


class ModeSpec(BaseModel):
    """One named work mode. Serialized to/from the `[modes.<name>]` config table."""

    # Filled in by `available()` from the table key; `[modes.research]` carries
    # no `name` of its own. Consumers use it for error messages only.
    name: str = ""
    # Brief to render, as a built-in template file name (`research_prompt.md`)
    # or a path to one of the user's own, absolute or relative to the config dir.
    template: str | None = None
    # Literal first message, used verbatim instead of any brief.
    seed: str | None = None
    # Agent this mode requires. `None` leaves the choice to --agent / config.
    agent: str | None = None
    # Refuse the mode when the task carries no Linear ticket / GitHub issue.
    requires_ticket: bool = False
    # Sentence introducing the `{focus}` paragraph `--prompt` renders into.
    focus_lead: str = _DEFAULT_FOCUS_LEAD
    # One line for `gw new --help` and error hints.
    summary: str = ""
    # The ticket shape that should make gw *suggest* this mode at task-creation
    # time (`classify`, ADR 0011), written as the condition itself: "the ticket
    # is question-shaped rather than …". Empty means never suggested, which is
    # the default and the right answer for a mode that answers to a working
    # style rather than to anything readable in the ticket.
    suggest_when: str = ""

    @property
    def allows_prompt(self) -> bool:
        """Whether `--prompt` composes with this mode.

        Derived rather than configured: a template mode has a `{focus}` slot to
        render the prompt into, a seed mode has nowhere to put it at all.
        """
        return self.template is not None


BUILTIN_MODES: dict[str, ModeSpec] = {
    "research": ModeSpec(
        name="research",
        template="research_prompt.md",
        requires_ticket=True,
        focus_lead=(
            "Focus this research on the following, and say so if it turns out to "
            "be the wrong thing to look at"
        ),
        summary="Investigate the ticket and report findings in the session; don't implement.",
        suggest_when=(
            "the ticket is question-shaped rather than change-shaped — it asks whether, "
            "why, or how something works, asks for an investigation, comparison, or "
            "recommendation, or has to be understood before anyone can say what to "
            "change. Not merely because it is large or vague"
        ),
    ),
    "adversarial-review": ModeSpec(
        name="adversarial-review",
        seed="/codex:adversarial-review --wait",
        agent="claude",
        summary="Seed `/codex:adversarial-review --wait` as the entire first message.",
        # No `suggest_when`: this is a review ritual you choose, not a shape a
        # ticket can have.
    ),
}

# Legacy boolean flags kept working as aliases for the mode they used to be.
ALIAS_FLAGS: dict[str, str] = {
    "--research": "research",
    "--adversarial-review": "adversarial-review",
}


def available(user_modes: Mapping[str, ModeSpec] | None = None) -> dict[str, ModeSpec]:
    """The built-in modes, with the user's `[modes.*]` entries merged over them.

    A user entry replaces the built-in of the same name whole — there is no
    per-field merge, so a partial override does not inherit half of a built-in
    it never mentioned.
    """
    table = dict(BUILTIN_MODES)
    for raw, spec in (user_modes or {}).items():
        key = raw.strip().lower()
        table[key] = spec.model_copy(update={"name": key})
    return table


def mode_names(user_modes: Mapping[str, ModeSpec] | None = None) -> list[str]:
    return sorted(available(user_modes))


def resolve(name: str, user_modes: Mapping[str, ModeSpec] | None = None) -> ModeSpec:
    """Look `name` up and validate it, or raise a `GoblinError` naming the rest.

    Validation happens here rather than in a Pydantic validator so a malformed
    `[modes.foo]` breaks `gw new --mode foo` and nothing else — a bad entry
    should not take down every command that loads config.
    """
    table = available(user_modes)
    key = name.strip().lower()
    spec = table.get(key)
    if spec is None:
        raise GoblinError(
            f"Unknown mode {name!r}.",
            hint=f"Available modes: {', '.join(sorted(table))}. Add your own under "
            f"[modes.<name>] in {paths.config_file()}.",
        )
    if spec.template is None and spec.seed is None:
        raise GoblinError(
            f"Mode {key!r} defines neither `template` nor `seed`.",
            hint=f"Set one of them under [modes.{key}] in {paths.config_file()}.",
        )
    if spec.template is not None and spec.seed is not None:
        raise GoblinError(
            f"Mode {key!r} defines both `template` and `seed`.",
            hint=f"A mode renders a brief or seeds a literal message, not both. "
            f"Drop one under [modes.{key}] in {paths.config_file()}.",
        )
    return spec


def template_path(spec: ModeSpec) -> Path:
    """Resolve a template mode's brief to a readable file.

    A bare file name that exists among the packaged templates wins, so
    `template = "research_prompt.md"` means the built-in brief no matter where
    the user runs from. Anything else is theirs: `~` is expanded and a relative
    path resolves against the config directory, next to `config.toml`.
    """
    raw = spec.template
    if raw is None:  # pragma: no cover - callers check `spec.template` first
        raise GoblinError(f"Mode {spec.name!r} has no template to render.")
    builtin = TEMPLATES_DIR / raw
    if "/" not in raw and builtin.is_file():
        return builtin
    candidate = Path(raw).expanduser()
    if not candidate.is_absolute():
        candidate = paths.config_dir() / candidate
    if not candidate.is_file():
        raise GoblinError(
            f"Mode {spec.name!r} points at a template that does not exist: {candidate}",
            hint="Use a built-in template name, or an absolute path / one relative "
            f"to {paths.config_dir()}.",
        )
    return candidate
