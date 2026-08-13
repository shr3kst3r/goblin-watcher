"""The `modes` registry — lookup, validation, and template resolution (ADR 0009)."""

from __future__ import annotations

from pathlib import Path

import pytest

from goblin_watcher import config, modes, paths
from goblin_watcher.errors import GoblinError
from goblin_watcher.modes import ModeSpec


def test_builtins_are_available_without_config() -> None:
    assert modes.mode_names() == ["adversarial-review", "research"]
    research = modes.resolve("research")
    assert research.template == "research_prompt.md"
    assert research.requires_ticket is True
    assert research.allows_prompt is True


def test_seed_mode_refuses_a_prompt_by_construction() -> None:
    """`allows_prompt` is derived, not configured: a seed mode has no `{focus}`
    slot to render a prompt into, so no mode can be declared to accept one."""
    adversarial = modes.resolve("adversarial-review")
    assert adversarial.seed == "/codex:adversarial-review --wait"
    assert adversarial.agent == "claude"
    assert adversarial.allows_prompt is False


def test_lookup_is_case_and_whitespace_insensitive() -> None:
    assert modes.resolve("  Research ").name == "research"


def test_unknown_mode_lists_the_known_ones() -> None:
    with pytest.raises(GoblinError) as excinfo:
        modes.resolve("reserch")
    assert "Unknown mode 'reserch'" in str(excinfo.value)
    assert "adversarial-review, research" in (excinfo.value.hint or "")


def test_user_modes_merge_over_the_builtins() -> None:
    user = {"spike": ModeSpec(template="research_prompt.md")}
    assert modes.mode_names(user) == ["adversarial-review", "research", "spike"]
    spec = modes.resolve("spike", user)
    # The name comes from the table key — `[modes.spike]` carries none of its own.
    assert spec.name == "spike"


def test_a_user_entry_replaces_a_builtin_whole() -> None:
    """No per-field merge: an override that never mentions `requires_ticket`
    does not inherit the built-in's."""
    user = {"research": ModeSpec(seed="/my-research")}
    spec = modes.resolve("research", user)
    assert spec.seed == "/my-research"
    assert spec.template is None
    assert spec.requires_ticket is False


def test_a_mode_with_neither_template_nor_seed_is_refused() -> None:
    with pytest.raises(GoblinError) as excinfo:
        modes.resolve("empty", {"empty": ModeSpec(agent="claude")})
    assert "defines neither `template` nor `seed`" in str(excinfo.value)


def test_a_mode_with_both_template_and_seed_is_refused() -> None:
    with pytest.raises(GoblinError) as excinfo:
        modes.resolve("both", {"both": ModeSpec(template="research_prompt.md", seed="/x")})
    assert "defines both `template` and `seed`" in str(excinfo.value)


def test_a_broken_mode_only_breaks_its_own_lookup() -> None:
    """Validation happens at resolve time, not in a Pydantic validator, so a
    malformed `[modes.foo]` doesn't take down every command that loads config."""
    user = {"broken": ModeSpec()}
    assert modes.resolve("research", user).name == "research"


def test_builtin_template_resolves_to_the_packaged_file() -> None:
    path = modes.template_path(modes.resolve("research"))
    assert path == modes.TEMPLATES_DIR / "research_prompt.md"
    assert path.is_file()


def test_relative_user_template_resolves_against_the_config_dir(isolated_xdg: Path) -> None:
    paths.config_dir().mkdir(parents=True, exist_ok=True)
    brief = paths.config_dir() / "spike_prompt.md"
    brief.write_text("Spike: {title}\n")
    spec = modes.resolve("spike", {"spike": ModeSpec(template="spike_prompt.md")})
    assert modes.template_path(spec) == brief


def test_absolute_user_template_is_taken_as_given(isolated_xdg: Path, tmp_path: Path) -> None:
    brief = tmp_path / "elsewhere" / "brief.md"
    brief.parent.mkdir()
    brief.write_text("Brief: {title}\n")
    spec = modes.resolve("spike", {"spike": ModeSpec(template=str(brief))})
    assert modes.template_path(spec) == brief


def test_missing_user_template_reports_where_it_looked(isolated_xdg: Path) -> None:
    spec = modes.resolve("spike", {"spike": ModeSpec(template="nope.md")})
    with pytest.raises(GoblinError) as excinfo:
        modes.template_path(spec)
    assert "points at a template that does not exist" in str(excinfo.value)
    assert str(paths.config_dir()) in str(excinfo.value)


def test_modes_round_trip_through_the_config_file(isolated_xdg: Path) -> None:
    """`[modes.*]` is a real config table, so a written config reloads intact."""
    cfg = config.load()
    cfg.modes["spike"] = ModeSpec(template="spike_prompt.md", requires_ticket=True)
    config.save(cfg)
    reloaded = config.load()
    assert reloaded.modes["spike"].template == "spike_prompt.md"
    assert reloaded.modes["spike"].requires_ticket is True
