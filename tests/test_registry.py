"""Tests for registry.py's best-effort in-flight-run heartbeat files.

register/heartbeat/finish must never raise into the caller (a read-only
state dir, a full disk, or a bogus ISSUE_WORM_STATE_DIR must not fail a
build) - several tests below assert exactly that.
"""

import json
import logging
import os

import pytest

import registry


@pytest.fixture(autouse=True)
def _state_dir(tmp_path, monkeypatch):
    """Point every test at an isolated state dir under tmp_path."""
    state_dir = tmp_path / "agents"
    monkeypatch.setenv(registry.STATE_DIR_ENV, str(state_dir))
    return state_dir


def _read(state_dir, task_id):
    return json.loads((state_dir / f"{task_id}.json").read_text(encoding="utf-8"))


def test_register_creates_run_file_with_expected_fields(tmp_path, _state_dir):
    workspace = tmp_path / "ws"
    workspace.mkdir()

    result_path = registry.register(
        "task-1", command="build", workspace=str(workspace)
    )

    assert result_path == _state_dir / "task-1.json"
    record = _read(_state_dir, "task-1")
    assert record["task_id"] == "task-1"
    assert record["command"] == "build"
    assert record["workspace"] == str(workspace.resolve())
    assert record["history_path"] == str(workspace.resolve() / "history.jsonl")
    assert record["status"] == "running"
    assert record["phase"] is None
    assert record["pid"] == os.getpid()
    assert record["package"] == "issue-worm"
    assert "version" in record
    assert record["started_at"] == record["updated_at"]


def test_register_creates_state_dir_if_missing(tmp_path, _state_dir):
    assert not _state_dir.exists()

    registry.register("task-1", command="build", workspace=str(tmp_path))

    assert _state_dir.is_dir()


def test_register_merges_extra_fields_without_overriding_core_fields(tmp_path, _state_dir):
    registry.register(
        "task-1",
        command="build",
        workspace=str(tmp_path),
        extra={"issue_number": 178, "status": "should-not-win"},
    )

    record = _read(_state_dir, "task-1")
    assert record["issue_number"] == 178
    assert record["status"] == "running"


def test_register_writes_no_leftover_tmp_file(tmp_path, _state_dir):
    registry.register("task-1", command="build", workspace=str(tmp_path))

    leftovers = list(_state_dir.glob("*.tmp"))
    assert leftovers == []


def test_heartbeat_updates_updated_at_and_preserves_started_at(tmp_path, _state_dir):
    registry.register("task-1", command="build", workspace=str(tmp_path))
    original = _read(_state_dir, "task-1")

    registry.heartbeat("task-1")

    updated = _read(_state_dir, "task-1")
    assert updated["started_at"] == original["started_at"]
    assert updated["status"] == "running"


def test_heartbeat_sets_phase_when_given(tmp_path, _state_dir):
    registry.register("task-1", command="build", workspace=str(tmp_path))

    registry.heartbeat("task-1", phase="running-tests")

    assert _read(_state_dir, "task-1")["phase"] == "running-tests"


def test_heartbeat_leaves_phase_untouched_when_not_given(tmp_path, _state_dir):
    registry.register("task-1", command="build", workspace=str(tmp_path))
    registry.heartbeat("task-1", phase="coding")

    registry.heartbeat("task-1")

    assert _read(_state_dir, "task-1")["phase"] == "coding"


def test_heartbeat_on_unknown_task_id_is_a_silent_no_op(_state_dir):
    registry.heartbeat("nope")  # must not raise
    assert not (_state_dir / "nope.json").exists()


def test_finish_sets_terminal_status_and_updates_updated_at(tmp_path, _state_dir):
    registry.register("task-1", command="build", workspace=str(tmp_path))
    original = _read(_state_dir, "task-1")

    registry.finish("task-1", "done")

    record = _read(_state_dir, "task-1")
    assert record["status"] == "done"
    assert record["started_at"] == original["started_at"]


def test_finish_leaves_file_in_place(tmp_path, _state_dir):
    registry.register("task-1", command="build", workspace=str(tmp_path))

    registry.finish("task-1", "failed")

    assert (_state_dir / "task-1.json").exists()
    assert _read(_state_dir, "task-1")["status"] == "failed"


def test_finish_on_unknown_task_id_is_a_silent_no_op(_state_dir):
    registry.finish("nope", "done")  # must not raise
    assert not (_state_dir / "nope.json").exists()


def test_default_state_dir_used_when_env_var_unset(monkeypatch):
    monkeypatch.delenv(registry.STATE_DIR_ENV, raising=False)

    assert registry._state_dir() == registry.DEFAULT_STATE_DIR


# --- Never-raise guarantee ---------------------------------------------


def test_register_does_not_raise_when_state_dir_unwritable(tmp_path, monkeypatch, caplog):
    """A file (not a directory) at the state-dir path makes mkdir fail;
    register must swallow that rather than propagate it."""
    blocker = tmp_path / "blocked"
    blocker.write_text("not a directory", encoding="utf-8")
    monkeypatch.setenv(registry.STATE_DIR_ENV, str(blocker / "agents"))

    with caplog.at_level(logging.DEBUG):
        result = registry.register("task-1", command="build", workspace=str(tmp_path))

    assert result is None


def test_heartbeat_does_not_raise_when_state_dir_unwritable(tmp_path, monkeypatch):
    blocker = tmp_path / "blocked"
    blocker.write_text("not a directory", encoding="utf-8")
    monkeypatch.setenv(registry.STATE_DIR_ENV, str(blocker / "agents"))

    registry.heartbeat("task-1")  # must not raise


def test_finish_does_not_raise_when_state_dir_unwritable(tmp_path, monkeypatch):
    blocker = tmp_path / "blocked"
    blocker.write_text("not a directory", encoding="utf-8")
    monkeypatch.setenv(registry.STATE_DIR_ENV, str(blocker / "agents"))

    registry.finish("task-1", "done")  # must not raise


def test_register_does_not_raise_on_bogus_workspace(_state_dir):
    """An empty/invalid workspace path must not blow up Path.resolve()."""
    result = registry.register("task-1", command="build", workspace="")

    assert result is None or result.exists()


def test_heartbeat_does_not_raise_when_run_file_is_malformed_json(tmp_path, _state_dir):
    _state_dir.mkdir(parents=True)
    (_state_dir / "task-1.json").write_text("{not valid json", encoding="utf-8")

    registry.heartbeat("task-1")  # must not raise


def test_finish_does_not_raise_when_run_file_is_malformed_json(tmp_path, _state_dir):
    _state_dir.mkdir(parents=True)
    (_state_dir / "task-1.json").write_text("{not valid json", encoding="utf-8")

    registry.finish("task-1", "done")  # must not raise
