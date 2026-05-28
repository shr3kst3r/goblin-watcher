@AGENTS.md

Loop closure: after any code change, run `just verify` (or, equivalently,
`uv run ruff check . && uv run ruff format --check . && uv run ty check src && uv run pytest -q`)
and only consider the task complete when that command set exits zero.
