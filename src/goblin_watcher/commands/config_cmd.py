"""`gw config` — inspect and edit the user config (config.toml).

`show` / `get` operate on the *resolved* config (file merged over defaults).
`set` / `unset` / `edit` operate on the raw file, so only keys the user has
actually set are persisted — defaults stay implicit. Every write is validated
through the `Config` model before it lands, so a typo'd key or value fails
loudly instead of being discovered at the next `gw` invocation.
"""

from __future__ import annotations

import sys
import tomllib
from typing import Any

import click
import tomli_w
import typer
from pydantic import ValidationError

from goblin_watcher import config, paths
from goblin_watcher.console import console, print_success
from goblin_watcher.errors import GoblinError

app = typer.Typer()


def _read_raw() -> dict[str, Any]:
    f = paths.config_file()
    if not f.exists():
        return {}
    try:
        return tomllib.loads(f.read_text())
    except tomllib.TOMLDecodeError as e:
        raise GoblinError(
            f"Config file is not valid TOML: {e}",
            hint=f"Fix {f} by hand (or `gw config edit`).",
        ) from e


def _validate(raw: dict[str, Any]) -> None:
    try:
        config.Config.model_validate(raw)
    except ValidationError as e:
        first = e.errors()[0]
        loc = ".".join(str(p) for p in first.get("loc", ()))
        raise GoblinError(
            f"Invalid config value at {loc or '(root)'}: {first.get('msg', e)}",
        ) from e


def _write_raw(raw: dict[str, Any]) -> None:
    f = paths.config_file()
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_bytes(tomli_w.dumps(raw).encode())


def _parse_value(value: str) -> Any:
    """Interpret `value` as a TOML literal (bool/int/float/quoted string);
    fall back to a plain string for bare words like `codex`."""
    try:
        return tomllib.loads(f"v = {value}")["v"]
    except tomllib.TOMLDecodeError:
        return value


def _ensure_known_key(key: str) -> None:
    """Reject `set` on a key the Config model doesn't define.

    `config.load()` tolerates unknown keys by design (forward compat), so
    model validation alone won't catch a typo — walk the model fields instead.
    """
    cls: Any = config.Config
    parts = key.split(".")
    for i, part in enumerate(parts):
        fields = getattr(cls, "model_fields", None)
        if fields is None or part not in fields:
            raise GoblinError(
                f"Unknown config key {key!r}.",
                hint="Run `gw config show` to see the available keys.",
            )
        if i < len(parts) - 1:
            cls = fields[part].annotation


def _dig(data: dict[str, Any], key: str) -> Any:
    node: Any = data
    for part in key.split("."):
        if not isinstance(node, dict) or part not in node:
            raise GoblinError(
                f"No config key {key!r}.",
                hint="Run `gw config show` to see the available keys.",
            )
        node = node[part]
    return node


@app.command("show")
def show() -> None:
    """Print the resolved config (file merged over defaults) as TOML."""
    f = paths.config_file()
    console.print(f"[muted]file: {f}[/]")
    if not f.exists():
        console.print("[muted](not present; showing defaults)[/]")
    console.print()
    # Plain stdout: TOML's `[section]` headers would read as Rich markup.
    sys.stdout.write(tomli_w.dumps(config.load().model_dump(exclude_none=True)))


@app.command("path")
def path() -> None:
    """Print the config file path."""
    sys.stdout.write(f"{paths.config_file()}\n")


@app.command("get")
def get(key: str = typer.Argument(..., help="Dotted key, e.g. defaults.agent.")) -> None:
    """Print one resolved config value."""
    value = _dig(config.load().model_dump(exclude_none=True), key)
    if isinstance(value, dict):
        sys.stdout.write(tomli_w.dumps(value))
    else:
        sys.stdout.write(f"{value}\n")


@app.command("set")
def set_cmd(
    key: str = typer.Argument(..., help="Dotted key, e.g. defaults.agent."),
    value: str = typer.Argument(
        ..., help="Value; parsed as a TOML literal (true, 30, …) or a bare string."
    ),
) -> None:
    """Set one config value, validating before write."""
    _ensure_known_key(key)
    raw = _read_raw()
    node: Any = raw
    parts = key.split(".")
    for part in parts[:-1]:
        nxt = node.setdefault(part, {})
        if not isinstance(nxt, dict):
            raise GoblinError(f"Config key {part!r} in {key!r} is not a table.")
        node = nxt
    node[parts[-1]] = _parse_value(value)
    _validate(raw)
    _write_raw(raw)
    print_success(f"Set {key} = {node[parts[-1]]!r} ({paths.config_file()})")


@app.command("unset")
def unset(
    key: str = typer.Argument(..., help="Dotted key to remove (the default applies again)."),
) -> None:
    """Remove one key from the config file."""
    raw = _read_raw()
    node: Any = raw
    parts = key.split(".")
    for part in parts[:-1]:
        node = node.get(part) if isinstance(node, dict) else None
        if not isinstance(node, dict):
            raise GoblinError(f"No config key {key!r} in {paths.config_file()}.")
    if not isinstance(node, dict) or parts[-1] not in node:
        raise GoblinError(f"No config key {key!r} in {paths.config_file()}.")
    del node[parts[-1]]
    # Drop tables emptied by the removal so the file stays tidy.
    _prune_empty(raw)
    _write_raw(raw)
    print_success(f"Unset {key}; the built-in default applies.")


def _prune_empty(node: dict[str, Any]) -> None:
    for k in list(node):
        v = node[k]
        if isinstance(v, dict):
            _prune_empty(v)
            if not v:
                del node[k]


@app.command("edit")
def edit() -> None:
    """Open $EDITOR on the config file; the result is validated before save."""
    f = paths.config_file()
    current = f.read_text() if f.exists() else ""
    edited = click.edit(text=current, extension=".toml", require_save=True)
    if edited is None:
        console.print("[muted]No changes saved.[/]")
        return
    try:
        raw = tomllib.loads(edited)
    except tomllib.TOMLDecodeError as e:
        raise GoblinError(f"Edited config is not valid TOML: {e}", hint="Nothing saved.") from e
    _validate(raw)
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(edited)
    print_success(f"Saved {f}")
