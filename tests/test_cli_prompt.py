import subprocess
from pathlib import Path
from unittest.mock import patch

from typer.testing import CliRunner

from goblin_watcher import prompt_addition, state
from goblin_watcher.cli import _inject_project_sentinel, app


def _invoke(runner: CliRunner, argv: list[str], **kwargs):
    """Invoke `app` with the argv preprocessing that `cli.main` applies in production.

    Tests that pass `--project` without a value rely on
    `_inject_project_sentinel` to splice in the project-picker sentinel.
    """
    return runner.invoke(app, _inject_project_sentinel(argv), **kwargs)


def _init_repo(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", "-b", "main", str(path)], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.email", "t@t.example"], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.name", "tester"], check=True)
    (path / "README.md").write_text("hi")
    subprocess.run(["git", "-C", str(path), "add", "."], check=True)
    subprocess.run(["git", "-C", str(path), "commit", "-qm", "init"], check=True)


def _bootstrap(tmp_path: Path) -> None:
    repo = tmp_path / "alpha"
    _init_repo(repo)
    runner = CliRunner()
    runner.invoke(app, ["project", "new", "alpha", "--dir", str(repo)])


def test_set_writes_to_global_by_default(isolated_xdg: Path) -> None:
    runner = CliRunner()
    res = runner.invoke(app, ["prompt", "set", "use uv everywhere"])
    assert res.exit_code == 0, res.output
    assert prompt_addition.load_global() == "use uv everywhere"


def test_set_project_writes_to_project_file(isolated_xdg: Path, tmp_path: Path) -> None:
    _bootstrap(tmp_path)
    runner = CliRunner()
    res = _invoke(runner, ["prompt", "set", "alpha-specific note", "--project"])
    assert res.exit_code == 0, res.output
    proj = state.get_project("alpha")
    assert prompt_addition.load_project(proj) == "alpha-specific note"


def test_set_project_without_any_projects_errors(isolated_xdg: Path) -> None:
    runner = CliRunner()
    res = _invoke(runner, ["prompt", "set", "foo", "--project"])
    assert res.exit_code != 0
    assert "No projects registered" in str(res.exception.message)


def test_set_global_and_project_together_errors(isolated_xdg: Path, tmp_path: Path) -> None:
    _bootstrap(tmp_path)
    runner = CliRunner()
    res = _invoke(runner, ["prompt", "set", "x", "--global", "--project"])
    assert res.exit_code != 0


def test_set_project_by_name_writes_to_named_project(isolated_xdg: Path, tmp_path: Path) -> None:
    # Register two projects; target the second by name.
    _bootstrap(tmp_path)
    beta = tmp_path / "beta"
    _init_repo(beta)
    runner = CliRunner()
    runner.invoke(app, ["project", "new", "beta", "--dir", str(beta)])
    res = runner.invoke(app, ["prompt", "set", "beta-note", "--project", "beta"])
    assert res.exit_code == 0, res.output
    assert prompt_addition.load_project(state.get_project("beta")) == "beta-note"
    # The other project ('alpha') should be untouched.
    assert not prompt_addition.has_project_override(state.get_project("alpha"))


def test_set_project_by_unknown_name_errors(isolated_xdg: Path, tmp_path: Path) -> None:
    _bootstrap(tmp_path)
    runner = CliRunner()
    res = runner.invoke(app, ["prompt", "set", "x", "--project", "no-such-project"])
    assert res.exit_code != 0
    assert "no-such-project" in str(res.exception.message)


def test_show_project_by_name(isolated_xdg: Path, tmp_path: Path) -> None:
    _bootstrap(tmp_path)
    beta = tmp_path / "beta"
    _init_repo(beta)
    runner = CliRunner()
    runner.invoke(app, ["project", "new", "beta", "--dir", str(beta)])
    prompt_addition.save_project(state.get_project("beta"), "beta-only")
    res = runner.invoke(app, ["prompt", "show", "--project", "beta"])
    assert res.exit_code == 0, res.output
    assert "beta-only" in res.output
    assert "project 'beta'" in res.output


def test_clear_project_by_name(isolated_xdg: Path, tmp_path: Path) -> None:
    _bootstrap(tmp_path)
    beta = tmp_path / "beta"
    _init_repo(beta)
    runner = CliRunner()
    runner.invoke(app, ["project", "new", "beta", "--dir", str(beta)])
    proj = state.get_project("beta")
    prompt_addition.save_project(proj, "x")
    res = runner.invoke(app, ["prompt", "clear", "--project", "beta", "--force"])
    assert res.exit_code == 0, res.output
    assert not prompt_addition.has_project_override(proj)


def test_set_from_stdin(isolated_xdg: Path) -> None:
    runner = CliRunner()
    res = runner.invoke(app, ["prompt", "set"], input="piped content\n")
    assert res.exit_code == 0, res.output
    assert prompt_addition.load_global() == "piped content\n"


def test_show_default_reports_no_addition(isolated_xdg: Path) -> None:
    runner = CliRunner()
    res = runner.invoke(app, ["prompt", "show"])
    assert res.exit_code == 0, res.output
    assert "no prompt addition" in res.output


def test_show_default_prefers_project(isolated_xdg: Path, tmp_path: Path) -> None:
    _bootstrap(tmp_path)
    prompt_addition.save_global("from global")
    proj = state.get_project("alpha")
    prompt_addition.save_project(proj, "from project")
    runner = CliRunner()
    res = runner.invoke(app, ["prompt", "show"])
    assert res.exit_code == 0, res.output
    assert "from project" in res.output
    assert "overrides global" in res.output


def test_show_global_only(isolated_xdg: Path, tmp_path: Path) -> None:
    _bootstrap(tmp_path)
    prompt_addition.save_global("g")
    prompt_addition.save_project(state.get_project("alpha"), "p")
    runner = CliRunner()
    res = runner.invoke(app, ["prompt", "show", "--global"])
    assert res.exit_code == 0, res.output
    # Outputs the global text, never mentioning the project override.
    assert "g" in res.output
    assert "from project" not in res.output


def test_show_project_only_when_no_override(isolated_xdg: Path, tmp_path: Path) -> None:
    _bootstrap(tmp_path)
    prompt_addition.save_global("g")
    runner = CliRunner()
    res = _invoke(runner, ["prompt", "show", "--project"])
    assert res.exit_code == 0, res.output
    assert "no addition file" in res.output


def test_clear_global(isolated_xdg: Path) -> None:
    prompt_addition.save_global("to remove")
    runner = CliRunner()
    res = runner.invoke(app, ["prompt", "clear", "--force"])
    assert res.exit_code == 0, res.output
    assert prompt_addition.load_global() == ""


def test_clear_project(isolated_xdg: Path, tmp_path: Path) -> None:
    _bootstrap(tmp_path)
    proj = state.get_project("alpha")
    prompt_addition.save_project(proj, "x")
    runner = CliRunner()
    res = _invoke(runner, ["prompt", "clear", "--project", "--force"])
    assert res.exit_code == 0, res.output
    assert not prompt_addition.has_project_override(proj)


def test_clear_nothing_to_clear_is_noop(isolated_xdg: Path) -> None:
    runner = CliRunner()
    res = runner.invoke(app, ["prompt", "clear", "--force"])
    assert res.exit_code == 0, res.output
    assert "No global prompt addition to clear" in res.output


def test_edit_writes_editor_output(isolated_xdg: Path) -> None:
    runner = CliRunner()
    with patch("click.edit", return_value="edited content\n"):
        res = runner.invoke(app, ["prompt", "edit"])
    assert res.exit_code == 0, res.output
    assert prompt_addition.load_global() == "edited content\n"


def test_edit_skipped_save_is_noop(isolated_xdg: Path) -> None:
    prompt_addition.save_global("before")
    runner = CliRunner()
    with patch("click.edit", return_value=None):
        res = runner.invoke(app, ["prompt", "edit"])
    assert res.exit_code == 0, res.output
    assert prompt_addition.load_global() == "before"
    assert "No changes saved" in res.output
