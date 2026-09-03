"""Best-effort registry of in-flight runs, one JSON file per run.

``history.py`` only records a run *after* it completes (``record_run``
appends to ``.issue-worm/history.jsonl``), so there is no way to observe a
run in progress. This module fills that gap for the optional monitoring UI
(tracked in issue-worm-pro's "Monitoring UI" milestone): the worm drops a
heartbeat file into a shared state directory and never talks to the UI
directly, so if no UI is installed nothing reads the directory and
behaviour is unchanged.

Every public function is best-effort: a read-only state dir, a full disk,
or a bogus ``ISSUE_WORM_STATE_DIR`` must never raise into the caller, since
that would fail a build over a purely observational feature. Each function
wraps its body in a broad ``except Exception`` and logs at debug/warning
level instead.
"""

import json
import logging
import os
from datetime import datetime, timezone
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

STATE_DIR_ENV = "ISSUE_WORM_STATE_DIR"
DEFAULT_STATE_DIR = Path.home() / ".issue-worm" / "agents"
PACKAGE_NAME = "issue-worm"


def _state_dir() -> Path:
    """Resolve the run-registry state directory (not created here)."""
    override = os.environ.get(STATE_DIR_ENV)
    return Path(override) if override else DEFAULT_STATE_DIR


def _package_version() -> str:
    """Installed issue-worm version, or "unknown" if it can't be read."""
    try:
        return version(PACKAGE_NAME)
    except PackageNotFoundError:
        return "unknown"


def _run_path(task_id: str) -> Path:
    return _state_dir() / f"{task_id}.json"


def _read_run(path: Path) -> Optional[dict]:
    """Load a run file's JSON, or None if missing/unreadable/malformed."""
    try:
        with path.open("r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def _write_run_atomic(path: Path, record: dict) -> None:
    """Write ``record`` to ``path`` via a temp file + ``os.replace()``.

    ``os.replace`` is atomic on both POSIX and Windows, so a concurrent
    reader either sees the old file or the fully-written new one - never a
    half-written one.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    with tmp_path.open("w", encoding="utf-8") as f:
        json.dump(record, f)
    os.replace(tmp_path, path)


def register(
    task_id: str, command: str, workspace: str, extra: Optional[dict] = None
) -> Optional[Path]:
    """Record a run as newly started, writing ``<task_id>.json``.

    Args:
        task_id: Unique identifier for the run.
        command: The CLI command being run (e.g. "build").
        workspace: Absolute path to the run's workspace.
        extra: Optional extra fields merged into the record. Caller-supplied
            keys never overwrite the fields this function sets.

    Returns:
        The path written, or None if registration failed (or errored) - in
        which case the caller has nothing to clean up and should proceed as
        if monitoring were disabled.
    """
    try:
        workspace_path = Path(workspace).resolve()
        now = datetime.now(timezone.utc).isoformat()
        record: dict[str, Any] = dict(extra or {})
        record.update(
            {
                "task_id": task_id,
                "pid": os.getpid(),
                "status": "running",
                "command": command,
                "workspace": str(workspace_path),
                "history_path": str(workspace_path / "history.jsonl"),
                "phase": None,
                "started_at": now,
                "updated_at": now,
                "package": PACKAGE_NAME,
                "version": _package_version(),
            }
        )
        path = _run_path(task_id)
        _write_run_atomic(path, record)
        return path
    except Exception:
        logger.debug("registry.register failed for task_id=%s", task_id, exc_info=True)
        return None


def heartbeat(task_id: str, phase: Optional[str] = None) -> None:
    """Bump a registered run's ``updated_at`` (and ``phase`` if given).

    ``started_at`` and every other field are left untouched. A missing or
    unreadable run file is a silent no-op - there is nothing to update.

    Args:
        task_id: The run to update.
        phase: Free-text description of the current step, if provided.
    """
    try:
        path = _run_path(task_id)
        record = _read_run(path)
        if record is None:
            logger.debug("registry.heartbeat: no run file for task_id=%s", task_id)
            return
        record["updated_at"] = datetime.now(timezone.utc).isoformat()
        if phase is not None:
            record["phase"] = phase
        _write_run_atomic(path, record)
    except Exception:
        logger.debug("registry.heartbeat failed for task_id=%s", task_id, exc_info=True)


def finish(task_id: str, status: str) -> None:
    """Mark a registered run with its terminal status.

    The file is left in place (not deleted) so a reader can show the last
    outcome; pruning old run files is a separate concern.

    Args:
        task_id: The run to finish.
        status: Terminal status, e.g. "done" or "failed".
    """
    try:
        path = _run_path(task_id)
        record = _read_run(path)
        if record is None:
            logger.debug("registry.finish: no run file for task_id=%s", task_id)
            return
        record["status"] = status
        record["updated_at"] = datetime.now(timezone.utc).isoformat()
        _write_run_atomic(path, record)
    except Exception:
        logger.debug("registry.finish failed for task_id=%s", task_id, exc_info=True)
