from pathlib import Path

from goblin_watcher import paths


def test_data_dir_under_xdg_data_home(isolated_xdg: Path) -> None:
    assert paths.data_dir() == isolated_xdg / "data" / "goblin-watcher"


def test_config_dir_under_xdg_config_home(isolated_xdg: Path) -> None:
    assert paths.config_dir() == isolated_xdg / "config" / "goblin-watcher"


def test_state_file_lives_in_data_dir(isolated_xdg: Path) -> None:
    assert paths.state_file().parent == paths.data_dir()


def test_project_meta_dir() -> None:
    root = Path("/tmp/somewhere/repo")
    assert paths.project_meta_dir(root) == root / ".goblin"
    assert paths.project_tasks_dir(root) == root / ".goblin" / "tasks"


def test_worktree_root_default_and_override() -> None:
    root = Path("/tmp/somewhere/repo")
    assert paths.worktree_root(root) == root / ".worktrees"
    custom = Path("/tmp/elsewhere")
    assert paths.worktree_root(root, custom) == custom


def test_projects_root_under_home(isolated_xdg: Path) -> None:
    assert paths.projects_root() == isolated_xdg / "home" / "goblin"
