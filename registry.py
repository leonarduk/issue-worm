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

from history import DEFAULT_HISTORY_PATH

logger = logging.getLogger(__name__)

STATE_DIR_ENV = "ISSUE_WORM_STATE_DIR"
DEFAULT_STATE_DIR = Path.home() / ".issue-worm" / "agents"
PACKAGE_NAME = "issue-worm"

# A "running" record whose heartbeat hasn't moved in this long is reported
# as "stale" on read (issue #181) - inferred from `updated_at` only, never
# from probing the PID (see module docstring: on Windows, os.kill(pid, 0)
# calls TerminateProcess, so a liveness probe would actually kill the run).
STALE_AFTER_SECONDS_ENV = "ISSUE_WORM_STALE_AFTER_SECONDS"
DEFAULT_STALE_AFTER_SECONDS = 600  # 10 minutes

# Terminal statuses eligible for opportunistic pruning in register(), and
# how old (by `updated_at`) one has to be before it's removed.
_TERMINAL_STATUSES = ("done", "failed", "stale")
TERMINAL_RETENTION_SECONDS = 24 * 60 * 60  # 24 hours


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


def _stale_after_seconds() -> float:
    """Staleness threshold in seconds: $ISSUE_WORM_STALE_AFTER_SECONDS if
    set to a valid number, else DEFAULT_STALE_AFTER_SECONDS."""
    override = os.environ.get(STALE_AFTER_SECONDS_ENV)
    if override is None:
        return DEFAULT_STALE_AFTER_SECONDS
    try:
        return float(override)
    except ValueError:
        return DEFAULT_STALE_AFTER_SECONDS


def _parse_timestamp(value: Any) -> Optional[datetime]:
    """Parse an ISO-8601 timestamp, or None if missing/unparsable."""
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _with_staleness(record: dict) -> dict:
    """Return ``record``, or a copy with ``status`` overridden to "stale"
    if it's a "running" record whose ``updated_at`` heartbeat is older
    than the staleness threshold.

    Never mutates ``record`` in place and never touches disk - this is
    purely an in-memory annotation applied on read, so a caller reading
    the registry can never race a concurrent writer (issue #181).
    """
    if record.get("status") != "running":
        return record
    updated_at = _parse_timestamp(record.get("updated_at"))
    if updated_at is None:
        return record
    age = (datetime.now(timezone.utc) - updated_at).total_seconds()
    if age < _stale_after_seconds():
        return record
    marked = dict(record)
    marked["status"] = "stale"
    return marked


def list_runs() -> list[dict]:
    """Every parseable run record in the state dir, read-only.

    Best-effort like the rest of this module: a missing state dir, an
    unreadable file, or malformed/non-object JSON is skipped rather than
    raised or repaired. A "running" record whose heartbeat is older than
    the staleness threshold comes back with ``status: "stale"`` in the
    returned dict - the on-disk file is never rewritten by this read
    (issue #181; see also cli.py's `status` command, which relies on this
    staying read-only).
    """
    try:
        paths = list(_state_dir().glob("*.json"))
    except OSError:
        return []
    records = []
    for path in paths:
        record = _read_run(path)
        if isinstance(record, dict):
            records.append(_with_staleness(record))
    return records


def _prune_terminal_records() -> None:
    """Best-effort delete of terminal (done/failed/stale) records whose
    ``updated_at`` heartbeat is older than ``TERMINAL_RETENTION_SECONDS``,
    so the state dir doesn't grow without bound.

    Called opportunistically from `register()` on every new run rather
    than from a background thread or timer (issue #181 explicitly rules
    those out). Any failure - a single unreadable/undeletable file, a
    missing state dir - is swallowed per-file so it never blocks the
    registration this call is piggybacking on.
    """
    try:
        paths = list(_state_dir().glob("*.json"))
    except OSError:
        return
    now = datetime.now(timezone.utc)
    for path in paths:
        try:
            record = _read_run(path)
            if not isinstance(record, dict):
                continue
            if record.get("status") not in _TERMINAL_STATUSES:
                continue
            updated_at = _parse_timestamp(record.get("updated_at"))
            if updated_at is None:
                continue
            if (now - updated_at).total_seconds() < TERMINAL_RETENTION_SECONDS:
                continue
            path.unlink()
        except OSError:
            logger.debug("registry._prune_terminal_records: could not prune %s", path, exc_info=True)
            continue


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
        _prune_terminal_records()
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
                # Derived from history.py's own constant rather than spelled
                # out here: the file lives at <workspace>/.issue-worm/
                # history.jsonl, and a reader that guessed <workspace>/
                # history.jsonl would silently find no history at all.
                "history_path": str(workspace_path / DEFAULT_HISTORY_PATH),
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
