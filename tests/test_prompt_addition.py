import subprocess
from datetime import UTC, datetime
from pathlib import Path

from typer.testing import CliRunner

from goblin_watcher import paths, prompt_addition, state
from goblin_watcher.cli import app
from goblin_watcher.models import Project


def _init_repo(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", "-b", "main", str(path)], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.email", "t@t.example"], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.name", "tester"], check=True)
    (path / "README.md").write_text("hi")
    subprocess.run(["git", "-C", str(path), "add", "."], check=True)
    subprocess.run(["git", "-C", str(path), "commit", "-qm", "init"], check=True)


def _register_project(tmp_path: Path, name: str = "alpha") -> Project:
    repo = tmp_path / name
    _init_repo(repo)
    runner = CliRunner()
    runner.invoke(app, ["project", "new", name, "--dir", str(repo)])
    return state.get_project(name)


def test_paths_locate_addition_files(isolated_xdg: Path, tmp_path: Path) -> None:
    assert paths.global_prompt_file() == paths.config_dir() / "prompt.md"
    root = tmp_path / "repo"
    assert paths.project_prompt_file(root) == root / ".goblin" / "prompt.md"


def test_resolve_returns_empty_when_nothing_set(isolated_xdg: Path) -> None:
    assert prompt_addition.resolve(None) == ""


def test_resolve_returns_global_when_no_project_override(
    isolated_xdg: Path, tmp_path: Path
) -> None:
    proj = _register_project(tmp_path)
    prompt_addition.save_global("hello world")
    assert prompt_addition.resolve(proj) == "hello world"


def test_project_file_overrides_global(isolated_xdg: Path, tmp_path: Path) -> None:
    proj = _register_project(tmp_path)
    prompt_addition.save_global("from global")
    prompt_addition.save_project(proj, "from project")
    assert prompt_addition.resolve(proj) == "from project"


def test_empty_project_file_suppresses_global(isolated_xdg: Path, tmp_path: Path) -> None:
    """Project file presence is the override signal — an empty file disables global."""
    proj = _register_project(tmp_path)
    prompt_addition.save_global("from global")
    prompt_addition.save_project(proj, "")
    assert prompt_addition.has_project_override(proj) is True
    assert prompt_addition.resolve(proj) == ""


def test_clear_global_removes_file(isolated_xdg: Path) -> None:
    prompt_addition.save_global("x")
    assert prompt_addition.clear_global() is True
    assert not paths.global_prompt_file().exists()
    # Second clear is a no-op, not an error.
    assert prompt_addition.clear_global() is False


def test_clear_project_removes_file(isolated_xdg: Path, tmp_path: Path) -> None:
    proj = _register_project(tmp_path)
    prompt_addition.save_project(proj, "x")
    assert prompt_addition.clear_project(proj) is True
    assert not prompt_addition.has_project_override(proj)
    assert prompt_addition.clear_project(proj) is False


def test_resolve_for_task_project_falls_back_when_project_missing(
    isolated_xdg: Path,
) -> None:
    """If the task references an unregistered project, fall back to global silently."""
    prompt_addition.save_global("global only")
    assert prompt_addition.resolve_for_task_project("not-a-project") == "global only"


def test_seed_prompt_includes_addition(isolated_xdg: Path, tmp_path: Path) -> None:
    from goblin_watcher.agents.launcher import build_seed_prompt

    proj = _register_project(tmp_path)
    runner = CliRunner()
    res = runner.invoke(app, ["new", "--branch-name", "spike/foo", "--no-launch"])
    assert res.exit_code == 0, res.output
    [task] = state.list_tasks(proj)

    prompt_addition.save_global("Always run `just verify`.")
    seed = build_seed_prompt(task)
    assert "Always run `just verify`." in seed
    # Sits between description and the PR-open instruction.
    assert seed.index("Always run") < seed.index("open a PR via")


def test_seed_prompt_omits_addition_when_unset(isolated_xdg: Path, tmp_path: Path) -> None:
    from goblin_watcher.agents.launcher import build_seed_prompt

    proj = _register_project(tmp_path)
    runner = CliRunner()
    runner.invoke(app, ["new", "--branch-name", "spike/bar", "--no-launch"])
    [task] = state.list_tasks(proj)
    seed = build_seed_prompt(task)
    # No leftover placeholder, no extra blank lines around description.
    assert "{addition_block}" not in seed
    assert "\n\n\n" not in seed


def test_seed_prompt_project_addition_wins(isolated_xdg: Path, tmp_path: Path) -> None:
    from goblin_watcher.agents.launcher import build_seed_prompt

    proj = _register_project(tmp_path)
    runner = CliRunner()
    runner.invoke(app, ["new", "--branch-name", "spike/baz", "--no-launch"])
    [task] = state.list_tasks(proj)

    prompt_addition.save_global("GLOBAL ADDITION")
    prompt_addition.save_project(proj, "PROJECT ADDITION")
    seed = build_seed_prompt(task)
    assert "PROJECT ADDITION" in seed
    assert "GLOBAL ADDITION" not in seed


def _stub_project(tmp_path: Path) -> Project:
    """Make a Project record by hand without going through `gw project new`."""
    root = tmp_path / "standalone"
    root.mkdir()
    return Project(
        name="standalone",
        root=root,
        repo_url=None,
        default_branch="main",
        branch_prefix="",
        linear_team_key=None,
        created_at=datetime.now(UTC),
    )


def test_save_project_creates_meta_dir(isolated_xdg: Path, tmp_path: Path) -> None:
    proj = _stub_project(tmp_path)
    assert not (proj.root / ".goblin").exists()
    prompt_addition.save_project(proj, "x")
    assert (proj.root / ".goblin" / "prompt.md").read_text() == "x"
