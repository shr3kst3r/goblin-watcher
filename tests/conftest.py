from collections.abc import Iterator
from pathlib import Path

import pytest


@pytest.fixture
def isolated_xdg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Redirect XDG dirs and $HOME to tmp_path so gw state + clones are sandboxed."""
    data = tmp_path / "data"
    config = tmp_path / "config"
    home = tmp_path / "home"
    data.mkdir()
    config.mkdir()
    home.mkdir()
    monkeypatch.setenv("XDG_DATA_HOME", str(data))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config))
    monkeypatch.setenv("HOME", str(home))
    yield tmp_path
