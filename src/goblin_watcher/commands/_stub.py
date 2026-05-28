from goblin_watcher.errors import GoblinError


def not_yet_implemented(phase: str) -> None:
    raise GoblinError(
        f"Not yet implemented (lands in {phase}).",
        hint="Run `gw --help` to see what's already available.",
    )
