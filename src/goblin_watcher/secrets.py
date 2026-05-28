"""Resolve secrets from env, config, or 1Password (`op` CLI) references."""

from __future__ import annotations

import os
import shutil
import subprocess

from goblin_watcher import config
from goblin_watcher.errors import LinearAuthError, MissingDependencyError

OP_PREFIX = "op://"


def resolve_op_reference(ref: str) -> str:
    """Resolve an `op://vault/item/field` reference via the 1Password CLI."""
    if shutil.which("op") is None:
        raise MissingDependencyError(
            "1Password CLI (`op`) is not on PATH but the config uses an `op://` reference.",
            hint="Install the 1Password CLI from https://developer.1password.com/docs/cli/, "
            "or replace the reference with a literal value (or set LINEAR_API_KEY).",
        )
    res = subprocess.run(
        ["op", "read", ref],
        capture_output=True,
        text=True,
        check=False,
    )
    if res.returncode != 0:
        raise LinearAuthError(
            f"`op read {ref}` failed.",
            hint=(res.stderr or res.stdout).strip() or "Are you signed in? Try `op signin`.",
        )
    value = res.stdout.strip()
    if not value:
        raise LinearAuthError(f"`op read {ref}` returned an empty value.")
    return value


def get_linear_api_key(*, cfg: config.Config | None = None) -> str:
    """Resolve the Linear API key.

    Order: LINEAR_API_KEY env → config.linear.api_key (literal or `op://...`).
    """
    env_value = os.environ.get("LINEAR_API_KEY", "").strip()
    if env_value:
        return env_value

    cfg = cfg or config.load()
    configured = (cfg.linear.api_key or "").strip()
    if not configured:
        raise LinearAuthError(
            "No Linear API key configured.",
            hint=(
                'Set the LINEAR_API_KEY env var, or add `[linear] api_key = "..."` '
                "(literal or `op://vault/item/field`) to the config file."
            ),
        )

    if configured.startswith(OP_PREFIX):
        return resolve_op_reference(configured)
    return configured
