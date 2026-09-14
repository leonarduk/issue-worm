"""Posts one live progress comment per issue-worm run, edited in place (#345).

Mirrors issue-worm-pro's ``progress_reporter.py`` (the format - marker,
header, checklist, terminal `**Result:**` line - is identical, so a reader
can't tell from the comment alone which engine ran), but is a deliberate
"minimal port" rather than a shared import: pro's version listens for
stage-tagged log records across its scheduler/orchestrator (a router that
makes sense when many call sites emit stage events into one shared logging
stream); this free engine's build is one linear sequence with exactly three
external call sites - the Coder (Python, inside `cli.py`), the verifier and
the publish step (both bash, inside `action.yml`) - so this module exposes
plain functions those call sites invoke directly, with no log routing.

State has to survive across process boundaries (the Coder stage runs inside
`issue-worm build`; the verifier/publish stages run as separate `issue-worm
progress` invocations from action.yml's shell steps), so each call persists
a small per-issue JSON record to disk instead of holding an in-memory
object - the same reason registry.py's run records are files, not a
module-level dict.

Every public function is best-effort and never raises: a comment API
failure, a state directory permission problem, or GitHub being unreachable
logs a warning and lets the run continue. This is reporting, not control
flow.
"""

import json
import logging
import os
import subprocess
import sys
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

# Resolved lazily by _load_post_issue_comment - None until first use, or
# already set here by a test's `patch.object(progress_reporter,
# "post_issue_comment", ...)`, which _load_post_issue_comment then leaves
# untouched (see its own "already resolved" check).
post_issue_comment = None


def _load_post_issue_comment():
    """Return cicaid-devtools' `post_issue_comment`, importing it (and
    caching the result on the module-level `post_issue_comment` name
    above) on first use only - not at module import time.

    `cicaid_devtools.lib.github_issues` does bare top-level imports of its
    own sibling modules rather than package-relative ones, so it isn't
    importable as a plain subpackage - its lib/ dir has to be on sys.path
    first. Same hack issue-worm-pro's cicaid_bridge.py uses for the same
    reason (see that module's docstring); duplicated here rather than
    shared since pro isn't a dependency of this package.

    Deferred rather than done at module scope: `cli.py` imports this
    module unconditionally (for `build`'s own progress calls), and every
    other subcommand - `history`, `status`, `poll` - has nothing to do
    with progress reporting or cicaid-devtools. A git dependency install
    hiccup (network, a missing `git` on PATH - real risks for a `pip
    install git+https://...` dependency) would otherwise take down every
    subcommand, not just the one that actually needs it.
    """
    global post_issue_comment
    if post_issue_comment is not None:
        return post_issue_comment
    try:
        import cicaid_devtools
    except ImportError as exc:
        raise ImportError(
            "cicaid-devtools is not installed. Install the project's "
            "dependencies (see pyproject.toml) before running issue-worm."
        ) from exc
    for lib_dir in {Path(p) / "lib" for p in cicaid_devtools.__path__}:
        if lib_dir.is_dir() and str(lib_dir) not in sys.path:
            sys.path.insert(0, str(lib_dir))
    from github_issues import post_issue_comment as _post_issue_comment

    post_issue_comment = _post_issue_comment
    return post_issue_comment

# Same marker text issue-worm-pro's progress_reporter.py uses, so a reader
# (or a script) can't tell which engine produced a given comment without
# reading its body.
PROGRESS_MARKER = "<!-- issue-worm:progress -->"

STATE_DIR_ENV = "ISSUE_WORM_PROGRESS_STATE_DIR"
DEFAULT_STATE_DIR = Path.home() / ".issue-worm" / "progress"

_GH_TIMEOUT_SECONDS = 30


def _state_dir() -> Path:
    override = os.environ.get(STATE_DIR_ENV)
    return Path(override) if override else DEFAULT_STATE_DIR


def _state_path(repo: str, issue_number: int) -> Path:
    # Keyed on repo *and* issue number, not just the issue number: two
    # different repos can each have their own issue #5, and without the
    # repo in the path a run against one would read/overwrite the other's
    # in-flight state (DeepSeek review of #359) - real for any machine
    # that dispatches issue-worm against more than one repo, e.g. this
    # action reused across several consumer repos on the same
    # self-hosted runner. "/" isn't a valid path separator to keep intact
    # here, so it's replaced the same way registry.py's task_id does
    # (`owner_name`), rather than nesting a real subdirectory per repo.
    return _state_dir() / f"{repo.replace('/', '_')}-{issue_number}.json"


# Pro's scheduler claims an issue with this same label (its
# IN_PROGRESS_LABEL), so an issue a free run is working on looks claimed
# to both engines (#383).
IN_PROGRESS_LABEL = "in-progress"


def _run_url() -> str | None:
    """URL of the GitHub Actions run this process belongs to, or None
    outside Actions (a local `issue-worm build`). A re-run gets its own
    `/attempts/N` URL rather than sharing attempt 1's, so each attempt
    adopts its own announce comment and never an earlier attempt's (#383).
    action.yml's "Announce the run on the issue" step builds the same URL
    in bash - keep the two in step."""
    run_id = os.environ.get("GITHUB_RUN_ID")
    repository = os.environ.get("GITHUB_REPOSITORY")
    if not run_id or not repository:
        return None
    server = (os.environ.get("GITHUB_SERVER_URL") or "https://github.com").rstrip("/")
    url = f"{server}/{repository}/actions/runs/{run_id}"
    attempt = os.environ.get("GITHUB_RUN_ATTEMPT")
    if attempt and attempt != "1":
        url += f"/attempts/{attempt}"
    return url


def _run_link(run_url: str) -> str:
    # Matched verbatim (brackets and parens included) when adopting the
    # announce comment, so run 12's link is never a substring match for
    # run 123's, nor attempt 1's for attempt 2's.
    return f"[Actions run]({run_url})"


def _set_in_progress_label(repo: str, issue_number: int, present: bool) -> None:
    """Add or remove IN_PROGRESS_LABEL. Best-effort, never raises. Adding
    a label the repo doesn't have yet creates it; removing one the issue
    doesn't carry is a 404, which is the normal case after action.yml's
    own cleanup step already removed it, so only a failed add warns."""
    endpoint = f"repos/{repo}/issues/{issue_number}/labels"
    if present:
        cmd = ["gh", "api", "-X", "POST", endpoint, "-f", f"labels[]={IN_PROGRESS_LABEL}"]
    else:
        cmd = ["gh", "api", "-X", "DELETE", f"{endpoint}/{IN_PROGRESS_LABEL}"]
    try:
        result = subprocess.run(
            cmd,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=_GH_TIMEOUT_SECONDS,
        )
    except Exception:
        logger.warning(
            "issue #%s: could not update the %s label",
            issue_number,
            IN_PROGRESS_LABEL,
            exc_info=True,
        )
        return
    if result.returncode != 0 and present:
        logger.warning(
            "issue #%s: could not add the %s label: %s",
            issue_number,
            IN_PROGRESS_LABEL,
            result.stderr.strip(),
        )


@dataclass
class _ProgressState:
    repo: str
    issue_number: int
    comment_id: int | None = None
    # The Actions run that owns this record (None for a local run). See
    # `_load_state` for why a record from any other run is discarded.
    run_url: str | None = field(default_factory=_run_url)
    # Each entry: {"label": str, "elapsed": float | None}. `elapsed is None`
    # means the stage has started but not finished yet.
    stages: list = field(default_factory=list)
    finished: bool = False
    succeeded: bool = False
    terminal: str | None = None


def _load_state(repo: str, issue_number: int) -> "_ProgressState | None":
    try:
        path = _state_path(repo, issue_number)
        if not path.exists():
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
        # A record left by a different run (a persistent self-hosted
        # runner's previous dispatch of this issue) is not this run's: its
        # `finished` flag would turn this run's own finish() into a no-op,
        # and its comment_id points at the old run's comment. Compared on
        # the raw dict, not the dataclass - a pre-#383 record has no
        # run_url key, and the field's default_factory would silently
        # stamp it with the current run's URL.
        if data.get("run_url") != _run_url():
            return None
        return _ProgressState(**data)
    except Exception:
        logger.debug(
            "%s#%s: progress state unreadable, starting fresh",
            repo,
            issue_number,
            exc_info=True,
        )
        return None


def _save_state(state: _ProgressState) -> None:
    try:
        path = _state_path(state.repo, state.issue_number)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(asdict(state)), encoding="utf-8")
    except Exception:
        logger.debug(
            "%s#%s: could not persist progress state",
            state.repo,
            state.issue_number,
            exc_info=True,
        )


def _decode_paginated_json(text: str) -> list:
    """Decode the concatenated JSON arrays `gh api --paginate` emits - one
    page is one JSON array, and pages are written back-to-back with no
    separator, so a plain `json.loads` fails on anything past the first
    page. Mirrors issue-worm-pro's `cicaid_bridge._decode_paginated_json`.
    Raises `json.JSONDecodeError` on malformed input, same as `json.loads`
    - callers here already wrap every use in a broad `except Exception`.
    """
    items: list = []
    decoder = json.JSONDecoder()
    pos = 0
    while pos < len(text):
        while pos < len(text) and text[pos].isspace():
            pos += 1
        if pos >= len(text):
            break
        page, end = decoder.raw_decode(text, pos)
        items.extend(page)
        pos = end
    return items


def _find_progress_comment_id(
    repo: str, issue_number: int, must_contain: str | None = None
) -> int | None:
    """Id of the most recent comment on the issue carrying PROGRESS_MARKER
    (and `must_contain` too, when given - used to find this run's own
    comment among earlier runs' on the same issue).

    cicaid-devtools' ``github_issues`` module has ``post_issue_comment`` and
    ``get_issue_comments`` (the latter returns author/body/created_at, no
    id) but no way to *find* a specific comment's id for editing later, so
    this goes straight to `gh api`, the same way issue-worm-pro's
    `cicaid_bridge.find_issue_comment_id` does. `--paginate`, not a single
    page: the comments endpoint returns oldest-first, so on an issue with
    a long history the progress comment (posted well after the issue was
    opened) can easily be past the default 30-per-page cutoff - the most
    recent page, not the first, is where it usually lives.
    """
    try:
        result = subprocess.run(
            ["gh", "api", f"repos/{repo}/issues/{issue_number}/comments", "--paginate"],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=_GH_TIMEOUT_SECONDS,
        )
        if result.returncode != 0:
            logger.warning(
                "issue #%s: could not list comments to locate the progress "
                "comment: %s",
                issue_number,
                result.stderr.strip(),
            )
            return None
        items = _decode_paginated_json(result.stdout)
        matches = [
            item
            for item in items
            if isinstance(item, dict)
            and PROGRESS_MARKER in (item.get("body") or "")
            and (must_contain is None or must_contain in (item.get("body") or ""))
        ]
        if not matches:
            return None
        matches.sort(key=lambda item: item.get("created_at", ""))
        return matches[-1].get("id")
    except Exception:
        logger.debug(
            "issue #%s: progress comment lookup failed", issue_number, exc_info=True
        )
        return None


def _update_progress_comment(
    repo: str, comment_id: int, body: str, dry_run: bool = False
) -> bool:
    """Replace an existing comment's body. See `find_progress_comment_id`
    docstring: `gh` has no edit-by-arbitrary-comment-id porcelain command,
    so this uses `gh api -X PATCH` directly, body passed via `-F body=@file`
    (not argv) since a rendered checklist can be long and contains
    newlines."""
    if dry_run:
        logger.info(
            "[DRY RUN] Would update progress comment %s on %s:\n%s",
            comment_id,
            repo,
            body,
        )
        return True
    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(
            "w", suffix=".md", encoding="utf-8", delete=False
        ) as tmp:
            tmp.write(body)
            tmp_path = tmp.name
        result = subprocess.run(
            [
                "gh",
                "api",
                f"repos/{repo}/issues/comments/{comment_id}",
                "-X",
                "PATCH",
                "-F",
                f"body=@{tmp_path}",
            ],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=_GH_TIMEOUT_SECONDS,
        )
    finally:
        if tmp_path is not None:
            try:
                Path(tmp_path).unlink(missing_ok=True)
            except OSError:
                logger.debug("could not remove temp file %s", tmp_path)
    if result.returncode != 0:
        logger.warning(
            "%s: could not update progress comment %s: %s",
            repo,
            comment_id,
            result.stderr.strip(),
        )
        return False
    return True


def _status_word(state: _ProgressState) -> str:
    if not state.finished:
        return "working"
    return "done" if state.succeeded else "failed"


def _render(state: _ProgressState) -> str:
    lines = [PROGRESS_MARKER, f"\U0001fab1 issue-worm \u00b7 {_status_word(state)}"]
    if state.run_url:
        lines.append(_run_link(state.run_url))
    for stage in state.stages:
        label = stage["label"]
        elapsed = stage["elapsed"]
        if elapsed is not None:
            lines.append(f"- [x] {label} ({elapsed:.1f}s)")
        elif not state.finished:
            lines.append(f"- [ ] {label}  \u2190 current")
        else:
            lines.append(f"- [ ] {label}")
    if state.terminal is not None:
        lines.append("")
        lines.append(state.terminal)
    return "\n".join(lines)


def _sync(state: _ProgressState, dry_run: bool) -> None:
    body = _render(state)
    run_link = _run_link(state.run_url) if state.run_url else None
    if state.comment_id is None and run_link and not dry_run:
        # action.yml posts this run's comment itself, before checkout or
        # install (#383) - edit that one rather than posting a second.
        state.comment_id = _find_progress_comment_id(
            state.repo, state.issue_number, must_contain=run_link
        )
    if state.comment_id is None:
        post_issue_comment = _load_post_issue_comment()
        if not post_issue_comment(state.repo, state.issue_number, body, dry_run=dry_run):
            logger.warning(
                "issue #%s: could not post progress comment", state.issue_number
            )
            return
        if dry_run:
            return
        state.comment_id = _find_progress_comment_id(
            state.repo, state.issue_number, must_contain=run_link
        )
        if state.comment_id is None:
            logger.warning(
                "issue #%s: posted progress comment but could not locate its "
                "id; later updates will be skipped",
                state.issue_number,
            )
        return
    if not _update_progress_comment(state.repo, state.comment_id, body, dry_run=dry_run):
        logger.warning(
            "issue #%s: could not update progress comment", state.issue_number
        )


def start(repo: str, issue_number: int, dry_run: bool = False) -> None:
    """Post the initial 'working' progress comment. Best-effort, never
    raises. Overwrites any prior local state for this issue number - each
    dispatch (a fresh label, a re-run) starts its own checklist from
    scratch, matching pro's "one instance per dispatch" lifecycle. Also
    adds IN_PROGRESS_LABEL, which finish() removes again (#383)."""
    try:
        if not dry_run:
            _set_in_progress_label(repo, issue_number, True)
        state = _ProgressState(repo=repo, issue_number=issue_number)
        _sync(state, dry_run)
        _save_state(state)
    except Exception:
        logger.warning(
            "issue #%s: could not start progress comment", issue_number, exc_info=True
        )


def record_stage_start(repo: str, issue_number: int, label: str, dry_run: bool = False) -> None:
    """Add an open checklist row for `label` and sync. Best-effort.

    If no `start()` was recorded for this issue (state file missing - an
    out-of-order call, or the initial post itself failed), a fresh state is
    created here so a stage is never silently dropped; the 'working' header
    just appears a little late, already carrying this stage.
    """
    try:
        state = _load_state(repo, issue_number) or _ProgressState(
            repo=repo, issue_number=issue_number
        )
        if state.finished:
            return
        last = state.stages[-1] if state.stages else None
        if last is None or last["elapsed"] is not None or last["label"] != label:
            state.stages.append({"label": label, "elapsed": None})
        _sync(state, dry_run)
        _save_state(state)
    except Exception:
        logger.warning(
            "issue #%s: could not record start of stage %r",
            issue_number,
            label,
            exc_info=True,
        )


def record_stage_done(
    repo: str, issue_number: int, label: str, elapsed: float, dry_run: bool = False
) -> None:
    """Close `label`'s checklist row with `elapsed` seconds and sync.

    If `label` was never opened via `record_stage_start` (or state is
    missing), appends an already-closed row instead of raising - the
    checklist should still show the stage happened, even out of order.
    """
    try:
        state = _load_state(repo, issue_number) or _ProgressState(
            repo=repo, issue_number=issue_number
        )
        if state.finished:
            return
        for stage in reversed(state.stages):
            if stage["label"] == label and stage["elapsed"] is None:
                stage["elapsed"] = elapsed
                break
        else:
            state.stages.append({"label": label, "elapsed": elapsed})
        _sync(state, dry_run)
        _save_state(state)
    except Exception:
        logger.warning(
            "issue #%s: could not record completion of stage %r",
            issue_number,
            label,
            exc_info=True,
        )


def finish(
    repo: str,
    issue_number: int,
    pr_url: str | None = None,
    failure_detail: str | None = None,
    dry_run: bool = False,
) -> None:
    """Write the terminal `**Result:**` line and sync. Best-effort and
    idempotent - a second call (defensive code, an unexpected retry) is a
    no-op rather than overwriting an already-final record."""
    try:
        state = _load_state(repo, issue_number) or _ProgressState(
            repo=repo, issue_number=issue_number
        )
        if state.finished:
            return
        state.finished = True
        if pr_url:
            state.succeeded = True
            state.terminal = f"**Result:** \u2705 {pr_url}"
        elif failure_detail:
            state.terminal = f"**Result:** \u274c {failure_detail}"
        else:
            state.terminal = "**Result:** finished"
        _sync(state, dry_run)
        _save_state(state)
        if not dry_run:
            _set_in_progress_label(repo, issue_number, False)
    except Exception:
        logger.warning(
            "issue #%s: could not finish progress comment",
            issue_number,
            exc_info=True,
        )


__all__ = [
    "PROGRESS_MARKER",
    "start",
    "record_stage_start",
    "record_stage_done",
    "finish",
]
