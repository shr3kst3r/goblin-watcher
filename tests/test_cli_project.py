import subprocess
from pathlib import Path

from typer.testing import CliRunner

from goblin_watcher import state
from goblin_watcher.cli import app


def _init_repo(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", "-b", "main", str(path)], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.email", "t@t.example"], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.name", "tester"], check=True)
    (path / "README.md").write_text("hi")
    subprocess.run(["git", "-C", str(path), "add", "."], check=True)
    subprocess.run(["git", "-C", str(path), "commit", "-qm", "init"], check=True)


def test_project_new_dir_registers(isolated_xdg: Path, tmp_path: Path) -> None:
    repo = tmp_path / "alpha"
    _init_repo(repo)
    runner = CliRunner()
    result = runner.invoke(app, ["project", "new", "alpha", "--dir", str(repo)])
    assert result.exit_code == 0, result.output

    proj = state.get_project("alpha")
    assert proj.root == repo.resolve()
    assert proj.default_branch == "main"


def test_project_new_repo_clones_empty_remote(isolated_xdg: Path, tmp_path: Path) -> None:
    # A brand-new remote with zero commits: registration must still succeed.
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(remote)], check=True)
    runner = CliRunner()
    result = runner.invoke(
        app, ["project", "new", "alpha", "--repo", str(remote), "--prefix", "s/"]
    )
    assert result.exit_code == 0, result.output

    proj = state.get_project("alpha")
    assert proj.default_branch == "main"
    assert proj.branch_prefix == "s/"


def test_project_new_dir_rejects_non_repo(isolated_xdg: Path, tmp_path: Path) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()
    runner = CliRunner()
    result = runner.invoke(app, ["project", "new", "plain", "--dir", str(plain)])
    assert result.exit_code != 0


def test_project_new_requires_exactly_one_source(isolated_xdg: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(app, ["project", "new", "alpha"])
    assert result.exit_code != 0
    result2 = runner.invoke(
        app, ["project", "new", "alpha", "--repo", "x", "--dir", str(Path.cwd())]
    )
    assert result2.exit_code != 0


def test_project_ls_shows_registered(isolated_xdg: Path, tmp_path: Path) -> None:
    repo = tmp_path / "alpha"
    _init_repo(repo)
    runner = CliRunner()
    runner.invoke(app, ["project", "new", "alpha", "--dir", str(repo)])
    result = runner.invoke(app, ["project", "ls"])
    assert result.exit_code == 0
    assert "alpha" in result.output


def test_project_list_shows_registered(isolated_xdg: Path, tmp_path: Path) -> None:
    repo = tmp_path / "alpha"
    _init_repo(repo)
    runner = CliRunner()
    runner.invoke(app, ["project", "new", "alpha", "--dir", str(repo)])
    result = runner.invoke(app, ["project", "list"])
    assert result.exit_code == 0
    assert "alpha" in result.output


def test_project_info_auto_picks_when_single_project(isolated_xdg: Path, tmp_path: Path) -> None:
    repo = tmp_path / "alpha"
    _init_repo(repo)
    runner = CliRunner()
    runner.invoke(app, ["project", "new", "alpha", "--dir", str(repo)])
    # No argument and one project registered → auto-picks alpha, no picker.
    result = runner.invoke(app, ["project", "info"])
    assert result.exit_code == 0, result.output
    assert "alpha" in result.output
    assert "main" in result.output


def test_project_info_no_projects_errors(isolated_xdg: Path) -> None:
    from goblin_watcher.errors import GoblinError

    runner = CliRunner()
    result = runner.invoke(app, ["project", "info"])
    assert result.exit_code != 0
    assert isinstance(result.exception, GoblinError)
    assert "No projects registered" in result.exception.message


def test_project_pull_skips_no_remote(isolated_xdg: Path, tmp_path: Path) -> None:
    repo = tmp_path / "alpha"
    _init_repo(repo)
    runner = CliRunner()
    runner.invoke(app, ["project", "new", "alpha", "--dir", str(repo)])
    result = runner.invoke(app, ["project", "pull"])
    assert result.exit_code == 0, result.output
    assert "no remote" in result.output
    assert "init" in result.output


def test_project_pull_fast_forwards_default_branch(isolated_xdg: Path, tmp_path: Path) -> None:
    # Bare "remote" + a working clone that lands new commits on it.
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(remote)], check=True)

    seed = tmp_path / "seed"
    _init_repo(seed)
    subprocess.run(["git", "-C", str(seed), "remote", "add", "origin", str(remote)], check=True)
    subprocess.run(["git", "-C", str(seed), "push", "-q", "-u", "origin", "main"], check=True)

    # The project: a fresh clone of the remote, registered via --dir.
    proj_dir = tmp_path / "alpha"
    subprocess.run(["git", "clone", "-q", str(remote), str(proj_dir)], check=True)
    runner = CliRunner()
    runner.invoke(app, ["project", "new", "alpha", "--dir", str(proj_dir)])
    before = subprocess.run(
        ["git", "-C", str(proj_dir), "rev-parse", "main"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()

    # Advance the remote by one commit from the seed clone.
    (seed / "next.txt").write_text("more")
    subprocess.run(["git", "-C", str(seed), "add", "."], check=True)
    subprocess.run(["git", "-C", str(seed), "commit", "-qm", "next"], check=True)
    subprocess.run(["git", "-C", str(seed), "push", "-q", "origin", "main"], check=True)

    result = runner.invoke(app, ["project", "pull"])
    assert result.exit_code == 0, result.output

    after = subprocess.run(
        ["git", "-C", str(proj_dir), "rev-parse", "main"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    assert after != before
    # The "Last commit" column shows the title of the commit just pulled in.
    assert "next" in result.output


def test_project_rm_with_force(isolated_xdg: Path, tmp_path: Path) -> None:
    repo = tmp_path / "alpha"
    _init_repo(repo)
    runner = CliRunner()
    runner.invoke(app, ["project", "new", "alpha", "--dir", str(repo)])
    result = runner.invoke(app, ["project", "rm", "alpha", "--force"])
    assert result.exit_code == 0, result.output
    assert "alpha" not in state.load_global().projects


def test_project_rm_when_directory_missing(isolated_xdg: Path, tmp_path: Path) -> None:
    import shutil

    repo = tmp_path / "alpha"
    _init_repo(repo)
    runner = CliRunner()
    runner.invoke(app, ["project", "new", "alpha", "--dir", str(repo)])
    # Project directory (and its .goblin metadata) no longer exists on disk.
    shutil.rmtree(repo)
    result = runner.invoke(app, ["project", "rm", "alpha", "--force"])
    assert result.exit_code == 0, result.output
    assert "alpha" not in state.load_global().projects
