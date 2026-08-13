"""Bounded tail reads of JSONL transcripts.

Idle classification asks one narrow question — what does the *end* of the
transcript look like — and it asks it often: once per session per `gw status`
render, and `gw status --watch` renders every couple of seconds. Agent
transcripts run to tens of megabytes, so parsing a whole file per tick is not
affordable. These helpers seek to the end instead and parse only the last
`TAIL_WINDOW_BYTES`.

The window is safe for the question being asked. A tool call with no result is
by construction near the end of the file, so it is always inside the window. A
*result* whose call fell out of the window simply isn't counted as pending —
which is the same answer we'd give with the whole file. The one thing a bounded
window loses is a final record larger than the window itself; that reads as
"nothing parseable", and callers fall back to the mtime heuristic.
"""

from __future__ import annotations

import io
import json
from pathlib import Path

# 256 KiB is far more than the handful of records classification needs (agent
# transcripts run a few KiB per record) and still a single page-cached read.
TAIL_WINDOW_BYTES = 256 * 1024


def tail_lines(path: Path, window_bytes: int = TAIL_WINDOW_BYTES) -> list[str]:
    """The last `window_bytes` of `path`, split into whole lines.

    Returns [] when the file is missing or unreadable. When the window doesn't
    reach the start of the file its first line is a fragment of a record that
    began before it, so it is dropped rather than handed to a JSON parser.
    """
    try:
        with path.open("rb") as f:
            size = f.seek(0, io.SEEK_END)
            start = max(0, size - window_bytes)
            f.seek(start)
            blob = f.read()
    except OSError:
        return []
    lines = blob.decode("utf-8", errors="replace").splitlines()
    if start > 0 and lines:
        lines.pop(0)
    return lines


def tail_records(path: Path, window_bytes: int = TAIL_WINDOW_BYTES) -> list[dict]:
    """JSON objects decoded from the tail of `path`, oldest first.

    Unparseable lines are skipped: a transcript being appended to right now can
    end in a half-written record, and that must not look like a corrupt file.
    """
    out: list[dict] = []
    for raw in tail_lines(path, window_bytes):
        line = raw.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            out.append(obj)
    return out


def tail_text(text: str, max_len: int = 4000) -> str:
    """Keep the *end* of `text`, capped at `max_len`.

    The classifier cares about how a turn finished — the question is at the
    bottom of the message, not the top — so this trims from the front, unlike
    the head-capping used when rendering a transcript for the description LLM.
    """
    text = text.strip()
    if len(text) <= max_len:
        return text
    return "…" + text[-(max_len - 1) :]
