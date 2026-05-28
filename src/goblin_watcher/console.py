from rich.console import Console
from rich.theme import Theme

AGENT_STYLES = {
    "claude": "bold magenta",
    "codex": "bold cyan",
    "gemini": "bold green",
}

_theme = Theme(
    {
        "error": "bold red",
        "hint": "yellow",
        "success": "bold green",
        "muted": "dim",
        "agent.claude": AGENT_STYLES["claude"],
        "agent.codex": AGENT_STYLES["codex"],
        "agent.gemini": AGENT_STYLES["gemini"],
    }
)

console = Console(theme=_theme, highlight=False)
err_console = Console(theme=_theme, stderr=True, highlight=False)


def print_error(message: str, hint: str | None = None) -> None:
    err_console.print(f"[error]Error[/]: {message}")
    if hint:
        err_console.print(f"[hint]Hint[/]: {hint}")


def print_success(message: str) -> None:
    console.print(f"[success]{message}[/]")


def print_settings(items: list[tuple[str, str]]) -> None:
    """Print an aligned ``key  value`` block beneath a success line."""
    width = max((len(k) for k, _ in items), default=0)
    for key, value in items:
        console.print(f"  [muted]{key.ljust(width)}[/]  {value}")


def agent_badge(name: str) -> str:
    return f"[agent.{name}]{name}[/]" if name in AGENT_STYLES else name
