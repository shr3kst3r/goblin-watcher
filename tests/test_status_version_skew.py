"""A stale reader must say so, not render an empty tree (gh-51).

`Task` is `extra="forbid"`, so a record written by a newer gw fails to validate
in an older one. `state.list_tasks` used to swallow that and skip the file, which
is how a long-running `gw status --watch` — holding the models it imported at
startup while an editable install moved underneath it — ended up showing nothing
at all, with a footer blaming idleness.
"""

from __future__ import annotations

import json
import os
import subprocess
from datetime import UTC, datetime
from pathlib import Path

from typer.testing import CliRunner

from goblin_watcher import state
from goblin_watcher.cli import app
from goblin_watcher.models import Project, SessionRecord


def _init_repo(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", "-b", "main", str(path)], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.name", "tester"], check=True)
    (path / "README.md").write_text("hi")
    subprocess.run(["git", "-C", str(path), "add", "."], check=True)
    subprocess.run(["git", "-C", str(path), "commit", "-qm", "init"], check=True)


def _project_with_task(tmp_path: Path) -> tuple[CliRunner, Project]:
    repo = tmp_path / "alpha"
    _init_repo(repo)
    runner = CliRunner()
    runner.invoke(app, ["project", "new", "alpha", "--dir", str(repo)])
    runner.invoke(app, ["new", "--branch-name", "spike/foo", "--no-launch"])
    return runner, state.get_project("alpha")


def _write_future_field(proj: Project, task_id: str, field: str = "quantum_sha") -> Path:
    """Stamp a field this build has never heard of into a task record.

    Stands in for the real case, which is the reverse: the *record* is current
    and the running process's model is old. Same validation failure either way.
    """
    path = state.task_file(proj, task_id)
    payload = json.loads(path.read_text())
    payload[field] = "written-by-a-newer-gw"
    path.write_text(json.dumps(payload))
    return path


# --- state.scan_tasks -------------------------------------------------------


def test_scan_tasks_reports_unknown_fields_instead_of_dropping_the_record(
    isolated_xdg: Path, tmp_path: Path
) -> None:
    _runner, proj = _project_with_task(tmp_path)
    [task] = state.list_tasks(proj)
    _write_future_field(proj, task.id)

    scan = state.scan_tasks(proj)
    assert scan.tasks == []
    [bad] = scan.unreadable
    assert bad.task_id == task.id
    assert bad.unknown_fields == ("quantum_sha",)
    assert bad.newer_schema is True
    assert scan.newer_schema == [bad]


def test_scan_tasks_separates_corruption_from_version_skew(
    isolated_xdg: Path, tmp_path: Path
) -> None:
    _runner, proj = _project_with_task(tmp_path)
    [task] = state.list_tasks(proj)
    state.task_file(proj, task.id).write_text("{not json at all")

    scan = state.scan_tasks(proj)
    [bad] = scan.unreadable
    # Unparseable is not skew: nothing here says a newer gw was involved, so the
    # remedy is `gw doctor`, not "restart your watch".
    assert bad.unknown_fields == ()
    assert bad.newer_schema is False
    assert scan.newer_schema == []


def test_list_tasks_still_returns_only_the_healthy_records(
    isolated_xdg: Path, tmp_path: Path
) -> None:
    _runner, proj = _project_with_task(tmp_path)
    [task] = state.list_tasks(proj)
    _write_future_field(proj, task.id)
    assert state.list_tasks(proj) == []


# --- gw status --------------------------------------------------------------


def test_status_names_the_skew_instead_of_rendering_an_empty_tree(
    isolated_xdg: Path, tmp_path: Path
) -> None:
    runner, proj = _project_with_task(tmp_path)
    [task] = state.list_tasks(proj)
    _write_future_field(proj, task.id)

    res = runner.invoke(app, ["status"])
    assert res.exit_code == 0, res.output
    assert "written by a newer gw" in res.output
    assert "quantum_sha" in res.output
    assert "Restart this command" in res.output


def test_active_does_not_claim_idleness_it_could_not_observe(
    isolated_xdg: Path, tmp_path: Path
) -> None:
    runner, proj = _project_with_task(tmp_path)
    [task] = state.list_tasks(proj)
    _write_future_field(proj, task.id)

    res = runner.invoke(app, ["status", "--active"])
    assert res.exit_code == 0, res.output
    assert "written by a newer gw" in res.output
    # The old message asserted three things none of which were checked.
    assert "no session is mid tool call" not in res.output


def test_status_points_a_genuinely_broken_record_at_doctor(
    isolated_xdg: Path, tmp_path: Path
) -> None:
    runner, proj = _project_with_task(tmp_path)
    [task] = state.list_tasks(proj)
    state.task_file(proj, task.id).write_text("{not json at all")

    res = runner.invoke(app, ["status"])
    assert res.exit_code == 0, res.output
    assert "could not be read" in res.output
    assert "gw doctor" in res.output
    assert "written by a newer gw" not in res.output


def test_healthy_records_produce_no_banner(isolated_xdg: Path, tmp_path: Path) -> None:
    runner, _proj = _project_with_task(tmp_path)
    res = runner.invoke(app, ["status"])
    assert res.exit_code == 0, res.output
    assert "could not be read" not in res.output
    assert "newer gw" not in res.output


# --- gw doctor --------------------------------------------------------------


def test_doctor_reports_an_unreadable_record_and_never_offers_to_repair_it(
    isolated_xdg: Path, tmp_path: Path
) -> None:
    from goblin_watcher import drift

    _runner, proj = _project_with_task(tmp_path)
    [task] = state.list_tasks(proj)
    _write_future_field(proj, task.id)

    findings = drift.detect([proj])
    [f] = [f for f in findings if f.kind == "unreadable-record"]
    assert f.task_id == task.id
    # Repairing would turn a version skew into data loss: the record is the only
    # copy, and it is almost certainly *fine*.
    assert f.repairable is False
    assert "unreadable-record" not in drift.REPAIRABLE_KINDS
    assert "quantum_sha" in f.detail


def test_doctor_exits_non_zero_on_an_unreadable_record(isolated_xdg: Path, tmp_path: Path) -> None:
    runner, proj = _project_with_task(tmp_path)
    [task] = state.list_tasks(proj)
    _write_future_field(proj, task.id)

    res = runner.invoke(app, ["doctor"])
    assert res.exit_code != 0, res.output
    assert "unreadable task record" in res.output


# --- `--active` must not hide a task it never classified --------------------


def test_active_heals_a_session_with_no_recorded_transcript_path(
    isolated_xdg: Path, tmp_path: Path, monkeypatch
) -> None:
    """A brand-new session has no `transcript_path` yet, so it classifies as
    `unknown` and `_in_flight` says no. The refresh that records the path used to
    sit *behind* the `--active` filter, so the task was invisible for as long as
    it ran — worst under `--watch`, which never reconciles.
    """
    from goblin_watcher.agents import claude as claude_agent

    runner, proj = _project_with_task(tmp_path)
    [task] = state.list_tasks(proj)

    # A live transcript on disk, in the place claude would put it.
    project_dir = tmp_path / "claude-projects" / "encoded"
    project_dir.mkdir(parents=True)
    transcript = project_dir / "s1.jsonl"
    transcript.write_text(
        json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": "hi"}]}})
        + "\n"
    )
    monkeypatch.setattr(claude_agent.ClaudeAgent, "_project_dir", lambda self, cwd: project_dir)

    now = datetime.now(UTC)
    state.save_task(
        proj,
        task.model_copy(
            update={
                "sessions": [
                    SessionRecord(
                        agent="claude",
                        session_id="s1",
                        created_at=now,
                        last_used_at=now,
                        transcript_path=None,  # never recorded
                    )
                ]
            }
        ),
    )

    res = runner.invoke(app, ["status", "--active"])
    assert res.exit_code == 0, res.output
    assert task.id in res.output, res.output
    # And the heal is persisted, so the next tick classifies from the transcript.
    [persisted] = state.list_tasks(proj)
    assert persisted.sessions[0].transcript_path == transcript


def test_active_still_hides_a_task_whose_transcript_is_genuinely_stale(
    isolated_xdg: Path, tmp_path: Path
) -> None:
    """The heal above must not turn `--active` into "show everything"."""
    runner, proj = _project_with_task(tmp_path)
    [task] = state.list_tasks(proj)

    transcript = tmp_path / "old.jsonl"
    transcript.write_text("{}\n")
    stamp = datetime.now(UTC).timestamp() - 86400
    os.utime(transcript, (stamp, stamp))

    now = datetime.now(UTC)
    state.save_task(
        proj,
        task.model_copy(
            update={
                "sessions": [
                    SessionRecord(
                        agent="claude",
                        session_id="s1",
                        created_at=now,
                        last_used_at=now,
                        summary="quiet",
                        summary_updated_at=now,
                        transcript_path=transcript,
                    )
                ]
            }
        ),
    )

    res = runner.invoke(app, ["status", "--active"])
    assert res.exit_code == 0, res.output
    assert "Nothing in flight" in res.output
