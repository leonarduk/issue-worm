"""Tests for history.py's append-only JSONL run log.

record_run has no dependency on orchestrator.TaskRun's class (see
history.py) — only that it's a dataclass instance — so these tests use a
local stand-in rather than importing the private orchestrator module.
"""

import json
import logging
from dataclasses import dataclass

from history import count_rejections, get_run, load_runs, record_rejection, record_run


@dataclass
class _AgentResult:
    role: str
    output: str


@dataclass
class _TaskRun:
    task_id: str
    source: str
    description: str
    files: list
    status: str
    attempts: list
    final_diff: str


def _task_run(task_id="task-1", status="completed"):
    return _TaskRun(
        task_id=task_id,
        source="cli",
        description="do something",
        files=["a.py"],
        status=status,
        attempts=[_AgentResult(role="coder", output="diff1")],
        final_diff="diff1",
    )


def test_load_runs_returns_empty_list_when_file_missing(tmp_path):
    assert load_runs(str(tmp_path / "missing.jsonl")) == []


def test_record_run_appends_one_json_line(tmp_path):
    history_path = str(tmp_path / "history.jsonl")

    record_run(_task_run("task-1"), history_path=history_path)
    record_run(_task_run("task-2"), history_path=history_path)

    lines = (tmp_path / "history.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    assert json.loads(lines[0])["task_id"] == "task-1"
    assert json.loads(lines[1])["task_id"] == "task-2"


def test_record_run_creates_parent_directories(tmp_path):
    history_path = str(tmp_path / "nested" / "dir" / "history.jsonl")

    record_run(_task_run(), history_path=history_path)

    assert (tmp_path / "nested" / "dir" / "history.jsonl").exists()


def test_load_runs_preserves_order_and_full_record(tmp_path):
    history_path = str(tmp_path / "history.jsonl")
    record_run(_task_run("task-1", status="completed"), history_path=history_path)
    record_run(_task_run("task-2", status="failed"), history_path=history_path)

    runs = load_runs(history_path)

    assert [r["task_id"] for r in runs] == ["task-1", "task-2"]
    assert runs[0]["status"] == "completed"
    assert runs[0]["attempts"][0]["role"] == "coder"
    assert runs[1]["status"] == "failed"


def test_load_runs_skips_malformed_lines(tmp_path):
    history_path = tmp_path / "history.jsonl"
    history_path.write_text(
        json.dumps({"task_id": "good"}) + "\nnot-json\n\n",
        encoding="utf-8",
    )

    runs = load_runs(str(history_path))

    assert runs == [{"task_id": "good"}]


def test_load_runs_warns_on_malformed_lines(tmp_path, caplog):
    history_path = tmp_path / "history.jsonl"
    history_path.write_text(
        json.dumps({"task_id": "good"}) + "\nnot-json\n\n",
        encoding="utf-8",
    )

    with caplog.at_level(logging.WARNING):
        load_runs(str(history_path))

    records = [r for r in caplog.records if r.name == "history"]
    assert len(records) == 1
    assert "line 2" in records[0].message
    assert str(history_path) in records[0].message


def test_get_run_returns_matching_run(tmp_path):
    history_path = str(tmp_path / "history.jsonl")
    record_run(_task_run("task-1"), history_path=history_path)
    record_run(_task_run("task-2"), history_path=history_path)

    run = get_run("task-2", history_path=history_path)

    assert run is not None
    assert run["task_id"] == "task-2"


def test_get_run_returns_none_when_not_found(tmp_path):
    history_path = str(tmp_path / "history.jsonl")
    record_run(_task_run("task-1"), history_path=history_path)

    assert get_run("nope", history_path=history_path) is None


def test_get_run_returns_last_occurrence_when_task_id_repeated(tmp_path):
    history_path = str(tmp_path / "history.jsonl")
    record_run(_task_run("task-1", status="failed"), history_path=history_path)
    record_run(_task_run("task-1", status="completed"), history_path=history_path)

    run = get_run("task-1", history_path=history_path)

    assert run["status"] == "completed"


def test_record_run_concurrent_appends_are_atomic(tmp_path):
    """N parallel workers appending must yield a valid JSONL file with
    exactly N intact records - the parallel-dispatch guarantee (#159)."""
    import threading

    history_path = str(tmp_path / "history.jsonl")
    errors = []

    def _record(i):
        try:
            record_run(_task_run(f"task-{i}"), history_path=history_path)
        except Exception as exc:  # pragma: no cover - failure surfaces below
            errors.append(exc)

    threads = [threading.Thread(target=_record, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == []
    lines = (tmp_path / "history.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 8
    assert {json.loads(line)["task_id"] for line in lines} == {
        f"task-{i}" for i in range(8)
    }


# --- Review-rejection events (issue #306) -----------------------------------


def test_record_rejection_and_count_roundtrip(tmp_path):
    """Each rejection appends one event; count_rejections is per-issue."""
    history_path = str(tmp_path / "history.jsonl")

    record_rejection(7, history_path=history_path)
    record_rejection(7, history_path=history_path)
    record_rejection(8, history_path=history_path)

    assert count_rejections(7, history_path=history_path) == 2
    assert count_rejections(8, history_path=history_path) == 1
    assert count_rejections(9, history_path=history_path) == 0


def test_record_rejection_creates_parent_directories(tmp_path):
    """Same file/creation contract as record_run."""
    history_path = str(tmp_path / "nested" / "dir" / "history.jsonl")
    record_rejection(7, history_path=history_path)
    assert (tmp_path / "nested" / "dir" / "history.jsonl").exists()


def test_count_rejections_ignores_task_runs(tmp_path):
    """A completed run for the same issue is not a rejection — the bound
    counts only review-rejection events (#306)."""
    history_path = str(tmp_path / "history.jsonl")
    record_run(_task_run("issue-7"), history_path=history_path)

    assert count_rejections(7, history_path=history_path) == 0


def test_load_runs_filters_rejection_events(tmp_path):
    """load_runs keeps returning exactly what record_run wrote — the
    history CLI never shows scheduler-internal rejection events."""
    history_path = str(tmp_path / "history.jsonl")
    record_run(_task_run("task-1"), history_path=history_path)
    record_rejection(7, history_path=history_path)
    record_run(_task_run("task-2"), history_path=history_path)

    runs = load_runs(history_path)
    assert [r["task_id"] for r in runs] == ["task-1", "task-2"]
    assert all("type" not in r for r in runs)


def test_get_run_ignores_rejection_events(tmp_path):
    """get_run is keyed on task_id; rejection events have none, so the
    most recent matching run still wins."""
    history_path = str(tmp_path / "history.jsonl")
    record_rejection(7, history_path=history_path)
    record_run(_task_run("issue-7"), history_path=history_path)

    run = get_run("issue-7", history_path=history_path)
    assert run is not None
    assert run["status"] == "completed"
