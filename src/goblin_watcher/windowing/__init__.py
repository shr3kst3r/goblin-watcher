from goblin_watcher.windowing.base import Windower
from goblin_watcher.windowing.headless import HeadlessWindower
from goblin_watcher.windowing.inline import InlineWindower
from goblin_watcher.windowing.tmux import TmuxWindower

__all__ = [
    "WINDOWING_MODES",
    "HeadlessWindower",
    "InlineWindower",
    "TmuxWindower",
    "Windower",
    "get_windower",
]

# Every valid `windowing` value, in the order they're offered to the user.
# Single source of truth for the `--windowing` choices on the spawn commands.
WINDOWING_MODES: tuple[str, ...] = ("inline", "tmux", "headless")


def get_windower(mode: str) -> Windower:
    from goblin_watcher.errors import GoblinError

    if mode == "inline":
        return InlineWindower()
    if mode == "tmux":
        return TmuxWindower()
    if mode == "headless":
        return HeadlessWindower()
    raise GoblinError(
        f"Unknown windowing mode {mode!r}.",
        hint=f"Use one of: {', '.join(WINDOWING_MODES)}.",
    )
