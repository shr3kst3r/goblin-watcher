import platform
import sys

from goblin_watcher import __version__
from goblin_watcher.console import console


def version() -> None:
    console.print(f"[bold]goblin-watcher[/] {__version__}")
    console.print(f"  python  {sys.version.split()[0]} ({platform.python_implementation()})")
    console.print(f"  os      {platform.platform()}")
