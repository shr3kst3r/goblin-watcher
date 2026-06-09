import random
import re

_SLUG_RE = re.compile(r"[^a-z0-9]+")
_MAX_SLUG_LEN = 40
_MAX_TOTAL_LEN = 80

# Friendly two-word branch names. Keep these short, evocative, and unambiguous.
_ADJECTIVES = (
    "brisk",
    "calm",
    "clever",
    "cosmic",
    "crisp",
    "curious",
    "dapper",
    "dusky",
    "fluffy",
    "gentle",
    "golden",
    "jolly",
    "lively",
    "lucky",
    "mellow",
    "merry",
    "misty",
    "nimble",
    "plucky",
    "quiet",
    "rapid",
    "snappy",
    "spry",
    "sunny",
    "swift",
    "tidy",
    "vivid",
    "witty",
    "zesty",
    "bright",
)

_NOUNS = (
    "badger",
    "beacon",
    "breeze",
    "canyon",
    "cedar",
    "comet",
    "dune",
    "ember",
    "falcon",
    "fern",
    "finch",
    "fjord",
    "grove",
    "harbor",
    "heron",
    "hollow",
    "lark",
    "lynx",
    "maple",
    "meadow",
    "oak",
    "otter",
    "pine",
    "prairie",
    "quail",
    "ridge",
    "river",
    "shore",
    "sparrow",
    "willow",
)


def slugify(text: str, max_len: int = _MAX_SLUG_LEN) -> str:
    s = _SLUG_RE.sub("-", text.lower()).strip("-")
    if not s:
        s = "task"
    return s[:max_len].rstrip("-")


# Single evocative words for the auto-generated suffix.
_WORDS = _ADJECTIVES + _NOUNS


def random_branch_name(project: str, rng: random.Random | None = None) -> str:
    """Return a `{project}-{word}` branch name (e.g. `goblin-watcher-falcon`)."""
    r = rng or random
    return f"{slugify(project)}-{r.choice(_WORDS)}"


def random_scratch_name(rng: random.Random | None = None) -> str:
    """Return an `{adjective}-{noun}` scratch-space name (e.g. `misty-falcon`)."""
    r = rng or random
    return f"{r.choice(_ADJECTIVES)}-{r.choice(_NOUNS)}"


def branch_slug(linear_id: str | None, title: str, prefix: str = "") -> str:
    """Build a branch slug.

    With Linear: `{prefix}{linear-id-lower}-{slug}` (e.g. `eng-123-add-rate-limit`).
    Without Linear: `{prefix}{slug}`.
    """
    lid = linear_id.lower() if linear_id else None
    body = slugify(title)
    if lid:
        body = f"{lid}-{body}" if body and body != "task" else lid
    out = f"{prefix}{body}"
    return out[:_MAX_TOTAL_LEN].rstrip("-")
