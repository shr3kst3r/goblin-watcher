from goblin_watcher.windowing.base import Windower
from goblin_watcher.windowing.inline import InlineWindower
from goblin_watcher.windowing.tmux import TmuxWindower

__all__ = ["InlineWindower", "TmuxWindower", "Windower", "get_windower"]


def get_windower(mode: str) -> Windower:
    from goblin_watcher.errors import GoblinError

    if mode == "inline":
        return InlineWindower()
    if mode == "tmux":
        return TmuxWindower()
    raise GoblinError(
        f"Unknown windowing mode {mode!r}.",
        hint="Use 'inline' or 'tmux'.",
    )
