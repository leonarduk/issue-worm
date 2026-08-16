"""Append-only JSONL log of TaskRun history.

One writer (the CLI today, the Scheduler in M2) and simple list/filter
reads - no schema or migration overhead. See docs/design.md for the
SQLite-vs-JSONL rationale; SQLite is deferred to M3's dashboard.
"""

import json
import threading
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

DEFAULT_HISTORY_PATH = ".issue-worm/history.jsonl"

# Records that are not TaskRuns (e.g. review-rejection events, #306) carry a
# ``type`` key — TaskRun's asdict() never emits one — so ``load_runs`` can
# filter them out without a schema version.
_REJECTION_EVENT_TYPE = "review-rejection"

# Parallel Scheduler workers (#159) record runs concurrently; serializing the
# append keeps one record a single, never-interleaved line in the JSONL file.
_record_lock = threading.Lock()


def _append_record(record: dict, history_path: str) -> None:
    """Append one JSON record under the shared write lock."""
    path = Path(history_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(record) + "\n"
    with _record_lock:
        with path.open("a", encoding="utf-8") as f:
            f.write(line)


def record_run(run: Any, history_path: str = DEFAULT_HISTORY_PATH) -> None:
    """Append a completed run as one JSON line to the history file.

    Thread-safe: concurrent callers (parallel issue dispatch) append
    atomically, so each line is one intact record. This module has no
    dependency on the run's class (e.g. orchestrator.TaskRun) — only
    that it's a dataclass instance ``dataclasses.asdict`` can serialize.

    Args:
        run: The completed run to record, as a dataclass instance.
        history_path: Path to the JSONL file. Parent directories are
            created if missing.
    """
    _append_record(asdict(run), history_path)


def record_rejection(
    issue_number: int, history_path: str = DEFAULT_HISTORY_PATH
) -> None:
    """Record one AI-review rejection of an issue's PR (#306).

    The review-feedback loop's rejection bound is counted from these
    events, not from TaskRuns: a completed run whose PR was later rejected
    is indistinguishable from one whose PR merged, so the requeue sweep
    appends an event each time it closes a PR with a ``Changes Requested``
    verdict.

    Args:
        issue_number: The issue whose PR was rejected.
        history_path: Path to the JSONL file, shared with ``record_run``.
    """
    _append_record(
        {
            "type": _REJECTION_EVENT_TYPE,
            "issue_number": issue_number,
            "recorded_at": datetime.now(timezone.utc).isoformat(),
        },
        history_path,
    )


def count_rejections(
    issue_number: int, history_path: str = DEFAULT_HISTORY_PATH
) -> int:
    """How many AI-review rejections ``issue_number`` has accumulated.

    Args:
        issue_number: The issue to count.
        history_path: Path to the JSONL file.

    Returns:
        The number of recorded review-rejection events for the issue.
    """
    return sum(
        1
        for r in _load_records(history_path)
        if r.get("type") == _REJECTION_EVENT_TYPE
        and r.get("issue_number") == issue_number
    )


def _load_records(history_path: str) -> list[dict]:
    """Every JSON record in the file, oldest first; malformed lines skipped."""
    path = Path(history_path)
    if not path.exists():
        return []
    records = []
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return records


def load_runs(history_path: str = DEFAULT_HISTORY_PATH) -> list[dict]:
    """Load all recorded runs from the history file, oldest first.

    Non-run records (e.g. review-rejection events, #306) are filtered
    out, so ``load_runs`` keeps returning exactly what ``record_run``
    wrote — the ``history`` CLI never shows scheduler internals.

    Args:
        history_path: Path to the JSONL file.

    Returns:
        List of run dicts, empty if the file doesn't exist. Lines that
        fail to parse are skipped rather than aborting the whole read.
    """
    return [r for r in _load_records(history_path) if "type" not in r]


def get_run(task_id: str, history_path: str = DEFAULT_HISTORY_PATH) -> Optional[dict]:
    """Find the most recently recorded run with the given task_id.

    Args:
        task_id: The task_id to look up.
        history_path: Path to the JSONL file.

    Returns:
        The run dict, or None if no run with that task_id was found. If
        task_id was recorded more than once, returns the last occurrence.
    """
    matches = [r for r in load_runs(history_path) if r.get("task_id") == task_id]
    return matches[-1] if matches else None
