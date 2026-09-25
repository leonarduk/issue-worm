"""Workspace management: reset-and-retry between revision attempts, Coder
output application (diff or full-file write), CI-check invocation, and
rollback on failure or interruption.

Branch creation is out of scope here (see docs/design.md's "Relationship
to cicaid") - `cicaid work-on-issue <id> --type fix` has already checked
out the branch this module operates on before orchestrator.py calls in.
No LLM calls happen in this module.

Timeouts (#45, #209)
--------------------

Every subprocess this module starts is bounded, so a hung git or CI
command surfaces as an error instead of blocking the orchestrator
forever. Each default can be overridden per call via a ``timeout``
argument; the four bounds differ because the operations do:

===========================  =======  =========================================
Constant                     Default  Applies to
===========================  =======  =========================================
:data:`DEFAULT_GIT_TIMEOUT`     30s   :func:`_run_git` - local git operations,
                                      which are fast and purely on-disk.
:data:`FETCH_TIMEOUT`          120s   ``git fetch origin`` in
                                      :func:`refresh_to_main` - talks to the
                                      network, so a big repo over a slow link
                                      legitimately outlasts the git default.
:data:`CLONE_TIMEOUT`          600s   the ``git clone`` in
                                      :func:`ensure_base_clone` - a full clone
                                      of a large repo takes minutes.
:data:`DEFAULT_CI_TIMEOUT`     600s   :func:`run_ci_checks` - a real test suite
                                      takes minutes.
===========================  =======  =========================================

Note the two different failure shapes. A CI command that *fails* is
reported as ``(False, output)`` so the Analyser can read it like any test
failure; a CI command that *times out* raises :class:`WorkspaceError`, so
a stall is never mistaken for a red test run. Git timeouts always raise.

Environment variables (#178)
----------------------------

``WORM_SKIP_REMOTE_CHECK=1`` downgrades :func:`ensure_base_clone`'s
repository check from an error to a warning, for a workspace whose
``origin`` is deliberately not the repo being scheduled (a fork).
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import shutil
import subprocess
import tempfile
import sys
import textwrap
import tomllib
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

logger = logging.getLogger(__name__)

# Coder output modes: a full-file write (MODE_FULL) or a `git apply`-able
# unified diff (MODE_DIFF). Defined here rather than in agents/coder.py
# since this module — not the Coder — decides how each mode is applied;
# agents/coder.py imports these back for the instructions it gives the LLM.
MODE_FULL = "FULL"
MODE_DIFF = "DIFF"
# Aider-style SEARCH/REPLACE blocks (see _parse_search_replace_blocks): the
# Coder quotes the exact lines to change instead of computing hunk headers
# and context, which is where LLM-written unified diffs most often break.
MODE_EDIT = "EDIT"

# The only forge this package clones from; part of a checkout's
# identity in _repo_identity (#178).
GITHUB_HOST = "github.com"

# Hosts that are github.com under another name: the SSH-over-443 endpoint
# GitHub documents for restrictive firewalls, and the www alias.
_HOST_ALIASES = {"ssh.github.com": GITHUB_HOST, "www.github.com": GITHUB_HOST}

# Set to "1" to downgrade a base-clone repository mismatch from an
# error to a warning (a deliberate fork-origin workspace).
SKIP_REMOTE_CHECK_ENV = "WORM_SKIP_REMOTE_CHECK"

# `cicaid run-ci-checks` reads .cicaid-checks.toml in the target repo (see
# design.md's "Relationship to cicaid"); orchestrator.py/config.py can pass
# a different command (e.g. a plain test runner) via run_ci_checks'/
# run_revision_attempt's ci_command argument.
DEFAULT_CI_COMMAND = ["cicaid", "run-ci-checks", "--all"]

# Matches the per-file sections NativeCoder instructs the LLM to emit (see
# agents/coder.py's FILE_START_MARKER/MODE_MARKER/FILE_END_MARKER):
#   === FILE: <path> ===
#   === MODE: FULL, DIFF or EDIT ===
#   <content>
#   === END FILE ===
_FILE_SECTION_RE = re.compile(
    r"=== FILE: (?P<path>.+?) ===\r?\n"
    r"=== MODE: (?P<mode>FULL|DIFF|EDIT) ===\r?\n"
    r"(?P<body>.*?)"
    # The terminator must be the entire line (modulo trailing spaces/tabs). A
    # bare substring match (the old `\r?\n?=== END FILE ===`) truncated MODE:
    # FULL sections at any `=== END FILE ===` appearing inside file content -
    # e.g. agents/analyser.py's prompt text contains it mid-line - silently
    # dropping the coder's real changes after the lookalike (issue #254).
    # Requiring the marker to start after a newline and then allowing only
    # trailing spaces/tabs before the next newline rules out every mid-line
    # lookalike: any non-whitespace after the marker fails to match.
    r"(?:\r?\n)=== END FILE ===[ \t]*(?:\r?\n|$)",
    re.DOTALL,
)
# The same section grammar split into its two anchors, for the tolerant
# pass in parse_coder_output: a section that the Coder never closed with an
# `=== END FILE ===` line is ended at the next FILE header or at EOF
# instead. Across 169 recorded Coder attempts (2026-09-12 analysis), 31 were
# rejected outright for a missing terminator and 20 of those were under
# 15 KB - the model had written the whole file/diff and then trailed off
# into prose, not hit a token cap. Rejecting the entire response for a
# missing sign-off line threw away work that was otherwise applicable.
_FILE_HEADER_RE = re.compile(
    r"^=== FILE: (?P<path>.+?) ===[ \t]*\r?\n"
    r"=== MODE: (?P<mode>FULL|DIFF|EDIT) ===[ \t]*\r?\n",
    re.MULTILINE,
)
# Whole-line only, for the same #254 reason as above.
_FILE_END_RE = re.compile(r"^=== END FILE ===[ \t]*$", re.MULTILINE)

# A unified diff's own syntax always starts with one of these; used to find
# where a MODE: DIFF section's diff body starts amid surrounding prose.
_DIFF_START_RE = re.compile(r"^(diff --git |--- )", re.MULTILINE)
_HUNK_START_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+\d+(?:,\d+)? @@", re.MULTILINE)
_DIFF_LINE_PREFIXES = ("diff --git", "index ", "---", "+++", "@@", " ", "+", "-", "\\")
# Modalities may wrap a section's body in a Markdown fenced code block
# (```diff / ```python / bare ``` / ...) in addition to the mandated
# FILE/MODE delimiters (agents/coder.py); the fence lines are not diff
# syntax or file content, so strip them before handing the body to git
# apply (MODE_DIFF, issue #248) or writing it straight to disk (MODE_FULL,
# issue #401 - a bare ```python fence silently landed inside a "full file"
# write with no verifier to catch the resulting SyntaxError, since only
# the diff path had fence-stripping). Matches any (or no) language tag,
# not just "diff", since a full-file body is just as likely to be fenced
# with the target language - but *not* at column 0 tolerance: no leading
# whitespace is allowed before the backticks, deliberately, since a real
# wrapping fence is always flush-left while a unified diff's own context
# lines are always prefixed with a space and can legitimately look
# fence-like (e.g. " ```python" when the diff edits a Markdown file) -
# see test_parse_diff_section_preserves_fence_like_context_lines.
_FENCE_RE = re.compile(r"^```[ \t]*[A-Za-z0-9_+-]*[ \t]*\r?\n?$", re.MULTILINE)


class WorkspaceError(RuntimeError):
    """workspace.py itself could not manage the repo (a git command failed
    unexpectedly). See design.md's "Workspace corruption" failure mode -
    this is distinct from a Coder attempt failing normally.
    """


class MalformedOutputError(ValueError):
    """Coder output rejected before it was handed to `git apply` (or before
    a full-file write): unparsable delimiters, an undeclared/unsafe file
    path, or a diff that doesn't apply against the current tree. Distinct
    from a real test failure - see design.md's "Malformed diffs".
    """


# Prefixes WorkspaceResult.error is set to below when a Coder attempt was
# rejected before any test ever ran, so agents/analyser.py can tell "the
# patch was never actually tried" apart from a real CI failure without
# re-parsing free-form error text (see design.md's "Malformed diffs").
MALFORMED_OUTPUT_ERROR_PREFIX = "malformed coder output:"
APPLY_FAILED_ERROR_PREFIX = "apply failed:"

# WorkspaceResult.category: a small, stable vocabulary for *why* an attempt
# failed, so a failure can be recorded and later fed back into a prompt
# ("this issue failed before because X") without re-parsing free-form
# `error`/`failure_detail` text. issue-worm-pro's scheduler/orchestrator
# reuse these same constants for its own failure sites (revision-bound-
# exhausted, review-rejected, coder-unreachable, timeout) rather than
# defining a second vocabulary, so a category means the same thing however
# the run was dispatched. Deliberately plain strings, not an Enum, so pro
# can be ahead or behind this package's pin without an import breaking.
CATEGORY_OUTPUT_SHAPE = "output_shape"
CATEGORY_TEST_FAILURE = "test_failure"
CATEGORY_TIMEOUT = "timeout"
CATEGORY_REVISION_BOUND_EXHAUSTED = "revision_bound_exhausted"
CATEGORY_REVIEW_REJECTED = "review_rejected"
CATEGORY_CODER_UNREACHABLE = "coder_unreachable"
CATEGORY_UNKNOWN = "unknown"


@dataclass
class FileChange:
    """One file's worth of a parsed Coder response."""
    path: str
    mode: str  # MODE_FULL | MODE_DIFF | MODE_EDIT
    body: str
    # How the section was recovered when the Coder's output was not
    # strictly to spec (None when it was): "implicit-terminator" for a
    # section with no `=== END FILE ===` line, ended at the next FILE
    # header or EOF instead. Surfaced so callers can count how often the
    # recovery, rather than the Coder, made the attempt applicable.
    recovery: str | None = None


@dataclass
class WorkspaceResult:
    """Result of applying one revision attempt and running CI checks."""
    success: bool
    test_output: str = ""
    diff_output: str = ""
    error: str | None = None
    # One of the CATEGORY_* constants above when success is False, None
    # otherwise. Set alongside `error` at each failure return below -
    # `error` stays the human-readable detail, `category` is what a caller
    # like history.record_run/cli.py's _fail groups and feeds back on.
    category: str | None = None
    # One entry per recovery step that was needed to parse or apply this
    # attempt (see FileChange.recovery and apply_file_change's ladder), in
    # the form "<path>: <what>". Empty when the Coder's output parsed and
    # applied strictly. Not a failure signal - the attempt still has to
    # pass CI - but the record of how much slack it took.
    recovery: list[str] = field(default_factory=list)


# Subprocess timeout bounds (#45): local git operations are fast, but a
# fresh clone or a real test suite legitimately takes minutes — and none of
# them may be allowed to hang the orchestrator forever.
DEFAULT_GIT_TIMEOUT = 30.0
CLONE_TIMEOUT = 600.0
DEFAULT_CI_TIMEOUT = 600.0
# `git fetch origin` talks to the network; a big repo over a slow link
# legitimately outlasts DEFAULT_GIT_TIMEOUT, but must still be bounded.
FETCH_TIMEOUT = 120.0


def _stdin_is_a_terminal() -> bool:
    """True when stdin is a TTY a human could type a git password into.

    Defensive because this decides whether git may block: a missing or
    closed stdin (pythonw, a daemonised runner, a closed descriptor)
    answers False, so the safe non-interactive path is the default and
    only a stdin we positively know is a terminal opts out of it.
    ``None.isatty`` raises AttributeError, which the same handler covers.
    """
    try:
        return bool(sys.stdin.isatty())
    except (AttributeError, ValueError, OSError):
        # AttributeError: stdin is None, or an object without isatty.
        # ValueError: operation on a closed file. OSError: a stdin whose
        # fileno() cannot be queried. (io.UnsupportedOperation subclasses
        # both of the latter two.)
        return False


def _non_interactive_env(env: dict[str, str] | None) -> dict[str, str] | None:
    """``env`` with every credential prompt disabled off a terminal (#58).

    ``subprocess.run(capture_output=True)`` redirects stdout and stderr
    but *not* stdin, so a git that decides it needs a credential inherits
    the parent's terminal and blocks on a prompt nobody can see. Bounded
    by the call's timeout, so it is not a hang - but a 120s
    ``FETCH_TIMEOUT`` spent waiting for typing is reported as a timeout,
    which names the wrong cause. (The two ``input_text=`` callers are the
    exception: passing ``input=`` puts stdin on a pipe, so ``git apply``
    could never have prompted.)

    One variable does not cover it, because there are three ways to ask:

    ``GIT_TERMINAL_PROMPT=0``
        git's *own* prompt, for HTTP(S) username/password. Yields
        ``could not read Username ... terminal prompts disabled``.
    ``GIT_ASKPASS=""`` and ``SSH_ASKPASS_REQUIRE=never``
        git consults ``GIT_ASKPASS`` -> ``core.askPass`` -> ``SSH_ASKPASS``
        *before* the terminal, and ``GIT_TERMINAL_PROMPT`` gates only that
        last hop. A desktop-launched process inherits one of these (VS
        Code sets ``GIT_ASKPASS``) and would block on a GUI dialog with
        stdin nowhere near a terminal. An empty value reads as "no
        askpass" and, being checked first, also suppresses
        ``core.askPass``.
    ``GIT_SSH_COMMAND=... -o BatchMode=yes``
        for an ``ssh://``/``git@`` remote git execs ``ssh``, which reads a
        key passphrase or a host-key confirmation from ``/dev/tty``
        **directly** - it never sees ``GIT_TERMINAL_PROMPT``, and opening
        ``/dev/tty`` succeeds whenever the process has a controlling
        terminal, whatever stdin points at. This matters most: the
        pre-cloned SSH checkout is the setup the README recommends for
        private repos. Appended to any existing value rather than
        replacing it, so a user's own ``GIT_SSH_COMMAND`` survives.

    Applied only when stdin is not a terminal, so `issue-worm build` run
    by hand can still be prompted the way git normally would; the
    Scheduler, CI, and any invocation whose stdin is redirected get the
    fast failure instead.

    An explicit ``env`` is still the subprocess's full environment, as
    :func:`_run_git` documents - these keys are added to it, not merged
    underneath it, so a caller that deliberately restricts the
    environment (#159) does not get ``os.environ`` leaked back in. Any
    key the caller set explicitly wins.
    """
    if _stdin_is_a_terminal():
        return env
    result = dict(os.environ if env is None else env)
    result.setdefault("GIT_TERMINAL_PROMPT", "0")
    result.setdefault("GIT_ASKPASS", "")
    result.setdefault("SSH_ASKPASS_REQUIRE", "never")
    ssh_command = result.get("GIT_SSH_COMMAND") or "ssh"
    # Match "batchmode=", not bare "batchmode": the option can only be
    # set as `BatchMode=<value>`, so requiring the "=" keeps an
    # incidental mention elsewhere in the command - a path, a
    # ProxyCommand - from reading as "already set". That mistake fails
    # in the unsafe direction: it would skip the append and leave ssh
    # free to prompt, which is the whole bug (#58). Case-insensitive
    # because ssh's own option parsing is.
    if "batchmode=" not in ssh_command.lower():
        result["GIT_SSH_COMMAND"] = f"{ssh_command} -o BatchMode=yes"
    return result


def _run_git(
    repo_path: str,
    *args: str,
    check: bool = True,
    input_text: str | None = None,
    timeout: float | None = None,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess:
    """Run a git command in ``repo_path`` with a bounded subprocess timeout.

    ``timeout`` defaults to :data:`DEFAULT_GIT_TIMEOUT` (local git
    operations are fast; the bound exists to turn a hung git process —
    network filesystem, stuck lock — into a :class:`WorkspaceError`
    instead of an indefinite orchestrator block, #45).

    ``env`` is the subprocess's **full** environment, not additions to it
    - the same convention as :func:`run_ci_checks` and ``subprocess.run``
    itself. None inherits the parent's, as before. A caller wanting to
    add one variable must spread the parent explicitly::

        env={**os.environ, "GIT_TERMINAL_PROMPT": "0"}

    When stdin is not a terminal, ``GIT_TERMINAL_PROMPT=0`` is added by
    default (#58) - see :func:`_non_interactive_env`. An explicit ``env``
    still wins, so a caller that sets the variable itself keeps its value
    on a TTY too.
    """
    effective_timeout = DEFAULT_GIT_TIMEOUT if timeout is None else timeout
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=repo_path,
            capture_output=True,
            text=True,
            encoding="utf-8",
            input=input_text,
            timeout=effective_timeout,
            env=_non_interactive_env(env),
        )
    except subprocess.TimeoutExpired as exc:
        raise WorkspaceError(
            f"git {' '.join(args)} timed out after {effective_timeout}s"
        ) from exc
    if check and result.returncode != 0:
        raise WorkspaceError(f"git {' '.join(args)} failed: {result.stderr.strip()}")
    return result


def get_current_commit(repo_path: str) -> str:
    """Return the current HEAD commit hash - the caller's starting point
    before any revision attempts begin.
    """
    return _run_git(repo_path, "rev-parse", "HEAD").stdout.strip()


def reset_to_commit(repo_path: str, commit: str) -> None:
    """Discard all working-tree/index changes and reset back to `commit`.

    Called before every attempt and on every failure, so a failed attempt's
    changes never bleed into the next one (see design.md's Analyser
    section) and a successful final attempt is the only thing left on disk.
    """
    _run_git(repo_path, "reset", "--hard", commit)
    _run_git(repo_path, "clean", "-fd")


def refresh_to_main(repo_path: str) -> None:
    """Fetch origin and put the checkout on the latest ``main``.

    The unified flow's per-pass refresh (#301): ``ensure_base_clone``
    never fetches an existing checkout, so without this a long-running
    Scheduler would keep reviewing and branching from stale code. Hard
    resets (``checkout --force`` + ``reset --hard``) because the caller
    guarantees a clean workspace first — see scheduler's
    ``_workspace_is_dirty`` guard. The local ``main`` branch is recreated
    at ``origin/main`` if it is stale or missing.

    Raises:
        WorkspaceError when fetch or reset fails.
    """
    _run_git(repo_path, "fetch", "origin", timeout=FETCH_TIMEOUT)
    _run_git(repo_path, "checkout", "--force", "-B", "main", "origin/main")
    _run_git(repo_path, "reset", "--hard", "origin/main")


class _RollbackGuard:
    """Resets the repo to `start_commit` on the way out unless disarmed.

    Using a context manager (rather than a plain try/except) means the
    reset also runs when the attempt is interrupted mid-run - e.g. a
    KeyboardInterrupt raised out of the CI-check subprocess call - not only
    on a normally-returned failure. See design.md's "Workspace corruption"
    and the rollback-path test coverage this issue asks for.
    """

    def __init__(self, repo_path: str, start_commit: str):
        self.repo_path = repo_path
        self.start_commit = start_commit
        self._armed = True

    def disarm(self) -> None:
        self._armed = False

    def __enter__(self) -> "_RollbackGuard":
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        if self._armed:
            try:
                reset_to_commit(self.repo_path, self.start_commit)
            except WorkspaceError:
                logger.error("Rollback to %s failed", self.start_commit, exc_info=True)
                if exc_type is None:
                    # No exception already in flight - the rollback failure
                    # itself is the news, so surface it instead of returning
                    # normally with a corrupted workspace left behind.
                    raise
        return False  # never suppress the original exception, if any


def _escapes_repo(path: str) -> bool:
    normalised = PurePosixPath(path.replace("\\", "/"))
    return normalised.is_absolute() or ".." in normalised.parts


# Characters never valid in a path on Windows. Coder output and Triage's
# FILES: lines frequently decorate real paths with markdown or glob
# syntax; any leftover of these after stripping means the entry is not a
# usable file path.
_INVALID_PATH_CHARS = '<>:"|?*'


def sanitize_file_path(path: str) -> str | None:
    """Normalize a possibly LLM-decorated path; None when not a usable path.

    The Coder echoes paths through ``=== FILE: ... ===`` markers and
    Triage writes FILES: lines from a small local model; both routinely
    decorate them with markdown backticks/emphasis (``** `x.py` **``),
    leading glob prefixes (``** README.md``), or trailing asides
    (``docs/ (if applicable)``). Taken literally, those paths fail on
    Windows with ``[Errno 22] Invalid argument`` (``*`` and backticks
    are not valid filename characters), so every path crossing into file
    I/O is sanitized here: markdown/glob decoration is stripped, Windows
    separators are normalized to ``/``, and anything that is still empty,
    absolute, ``.``/``..``, or carrying invalid characters is rejected.

    Args:
        path: A raw path extracted from LLM output.

    Returns:
        The normalized relative path, or None when the entry is not a
        usable file path and should be dropped.
    """
    if not path or not path.strip():
        return None
    cleaned = path.strip()
    # Backticks are markdown decoration, never part of a path.
    cleaned = re.sub(r"`", "", cleaned)
    # Leading glob prefixes ("** README.md") are decoration; any `*` that
    # survives this (i.e. sits mid-path) is a glob pattern, not a filename
    # — rejected below via _INVALID_PATH_CHARS.
    cleaned = cleaned.lstrip("*").strip()
    # Trailing parenthetical asides, only when whitespace-separated:
    # "docs/ (if applicable)" is an aside, but "src/foo(bar).py" is a real
    # filename and must be kept.
    cleaned = re.sub(r"\s+\([^)]*\)\s*$", "", cleaned).strip()
    cleaned = cleaned.replace("\\", "/")
    if not cleaned or cleaned in (".", ".."):
        return None
    if cleaned.startswith("/") or re.match(r"^[A-Za-z]:", cleaned):
        return None  # absolute path — never allowed
    if any(ch in cleaned for ch in _INVALID_PATH_CHARS) or any(
        ord(ch) < 32 for ch in cleaned
    ):
        return None
    return cleaned


def _strip_edge_fences(text: str) -> str:
    """Drop a Markdown fence line wrapping `text`, if present.

    Only fence lines at the very edges are removed - a fence-like line in
    the *middle* of the text (e.g. a " ```" context line in a diff that
    edits a Markdown file, or a triple-backtick a full-file rewrite is
    legitimately meant to contain) is real content and must be preserved.
    A no-op when there's no wrapping fence, so callers can apply this
    unconditionally rather than needing to detect fencing themselves.
    """
    lines = text.splitlines()
    if lines and _FENCE_RE.match(lines[0]):
        lines = lines[1:]
    if lines and _FENCE_RE.match(lines[-1]):
        lines = lines[:-1]
    return "\n".join(lines)


def _extract_diff(body: str, path: str | None = None) -> str | None:
    """Pull the unified-diff hunk out of a MODE: DIFF section's body.

    NativeCoder's prompt asks for "a unified diff ... with a brief
    explanation" (agents/coder.py), so the body can have prose before
    and/or after the actual diff. Finds where diff syntax starts, then
    stops at the first line that no longer looks diff-shaped. ``path`` is
    the section's declared file, used to synthesise the ``---``/``+++``
    header when the Coder sent bare hunks.
    """
    match = _DIFF_START_RE.search(body)
    if not match:
        # No `diff --git` / `---` header at all. A Coder that was told the
        # file's path in the FILE marker often answers with bare hunks
        # (`@@ -n,m +n,m @@` onward, usually inside a ```diff fence): the
        # header is redundant with the marker, so synthesise it for the
        # declared path rather than reject the section (2026-09-12: every
        # attempt on a 123 KB file failed this way twice before a retry
        # happened to include the header).
        hunk = _HUNK_START_RE.search(body)
        if not hunk or not path:
            return None
        body = f"--- a/{path}\n+++ b/{path}\n" + body[hunk.start():]
        match = _DIFF_START_RE.search(body)
        assert match is not None

    lines = body[match.start():].splitlines()
    end = len(lines)
    for index, line in enumerate(lines):
        if index == 0:
            continue
        if line.startswith(_DIFF_LINE_PREFIXES) or line.strip() == "":
            continue
        end = index
        break

    diff_text = "\n".join(lines[:end]).rstrip("\n")
    if not diff_text:
        return None
    # Drop a Markdown fence line wrapping the block (issue #248): the
    # Coder's ```diff / ``` wrapper is not diff syntax, and handing it to
    # git apply produces a "corrupt patch" error.
    diff_text = _strip_edge_fences(diff_text)
    return diff_text.rstrip("\n") + "\n"


def _split_sections(output: str) -> list[tuple[str, str, str, str | None]]:
    """Split a Coder response into (path, mode, body, recovery) sections.

    A section runs from its FILE/MODE header to its `=== END FILE ===` line
    (recovery None). One with no terminator before the next header, or
    before EOF, is ended there instead and tagged with a recovery note -
    the Coder's sign-off line is a parsing convenience, not part of the
    change, and its absence alone should not discard an otherwise
    complete file or diff (see _FILE_HEADER_RE). Text before the first
    header is ignored, as before.
    """
    headers = list(_FILE_HEADER_RE.finditer(output))
    sections: list[tuple[str, str, str, str | None]] = []
    for index, header in enumerate(headers):
        region_end = headers[index + 1].start() if index + 1 < len(headers) else len(output)
        region = output[header.end():region_end]
        end_marker = _FILE_END_RE.search(region)
        if end_marker:
            body = region[: end_marker.start()]
            recovery = None
        else:
            body = region
            recovery = (
                "implicit-terminator: ended at next FILE header"
                if index + 1 < len(headers)
                else "implicit-terminator: ended at end of output"
            )
        sections.append((header.group("path"), header.group("mode"), body, recovery))
    return sections


def _drop_prose_after_closing_fence(body: str) -> str:
    """For a fenced full-file body, drop anything after the closing fence.

    Only applies when the body *opens* with a fence line: then the first
    later fence line closes the file content and whatever follows is the
    model's commentary. An unfenced body is returned unchanged - there is
    no reliable way to tell trailing prose from trailing code, and the
    Verifier is the backstop for that.
    """
    lines = body.splitlines()
    if not lines or not _FENCE_RE.match(lines[0]):
        return body
    for index in range(1, len(lines)):
        if _FENCE_RE.match(lines[index]):
            return "\n".join(lines[: index + 1])
    return body


# Repetition guards (see issue-worm-pro runs #582/#623, 2026-09-13). A Coder
# response can degenerate in two ways that the section grammar alone does
# not catch:
#
# 1. The same FILE path is emitted more than once. Seen live: the model
#    "planned" out loud with FILE/MODE headers carrying placeholder bodies
#    ("[diff]", "(no change needed)"), never closed with END FILE, and only
#    then wrote the real, END FILE-terminated sections - 18 sections for 6
#    files. Treating the first placeholder as the change rejected the whole
#    response ("contains no parseable unified diff") although a complete
#    answer followed it. The same shape arises from a verbatim loop that
#    runs into the output cap: complete copies, then a truncated one.
# 2. A block of lines inside ONE section repeats over and over (a
#    generation loop that keeps "succeeding" at producing output).
#
# _resolve_repeated_sections keeps exactly one section per path when that
# is unambiguous and otherwise fails with a message naming the path and the
# count; _detect_line_repetition flags case 2.

# A repeated block must be at least this many lines and repeat at least this
# many times, consecutively, to be flagged - short accidental repeats (e.g.
# three near-identical `if` branches) are normal code and must not trip it.
_MIN_REPEATED_BLOCK_LINES = 3
_MIN_BLOCK_REPEATS = 4
# ...and the repeated run must cover a meaningful share of the section, so a
# small repeated snippet inside an otherwise large, legitimate file (a table
# of test cases, say) doesn't fail the whole attempt.
_MIN_REPEATED_BLOCK_COVERAGE = 0.3
# Bounding the block size searched caps the scan at O(n * max_block *
# block_len) rather than letting it grow quadratically in body length: a
# degenerate loop repeats a short pattern, never a huge one.
_MAX_REPEATED_BLOCK_LINES = 60


def _detect_line_repetition(body: str) -> str | None:
    """Find a contiguous block of lines repeated many times in ``body``.

    Returns a short description of the largest such run (by lines
    covered), or None when no block meets both the repeat-count and the
    coverage thresholds above.
    """
    lines = body.splitlines()
    n = len(lines)
    if n < _MIN_REPEATED_BLOCK_LINES * _MIN_BLOCK_REPEATS:
        return None

    best: tuple[int, int, int] | None = None  # (lines covered, block len, repeats)
    max_block = min(_MAX_REPEATED_BLOCK_LINES, n // _MIN_BLOCK_REPEATS)
    for block_len in range(_MIN_REPEATED_BLOCK_LINES, max_block + 1):
        i = 0
        while i + block_len * 2 <= n:
            block = lines[i : i + block_len]
            if not any(line.strip() for line in block):
                # Blank padding repeating is whitespace, not a content loop.
                i += 1
                continue
            repeats = 1
            j = i + block_len
            while j + block_len <= n and lines[j : j + block_len] == block:
                repeats += 1
                j += block_len
            if repeats >= _MIN_BLOCK_REPEATS:
                covered = block_len * repeats
                if covered / n >= _MIN_REPEATED_BLOCK_COVERAGE and (
                    best is None or covered > best[0]
                ):
                    best = (covered, block_len, repeats)
                i = j
            else:
                i += 1
    if best is None:
        return None
    _, block_len, repeats = best
    return f"a {block_len}-line block repeated {repeats} times in a row"


# A MODE: FULL section replaces its whole file, so a reply that ran out of
# room mid-file still parses cleanly: the section is present (the missing-
# declared-file(s) check only fires when one is absent, never when one is
# short) and it does not repeat itself (so _detect_line_repetition sees
# nothing). What lands is a fraction of the file that was there, and the
# attempt is reported as a success.
#
# Recorded live (leonarduk/allotmint-pro#44, 2026-09-20): three separate
# coders - a local 7B, a local 14B, and a cloud model that hit its
# 32768-token output cap - each rewrote a 1583-line CDK stack as 341-366
# lines of plausible, correct-looking code that kept the imports and the
# class but dropped ~77% of the body. All three runs finished "completed".
#
# Only a rewrite of an existing file can be judged this way, and only a
# substantial one: deleting most of a short file is ordinary work, and so
# is a deliberate cut. Both thresholds are deliberately permissive - the
# aim is to catch a body that lost most of a large file, not to police
# deletions.
_MIN_REWRITE_LINES = 200
_MAX_REWRITE_SHRINK = 0.5


def _detect_truncated_rewrite(before: str, after: str) -> str | None:
    """Flag a MODE: FULL body that drops most of the file it replaces.

    Returns a short description of the shrinkage, or None when the file
    was too small to judge or enough of it survived.
    """
    old = len(before.splitlines())
    if old < _MIN_REWRITE_LINES:
        return None
    new = len(after.splitlines())
    if new > old * _MAX_REWRITE_SHRINK:
        return None
    return (
        f"keeps {new} of {old} lines - looks like a truncated rewrite, "
        "refusing to apply it"
    )


def _resolve_repeated_sections(
    sections: list[tuple[str, str, str, str | None]],
) -> list[tuple[str, str, str, str | None]]:
    """Collapse (path, mode, body, recovery) sections to one per path.

    ``path`` must already be sanitized. Paths keep their first-seen order.
    A path seen once passes through untouched. For a repeated path:

    - every copy identical (same mode, same body modulo edge blank lines):
      keep one;
    - otherwise, if the copies closed with an explicit ``=== END FILE ===``
      (recovery None) all agree and every other copy is unterminated: keep
      the terminated one. The unterminated copies are drafts (placeholder
      headers written while planning) or a loop cut off by the output cap;
      the terminated copy is the one the Coder actually finished;
    - anything else is ambiguous: MalformedOutputError naming the path and
      how many times it was emitted.

    A kept section's recovery note records what was dropped, so callers
    can count how often this rescue, not the Coder, made an attempt usable.
    """
    order: list[str] = []
    groups: dict[str, list[tuple[str, str, str | None]]] = {}
    for path, mode, body, recovery in sections:
        if path not in groups:
            order.append(path)
            groups[path] = []
        groups[path].append((mode, body, recovery))

    resolved: list[tuple[str, str, str, str | None]] = []
    for path in order:
        entries = groups[path]
        if len(entries) == 1:
            mode, body, recovery = entries[0]
            resolved.append((path, mode, body, recovery))
            continue

        def key(entry: tuple[str, str, str | None]) -> tuple[str, str]:
            return entry[0], entry[1].strip("\n")

        terminated = [entry for entry in entries if entry[2] is None]
        if len({key(entry) for entry in entries}) == 1:
            kept = terminated[0] if terminated else entries[0]
            note = f"kept 1 of {len(entries)} identical repeated FILE sections"
        elif terminated and len({key(entry) for entry in terminated}) == 1:
            kept = terminated[0]
            dropped = len(entries) - len(terminated)
            note = (
                f"kept the END FILE-terminated section, dropped {dropped} "
                f"unterminated draft/looped section(s) ({len(entries)} emitted)"
            )
        else:
            raise MalformedOutputError(
                f"coder output emits a FILE section for {path!r} "
                f"{len(entries)} times with differing content (looks like a "
                "repetition loop) - emit exactly one section per file"
            )
        mode, body, recovery = kept
        resolved.append((path, mode, body, f"{recovery}; {note}" if recovery else note))
    return resolved


# MODE: EDIT body grammar - one or more of:
#   <<<<<<< SEARCH
#   <lines copied verbatim from the current file>
#   =======
#   <lines to put in their place>
#   >>>>>>> REPLACE
# Anything outside a block (prose, Markdown fences) is ignored. Marker
# lines tolerate 5-9 marker characters and trailing whitespace, since
# models miscount them.
#
# A diff-header-style spelling is accepted too. qwen3.8-216k with thinking
# off wrote its EDIT sections this way when asked to produce a fix on
# leonarduk/cicaid#51 run 5 (as the Analyser, which had been asked for
# instructions and answered with a full reply instead) - a spelling a
# Coder reply from the same model would be rejected unapplied for, with
# nothing else wrong in it:
#   --- SEARCH
#   <lines copied verbatim from the current file>
#   +++ REPLACE
#   <lines to put in their place>
# It has no closing line: the block ends at the next SEARCH marker, at a
# '>>>>>>> REPLACE' line if the model adds one, or at the end of the body.
# Only a block OPENED with '--- SEARCH' gets that leniency; for the
# canonical grammar an unclosed block still reads as a truncated reply.
_SEARCH_MARKER_RE = re.compile(r"^<{5,9} ?SEARCH[ \t]*$")
_DIVIDER_MARKER_RE = re.compile(r"^={5,9}[ \t]*$")
_DIFF_SEARCH_MARKER_RE = re.compile(r"^-{3,9} ?SEARCH[ \t]*$")
_DIFF_DIVIDER_MARKER_RE = re.compile(r"^\+{3,9} ?REPLACE[ \t]*$")
# Files where a line of "=" is legitimate content (a setext/rst heading
# underline), so a divider-shaped line inside a REPLACE part is kept rather
# than rejected as a second divider (#338).
_MARKUP_SUFFIXES = (".md", ".markdown", ".rst", ".txt", ".adoc")
_REPLACE_MARKER_RE = re.compile(r"^>{5,9} ?REPLACE[ \t]*$")

# Fallbacks, in order, when a SEARCH block has no exact match. Each is
# only used when it finds exactly one match; the weakest one needed for a
# file is reported like apply_file_change's diff ladder rungs.
EDIT_RUNG_WHITESPACE = "edit-whitespace-tolerant"
EDIT_RUNG_INDENT = "edit-indentation-tolerant"


def _parse_search_replace_blocks(body: str, path: str) -> list[tuple[str, str]]:
    """Return the (search, replace) pairs of a MODE: EDIT body, in order.

    Raises MalformedOutputError for a body with no blocks or a block whose
    markers are out of order or never closed (usually a truncated reply).
    """
    blocks: list[tuple[str, str]] = []
    state = "outside"
    search: list[str] = []
    replace: list[str] = []
    # True while inside a block opened with '--- SEARCH' (see the grammar
    # comment above): such a block may end at the next SEARCH marker or at
    # the end of the body instead of at a '>>>>>>> REPLACE' line.
    diff_style = False

    def is_search_marker(text: str) -> bool:
        return bool(_SEARCH_MARKER_RE.match(text) or _DIFF_SEARCH_MARKER_RE.match(text))

    for line in body.splitlines():
        stripped = line.rstrip("\r")
        if state == "outside":
            if is_search_marker(stripped):
                state, search, replace = "search", [], []
                diff_style = bool(_DIFF_SEARCH_MARKER_RE.match(stripped))
        elif state == "search":
            if _DIVIDER_MARKER_RE.match(stripped) or _DIFF_DIVIDER_MARKER_RE.match(
                stripped
            ):
                state = "replace"
            elif is_search_marker(stripped) or _REPLACE_MARKER_RE.match(stripped):
                raise MalformedOutputError(
                    f"MODE: EDIT section for {path!r}: SEARCH block "
                    f"{len(blocks) + 1} has no '=======' divider"
                )
            else:
                search.append(line)
        else:  # replace
            if _REPLACE_MARKER_RE.match(stripped):
                blocks.append(("\n".join(search), "\n".join(replace)))
                state = "outside"
            elif is_search_marker(stripped):
                if not diff_style:
                    raise MalformedOutputError(
                        f"MODE: EDIT section for {path!r}: block {len(blocks) + 1} "
                        "has no '>>>>>>> REPLACE' line before the next SEARCH"
                    )
                # Diff-style: the next SEARCH marker is this block's end.
                blocks.append(("\n".join(search), "\n".join(replace)))
                state, search, replace = "search", [], []
                diff_style = bool(_DIFF_SEARCH_MARKER_RE.match(stripped))
            elif _DIVIDER_MARKER_RE.match(stripped) and not path.lower().endswith(
                _MARKUP_SUFFIXES
            ):
                # A block has exactly one divider. A second one inside the
                # REPLACE part is the Coder mangling the format; taking it
                # as content pastes a literal "=======" into the file, which
                # only surfaces later as a bare SyntaxError (#338). Markup
                # files are exempt: a line of "=" underlines a heading there.
                raise MalformedOutputError(
                    f"MODE: EDIT section for {path!r}: block {len(blocks) + 1} "
                    "has a second '=======' divider - each block has exactly "
                    "one, between the SEARCH and REPLACE text"
                )
            else:
                replace.append(line)
    if state == "replace" and diff_style:
        # Diff-style has no closing line: end of body closes the block.
        blocks.append(("\n".join(search), "\n".join(replace)))
        state = "outside"
    if state != "outside":
        raise MalformedOutputError(
            f"MODE: EDIT section for {path!r}: block {len(blocks) + 1} is not "
            "closed with '>>>>>>> REPLACE' (response may have been truncated)"
        )
    if not blocks:
        raise MalformedOutputError(
            f"MODE: EDIT section for {path!r} contains no "
            "'<<<<<<< SEARCH / ======= / >>>>>>> REPLACE' blocks"
        )
    return blocks


def _leading_ws(line: str) -> str:
    return line[: len(line) - len(line.lstrip())]


def _reindent(
    lines: list[str], search_lines: list[str], matched: list[str]
) -> list[str]:
    """Shift ``lines`` by the indentation difference between the first
    non-blank SEARCH line and the file line it matched."""
    for want, got in zip(search_lines, matched):
        if want.strip():
            old, new = _leading_ws(want), _leading_ws(got)
            break
    else:
        return lines
    if old == new:
        return lines
    out = []
    for line in lines:
        if not line.strip():
            out.append(line)
        elif line.startswith(old):
            out.append(new + line[len(old) :])
        else:
            out.append(line)
    return out


def _apply_one_block(
    text: str, search: str, replace: str, path: str, number: int
) -> tuple[str, str | None]:
    """Apply one SEARCH/REPLACE pair to ``text``; return (text, rung)."""
    if not search.strip():
        if text.strip():
            raise MalformedOutputError(
                f"MODE: EDIT block {number} for {path!r} has an empty SEARCH "
                "but the file is not empty - quote the lines to replace"
            )
        return (replace + "\n" if replace else ""), None

    # Match whole lines only: both the SEARCH text and the file are wrapped
    # in newlines, so "x = 1" can't match inside "max = 1". The file view is
    # newline-terminated so a SEARCH quoting the last line still matches
    # when the file has no final newline; that absence is restored after,
    # because an edit must not change the end-of-file shape as a side
    # effect. Occurrences are counted with a lookahead, since adjacent
    # identical lines share a newline and str.count would undercount them.
    exact = "\n" + search + "\n"
    had_final_newline = text.endswith("\n")
    probe = "\n" + (text if had_final_newline else text + "\n")
    count = len(re.findall("(?=" + re.escape(exact) + ")", probe))
    if count == 1:
        out = probe.replace(exact, "\n" + (replace + "\n" if replace else ""), 1)[1:]
        if not had_final_newline and out.endswith("\n"):
            out = out[:-1]
        return out, None
    if count > 1:
        raise MalformedOutputError(
            f"MODE: EDIT block {number} for {path!r}: SEARCH text matches "
            f"{count} places - include more surrounding lines so it is unique"
        )

    file_lines = text.splitlines(keepends=True)
    search_lines = search.splitlines()
    size = len(search_lines)
    for rung, norm in (
        (EDIT_RUNG_WHITESPACE, lambda s: s.rstrip()),
        (EDIT_RUNG_INDENT, lambda s: s.strip()),
    ):
        want = [norm(line) for line in search_lines]
        have = [norm(line) for line in file_lines]
        hits = [
            i for i in range(len(file_lines) - size + 1) if have[i : i + size] == want
        ]
        if len(hits) > 1:
            raise MalformedOutputError(
                f"MODE: EDIT block {number} for {path!r}: SEARCH text matches "
                f"{len(hits)} places ({rung}) - include more surrounding lines "
                "so it is unique"
            )
        if hits:
            start = hits[0]
            matched = [line.rstrip("\r\n") for line in file_lines[start : start + size]]
            new_lines = replace.splitlines()
            if rung == EDIT_RUNG_INDENT:
                new_lines = _reindent(new_lines, search_lines, matched)
            eol = "\r\n" if file_lines[start].endswith("\r\n") else "\n"
            chunk = "".join(line + eol for line in new_lines)
            out = (
                "".join(file_lines[:start])
                + chunk
                + "".join(file_lines[start + size :])
            )
            # Same end-of-file rule as the exact path: an edit that reaches
            # the last line must not add a final newline the file lacked.
            if (
                not had_final_newline
                and start + size == len(file_lines)
                and out.endswith(eol)
            ):
                out = out[: -len(eol)]
            return out, rung

    first = next((line.strip() for line in search_lines if line.strip()), "")
    raise MalformedOutputError(
        f"MODE: EDIT block {number} for {path!r}: SEARCH text not found in the "
        f"current file (first line: {first[:80]!r}) - copy it verbatim"
    )


def _apply_search_replace(
    content: str | None, body: str, path: str
) -> tuple[str, str | None]:
    """Apply a MODE: EDIT body to ``content`` (None: file doesn't exist).

    Every block is applied in memory, in order, before anything is
    written, so a bad block leaves the file untouched. Returns the new
    content and the weakest fallback needed (None when every block
    matched exactly). Raises MalformedOutputError when a SEARCH block is
    missing, or matches more than once, at every rung.
    """
    text = "" if content is None else content
    blocks = _parse_search_replace_blocks(body, path)
    # A missing file can only be created by an empty first SEARCH; later
    # blocks may then edit what that block wrote, so only the first one
    # is checked here - a bad later block fails in _apply_one_block with
    # its own "not found" message.
    if content is None and blocks and blocks[0][0].strip():
        raise MalformedOutputError(
            f"MODE: EDIT section for {path!r}: the file does not exist - use "
            "MODE: FULL (or an empty SEARCH) to create it"
        )
    order = (None, EDIT_RUNG_WHITESPACE, EDIT_RUNG_INDENT)
    worst: str | None = None
    for number, (search, replace) in enumerate(blocks, 1):
        text, rung = _apply_one_block(text, search, replace, path, number)
        if order.index(rung) > order.index(worst):
            worst = rung
    return text, worst


def parse_coder_output(output: str, declared_files: list[str]) -> list[FileChange]:
    """Split a Coder response into per-file changes, rejecting anything
    that isn't safe to hand to git apply / a direct file write.

    Raises MalformedOutputError (not a generic exception) so callers can
    tell "the Coder's patch was never actually tried" apart from a real
    test failure - see design.md's "Malformed diffs".
    """
    if not output or not output.strip():
        raise MalformedOutputError("coder output is empty")

    sections = _split_sections(output)
    if not sections:
        raise MalformedOutputError(
            "no '=== FILE: ... === / === MODE: ... === / === END FILE ===' "
            "sections found in coder output"
        )

    # Declared files are normalized the same way the FILE marker path is,
    # so a Coder that echoes back a decorated path (``** `x.py` **``) still
    # matches its declared, sanitized file.
    declared = {sanitize_file_path(f) for f in declared_files}
    declared.discard(None)

    validated: list[tuple[str, str, str, str | None]] = []
    for path, mode, body, recovery in sections:
        path = path.strip()
        normalized = sanitize_file_path(path)
        if normalized is None:
            raise MalformedOutputError(
                f"coder output declares an invalid file path: {path!r}"
            )
        path = normalized
        if path not in declared:
            raise MalformedOutputError(
                f"coder output touches undeclared file {path!r} "
                f"(declared files: {sorted(declared)})"
            )
        if _escapes_repo(path):
            raise MalformedOutputError(f"file path escapes the repository: {path!r}")
        validated.append((path, mode.strip(), body.strip("\n"), recovery))

    changes: list[FileChange] = []
    seen_paths: set[str] = set()
    for path, mode, body, recovery in _resolve_repeated_sections(validated):
        seen_paths.add(path)

        if mode == MODE_DIFF:
            diff_text = _extract_diff(body, path)
            if diff_text is None:
                raise MalformedOutputError(
                    f"MODE: DIFF section for {path!r} contains no parseable unified diff"
                )
            if not _DIFF_START_RE.search(body):
                recovery = (
                    f"{recovery}; synthesised diff header" if recovery else "synthesised diff header"
                )
            body = diff_text
        elif mode == MODE_EDIT:
            # Fences around (or between) blocks are ignored by the block
            # parser; validating here fails a malformed or truncated EDIT
            # section before anything is applied.
            body = _strip_edge_fences(body)
            _parse_search_replace_blocks(body, path)
        else:
            if recovery is not None:
                # An unterminated FULL section may carry the model's
                # sign-off prose after the file content. When the content
                # was fenced, the closing fence marks where the file ends
                # and everything after it is commentary, not code.
                body = _drop_prose_after_closing_fence(body)
            # A full-file rewrite wrapped in a Markdown fence (```python /
            # bare ``` / ...) is not part of the file's real content, and
            # there's no verifier here (free-tier build, #401) to catch the
            # resulting SyntaxError the way issue-worm-pro's loop would -
            # strip it before writing, the same way MODE_DIFF already does
            # for its own fencing (issue #248).
            body = _strip_edge_fences(body)

        loop = _detect_line_repetition(body)
        if loop is not None:
            raise MalformedOutputError(
                f"MODE: {mode} section for {path!r} contains {loop} - looks "
                "like a generation loop, refusing to apply it"
            )

        if recovery is not None:
            logger.warning(
                "parse_coder_output: FILE section for %r needed recovery: %s",
                path,
                recovery,
            )
        changes.append(FileChange(path=path, mode=mode, body=body, recovery=recovery))

    missing = declared - seen_paths
    if missing:
        # A declared file with no FILE section usually means the Coder's
        # response was cut off mid-section (e.g. hit a provider's output
        # cap) rather than deliberately omitted - surfacing it here beats
        # silently handing back a partial patch that fails to apply later.
        raise MalformedOutputError(
            f"coder output is missing declared file(s) {sorted(missing)} "
            "(response may have been truncated)"
        )

    return changes


# How a MODE: DIFF body is handed to `git apply`, strictest first. The
# recorded failure data (2026-09-12: 41 of 169 Coder attempts rejected with
# "patch does not apply", every one a context mismatch, none a corrupt
# patch) says an LLM's diff usually has the right +/- lines and slightly
# wrong context: whitespace drift, a blank line more or less, a line number
# that moved. Re-applying the same 30 failed sections against their real
# base commit: 8 applied strictly, 26 with one line of context, 28 with
# none. Each rung below is tried with `--check` first so nothing lands
# partially; the rung that applied is reported so callers can see how
# much slack the attempt needed. `-C0` is last and is the risky rung: with
# no context a pure-addition hunk lands where its header says, so the CI
# checks that follow are the only thing standing between it and a
# misplaced insertion. `--3way` is deliberately absent: it needs the
# `index` blob ids an LLM diff never carries.
def _every_hunk_has_a_preimage(diff_text: str) -> bool:
    """True when each `@@` hunk in ``diff_text`` removes at least one line.

    Such a hunk carries a preimage git must match before applying, even
    with zero context; a pure-addition hunk carries none and would be
    placed by line number alone (see apply_file_change's -C0 rung).
    """
    hunks = re.split(r"(?m)^@@ .*$", diff_text)[1:]
    if not hunks:
        return False
    return all(
        any(line.startswith("-") and not line.startswith("---") for line in hunk.splitlines())
        for hunk in hunks
    )


_APPLY_LADDER: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("strict", ("--recount",)),
    ("ignore-whitespace", ("--recount", "--ignore-whitespace")),
    ("context-1", ("--recount", "-C1")),
    ("context-1-ignore-whitespace", ("--recount", "-C1", "--ignore-whitespace")),
)
# The zero-context rung is OFF by default. It trades a loud failure (the
# patch does not apply: obvious, costs one retry) for a quiet one (the
# patch applies in the wrong place, tests stay green, a reviewer gets a
# wrong PR) - the wrong trade for an unattended tool. It rescued 4 of 30
# recorded failures that -C1 did not, and one of those placements was a
# live unmergeable PR. Opt in per process with ISSUE_WORM_APPLY_CONTEXT0=1;
# even then it is skipped for hunks that only add lines (no preimage).
_CONTEXT0_RUNG: tuple[str, tuple[str, ...]] = ("context-0", ("--recount", "-C0"))
APPLY_CONTEXT0_ENV = "ISSUE_WORM_APPLY_CONTEXT0"


def _apply_ladder() -> tuple[tuple[str, tuple[str, ...]], ...]:
    """The rungs to try, with the opt-in zero-context rung appended only
    when :data:`APPLY_CONTEXT0_ENV` is set to a truthy value."""
    if os.environ.get(APPLY_CONTEXT0_ENV, "").strip().lower() in ("1", "true", "yes", "on"):
        return _APPLY_LADDER + (_CONTEXT0_RUNG,)
    return _APPLY_LADDER


def apply_file_change(repo_path: str, change: FileChange) -> str | None:
    """Apply one parsed file change: a direct write for MODE_FULL,
    in-memory SEARCH/REPLACE for MODE_EDIT (see _apply_search_replace), or
    `git apply` (pre-checked with --check) for MODE_DIFF.

    For a diff, the rungs of :data:`_APPLY_LADDER` are tried in order and
    the first whose `--check` passes is applied. Returns None for a
    full-file write or a strictly-applied diff, else the name of the rung
    that was needed ("ignore-whitespace", "context-1", ...).

    Raises MalformedOutputError if no rung applies cleanly against the
    current tree - checked before the real apply so a bad patch can't
    partially land. The error carries the STRICT rung's git message, which
    names the first mismatching hunk and is the most useful to a reader.

    Also raises MalformedOutputError when a MODE_FULL body would replace an
    existing file with a fraction of its lines (see
    :func:`_detect_truncated_rewrite`) - a reply cut off mid-file parses
    like a complete one, so this is the only place it can be caught.
    """
    if change.mode == MODE_FULL:
        target = Path(repo_path) / change.path
        content = change.body
        if content and not content.endswith("\n"):
            content += "\n"
        if target.is_file():
            # Checked before the write, not after: a full-file section is
            # the one mode that destroys what it replaces, so a body that
            # lost most of the file has to be refused rather than written
            # and rolled back. errors="replace" because this is a size
            # comparison - a file that doesn't decode cleanly still has a
            # line count worth knowing, and refusing to apply over it for
            # an encoding reason would be its own bug.
            before = target.read_text(encoding="utf-8", errors="replace")
            shrink = _detect_truncated_rewrite(before, content)
            if shrink is not None:
                raise MalformedOutputError(
                    f"MODE: {MODE_FULL} section for {change.path!r} {shrink}"
                )
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        return None

    if change.mode == MODE_EDIT:
        target = Path(repo_path) / change.path
        current: str | None = None
        if target.is_file():
            # newline="" keeps the file's own line endings intact.
            with open(target, encoding="utf-8", newline="") as handle:
                current = handle.read()
        new_text, rung = _apply_search_replace(current, change.body, change.path)
        target.parent.mkdir(parents=True, exist_ok=True)
        with open(target, "w", encoding="utf-8", newline="") as handle:
            handle.write(new_text)
        if rung is not None:
            logger.warning(
                "apply_file_change: EDIT for %r applied only with %s",
                change.path,
                rung,
            )
        return rung

    strict_error: str | None = None
    for rung, flags in _apply_ladder():
        if rung == "context-0" and not _every_hunk_has_a_preimage(change.body):
            # With no context, a hunk that only ADDS lines is anchored by
            # nothing but its line number, so git will happily put it
            # wherever the (often wrong) header says. Seen live: an
            # `import os` meant for the top of a test module landed as its
            # last line, CI green, diff unmergeable. A hunk that removes or
            # replaces lines still has a preimage that must match, so -C0
            # stays available for those.
            continue
        check_result = _run_git(
            repo_path, "apply", "--check", *flags, "-",
            check=False, input_text=change.body,
        )
        if check_result.returncode != 0:
            if strict_error is None:
                strict_error = check_result.stderr.strip()
            continue
        _run_git(repo_path, "apply", *flags, "-", input_text=change.body)
        if rung != "strict":
            logger.warning(
                "apply_file_change: diff for %r applied only with %s (%s)",
                change.path,
                rung,
                " ".join(flags),
            )
            return rung
        return None
    raise MalformedOutputError(
        f"diff for {change.path!r} does not apply: {strict_error}"
    )


def get_working_diff(
    repo_path: str,
    declared_files: list[str] | None = None,
    *,
    stage_all: bool = False,
) -> str:
    """Stage changes (including new/deleted files) and return the
    resulting diff against HEAD - the patch a passing attempt hands back
    to the caller for commit-and-push.

    When ``declared_files`` is given, only those paths are staged, so a
    stray untracked/modified file already sitting in the working tree
    (left over from a previous attempt, a build artifact, etc.) is never
    swept into the diff. Falls back to staging everything when no paths
    are declared.

    ``stage_all=True`` stages every non-ignored change regardless, and says
    so at the call site: an advisory-scope coder (see
    :func:`run_advisory_attempt`) may edit files outside its declared list,
    and those edits must reach the diff. It is only safe on a tree that was
    reset to the attempt's base commit before the coder ran, which is what
    keeps leftovers out instead of the path filter. Passing both - even an
    empty ``declared_files`` list - is a contradiction and raises
    ``ValueError``.
    """
    if stage_all and declared_files is not None:
        raise ValueError("get_working_diff: pass declared_files or stage_all, not both")
    if stage_all:
        _run_git(repo_path, "add", "-A")
    elif declared_files:
        _run_git(repo_path, "add", "-A", "--", *declared_files)
    else:
        _run_git(repo_path, "add", "-A")
    return _run_git(repo_path, "diff", "--cached", "HEAD", check=False).stdout


def _clone_url(repo: str) -> str:
    """HTTPS URL to fresh-clone ``repo`` from (``owner/name`` format)."""
    return f"https://github.com/{repo}.git"


def _is_git_checkout(path: Path) -> bool:
    """True when ``path`` is inside a git working tree."""
    result = _run_git(str(path), "rev-parse", "--git-dir", check=False)
    return result.returncode == 0


def _redact_url(url: str) -> str:
    """``url`` with any userinfo removed, safe to put in a message.

    A remote can carry a token (``https://x-access-token:TOKEN@...``) and
    these strings reach stderr and log files, so the credential is
    stripped before the URL is ever shown.
    """
    # Two shapes carry userinfo: a URL (scheme://user:pass@host/...) and
    # scp-style (user:pass@host:owner/name). Both are redacted; the
    # lookbehind on the first keeps an "@" later in the path - a ref like
    # name@v2 - untouched.
    url = re.sub(r"(?<=://)[^/@]*@", "***@", url)
    if "://" not in url:
        url = re.sub(r"^[^/@]*@", "***@", url)
    return url


def _repo_identity(url: str) -> str | None:
    """The ``host/owner/name`` a git remote URL points at, lowercased.

    Compares *identity*, not URL spelling (#178). All of these name the
    same repository and must compare equal, because a pre-cloned
    checkout - the documented way to use a private repo, see
    :func:`ensure_base_clone` - is very often an SSH one::

        https://github.com/owner/name.git
        https://github.com/owner/name
        git@github.com:owner/name.git
        ssh://git@github.com/owner/name.git
        https://x-access-token:TOKEN@github.com/owner/name.git

    The host is part of the identity: ``gitlab.com/owner/name`` is not
    ``github.com/owner/name``, and this package only ever clones from
    github.com.

    Returns None for anything unrecognised - a bare local path, a
    ``file://`` URL, a URL with no host - which callers treat as "cannot
    verify" rather than as a mismatch. Refusing to run against a checkout
    whose remote we merely failed to parse would be worse than the stale
    checkout this guards against.
    """
    url = url.strip()
    if not url:
        return None
    # scp-style SSH: [user@]host:owner/name(.git) - no "//", which is what
    # separates it from a URL, and a path that is relative and has exactly
    # one "/" in it. The narrow path is what keeps "C:\dir\repo" and
    # "example.com:8080" from being read as a host and a repository at all
    # (both used to match and then fail the segment count instead).
    # The host needs at least two characters: a one-character "host" is a
    # Windows drive letter, and "C:repo/sub" is a drive-relative path, not
    # a remote. (C:/repo and C:\repo are already excluded by the path
    # having to be relative.)
    scp = re.match(
        r"^(?:[^/@]+@)?(?P<host>[^/:]{2,}):(?P<path>[^/][^:]*/[^/:]+)$", url
    )
    if scp and "//" not in url:
        host, path = scp.group("host"), scp.group("path")
    else:
        match = re.match(
            r"^[a-zA-Z][a-zA-Z0-9+.-]*://(?P<host>[^/]*)/(?P<path>.+)$", url
        )
        if not match:
            return None
        host, path = match.group("host"), match.group("path")
        # An authority-less URL (file:///srv/repo) has consumed the path's
        # own leading slash as the host separator, so what looks like an
        # owner is really a directory. Not a repository we can identify.
        if not host:
            return None
    # Strip any userinfo and port from the host, then fold the aliases so
    # an ssh.github.com remote is not mistaken for a different forge.
    host = host.rsplit("@", 1)[-1].split(":", 1)[0].lower()
    host = _HOST_ALIASES.get(host, host)
    path = path.strip("/")
    if path.endswith(".git"):
        path = path[: -len(".git")]
    parts = [part for part in path.split("/") if part]
    if not host or len(parts) != 2:
        return None
    return f"{host}/{parts[0]}/{parts[1]}".lower()


def _check_existing_remote(path: Path, repo: str) -> None:
    """Warn or raise when an existing checkout is a different repository.

    A misconfigured WORKSPACE_ROOT pointing at another project would
    otherwise be silent: the pass would refresh, dispatch, and commit
    against the wrong codebase (#178). ``refresh_to_main`` hard-resets to
    that origin's ``main``, so the wrong origin means the wrong code, not
    merely the wrong label.

    Only an unambiguous mismatch - both sides parsed, both naming a
    repository, and the two differing - raises. A missing ``origin``, a
    git failure, or an unparseable URL warns and continues, so an unusual
    but working setup is never blocked by this check. Set
    ``WORM_SKIP_REMOTE_CHECK=1`` to downgrade even a real mismatch to a
    warning, for a deliberate fork-origin setup.
    """
    try:
        result = _run_git(str(path), "remote", "get-url", "origin", check=False)
    except WorkspaceError as exc:
        # _run_git raises on timeout; this check must never be the thing
        # that stops a pass, so it fails open like every branch below.
        logger.warning(
            "Could not read origin's URL in %s (%s); skipping the "
            "base-clone repository check",
            path,
            exc,
        )
        return
    if result.returncode != 0:
        logger.warning(
            "Could not read origin's URL in %s (%s); skipping the "
            "base-clone repository check",
            path,
            result.stderr.strip() or f"git exited {result.returncode}",
        )
        return
    actual_url = result.stdout.strip()
    actual = _repo_identity(actual_url)
    # Derived from `repo` ("owner/name") directly rather than from
    # _clone_url: this check never clones, and going through the clone
    # helper would tie a read-only guard to the fresh-clone path.
    expected_parts = [part for part in repo.strip().strip("/").split("/") if part]
    expected = (
        f"{GITHUB_HOST}/{expected_parts[0]}/{expected_parts[1]}".lower()
        if len(expected_parts) == 2
        else None
    )
    if actual is None or expected is None:
        # A local mirror or a bare path is a normal setup we simply cannot
        # identify — debug, not warning, so it does not shout every pass.
        logger.debug(
            "Could not interpret origin's URL %r in %s; skipping the "
            "base-clone repository check",
            _redact_url(actual_url),
            path,
        )
        return
    if actual == expected:
        return
    message = (
        f"WORKSPACE_ROOT {str(path)!r} is a checkout of {actual!r}, not "
        f"{expected!r} (origin is {_redact_url(actual_url)!r})"
    )
    if os.environ.get(SKIP_REMOTE_CHECK_ENV) == "1":
        logger.warning("%s; continuing because %s=1", message, SKIP_REMOTE_CHECK_ENV)
        return
    raise WorkspaceError(
        f"{message} - refusing to run against the wrong repository. Point "
        "WORKSPACE_ROOT at a clone of the scheduled repo; if this is a "
        "deliberate fork setup, either set origin to the upstream and push "
        f"via a second remote, or set {SKIP_REMOTE_CHECK_ENV}=1 to allow it."
    )


def _discard_checkout(path: Path) -> None:
    """Move ``path`` out of the way and best-effort delete it, for `fresh`.

    Renamed aside first, then removed: the rename is a single filesystem
    operation, so from the caller's perspective ``path`` either still has
    its old, intact checkout (rename failed - the checkout is untouched
    and the caller should not proceed with a clone into an occupied path)
    or ``path`` is already free for a fresh clone, full stop. A `shutil.
    rmtree` failure on the *renamed* copy (a read-only object file, a
    stale NFS handle, ...) is logged and otherwise ignored, never raised:
    the whole point of `fresh` is to reliably reach a usable checkout, and
    an orphaned, harmlessly-named leftover directory is a far smaller
    problem than the alternative — `rmtree` dying midway through the
    checkout actually being reused would leave a non-empty, non-git
    directory at ``path`` itself, which every future run (fresh or not)
    then permanently refuses to touch (the exact "wedged" state `fresh`
    exists to get out of).
    """
    stale = path.with_name(f"{path.name}.stale-{os.getpid()}")
    try:
        path.rename(stale)
    except OSError as exc:
        raise WorkspaceError(
            f"fresh=True could not move aside the existing checkout at "
            f"{str(path)!r} before re-cloning: {exc}"
        ) from exc
    try:
        shutil.rmtree(stale)
    except OSError as exc:
        logger.warning(
            "fresh=True: moved the old checkout at %s aside to %s but "
            "could not fully delete it (%s) — remove it manually; a fresh "
            "clone is proceeding at %s regardless",
            path,
            stale,
            exc,
            path,
        )


def ensure_base_clone(repo_path: str, repo: str, *, fresh: bool = False) -> str:
    """Ensure ``repo_path`` (WORKSPACE_ROOT) is a usable git checkout of ``repo``.

    The base clone every issue's worktree is created from. Reused as-is
    when it is already a git checkout; fresh-cloned from
    ``https://github.com/<repo>.git`` when the path is missing or empty;
    never touched when it exists, is non-empty, and is not a git checkout
    — that is user data, so a ``WorkspaceError`` is raised and the caller
    should skip rather than clobber it. No fetch/pull of an existing
    checkout: the worker's own ``commit-and-push`` is what advances it.

    ``fresh=True`` forces a re-clone even when ``repo_path`` is already a
    usable git checkout — the existing checkout is deleted first, then
    the normal missing-path clone path below runs. It never widens what
    counts as safe to delete beyond a checkout genuinely rooted at
    ``repo_path`` itself: an existing non-empty *non*-git directory still
    raises rather than being removed, exactly as without ``fresh`` (that
    guard is about not clobbering unrelated user data, which ``fresh`` —
    a way to discard a stale *clone* — has no bearing on); and the
    same-repo check (below) still runs before anything is deleted, so
    ``fresh`` cannot silently discard a checkout of the *wrong* repository
    — it still raises, exactly as without ``fresh``.

    A reused checkout is checked against ``repo`` first (#178): if its
    ``origin`` names a different repository, that is a misconfigured
    WORKSPACE_ROOT and raises rather than silently dispatching against
    the wrong codebase. The comparison is on the ``owner/name`` identity,
    not the URL's spelling, so SSH and HTTPS remotes of the same repo are
    equivalent; an origin that cannot be read or parsed warns and is
    allowed through.

    Private repositories (#177)
        Fresh cloning is **HTTPS without credentials** — :func:`_clone_url`
        builds ``https://github.com/<owner>/<name>.git`` and nothing adds
        a token. Git may still satisfy that from the host's own
        configuration (a credential helper, ``gh auth setup-git``, an
        ``insteadOf`` rewrite to SSH), so private HTTPS cloning does work
        on a machine set up that way; without one, the clone fails to
        authenticate.

        The supported route is to create the checkout yourself and point
        WORKSPACE_ROOT at it; an existing checkout is reused as-is, by
        any protocol::

            git clone git@github.com:owner/private-repo.git /srv/worm/private-repo
            # .env
            WORKSPACE_ROOT=/srv/worm/private-repo

        Whatever credentials that clone was made with (an SSH key, a
        stored HTTPS token, a credential helper) are what the later
        ``git fetch``/``push`` use, since they run in that checkout.

    Args:
        repo_path: The base-clone path (WORKSPACE_ROOT).
        repo: Repository in "owner/name" format.
        fresh: Delete an existing git checkout at ``repo_path`` first and
            re-clone, instead of reusing it as-is. Has no effect when the
            path is already missing/empty (there is nothing to discard).

    Returns:
        The base-clone path (in the same relative/absolute form it was
        given).

    Raises:
        WorkspaceError when the path cannot be made a usable checkout — a
        non-empty non-git directory, a non-directory path, a checkout of
        a different repository, a failed clone, or (``fresh=True`` only)
        a checkout that could not be moved aside to be discarded. The
        caller should skip the pass with nothing attempted.
    """
    path = Path(repo_path)
    if path.exists():
        if not path.is_dir():
            raise WorkspaceError(
                f"WORKSPACE_ROOT {repo_path!r} is not a directory"
            )
        if _is_git_checkout(path):
            # Run regardless of `fresh`: this is the guard against
            # discarding the *wrong* repository, which matters exactly as
            # much when about to delete it as when about to reuse it.
            _check_existing_remote(path, repo)
            if not fresh:
                return str(path)
            if not (path / ".git").exists():
                # _is_git_checkout only proves `path` is somewhere *inside*
                # a git working tree (it runs `git rev-parse` from `path`,
                # which walks up to find one) — not that `path` is that
                # tree's own root. Without this check, fresh=True on a
                # WORKSPACE_ROOT nested inside an unrelated checkout would
                # delete that surrounding repo's working tree, entirely
                # unrelated to whatever `_check_existing_remote` just
                # approved. (A linked worktree does have its own `.git`
                # — a file, not a directory, pointing back at the main
                # checkout's git-dir — so this check passes it through;
                # deleting it only orphans its worktree registration,
                # never touches the main checkout.)
                raise WorkspaceError(
                    f"WORKSPACE_ROOT {repo_path!r} is inside a git working "
                    "tree but is not that tree's own root (no .git directly "
                    "in it) — refusing to delete it with fresh=True; point "
                    "WORKSPACE_ROOT at the checkout's own top-level directory"
                )
            _discard_checkout(path)
        elif any(path.iterdir()):
            raise WorkspaceError(
                f"WORKSPACE_ROOT {repo_path!r} exists, is non-empty, and is "
                "not a git checkout — refusing to touch it; point "
                "WORKSPACE_ROOT at a clone or at a missing/empty directory"
            )
    # Missing (or an empty directory): fresh-clone it. The destination is
    # absolute so it resolves against the process cwd, not the clone
    # subprocess's cwd (the parent directory).
    clone_url = _clone_url(repo)
    path.parent.mkdir(parents=True, exist_ok=True)
    # _run_git inherits stdin, so a repo needing credentials git cannot
    # supply non-interactively would sit on a username prompt until
    # CLONE_TIMEOUT (ten minutes). Fail fast instead, so the private-repo
    # case surfaces as an auth error the caller can act on (#177).
    # Set explicitly rather than left to _non_interactive_env's default
    # (#58): that one steps aside on a TTY so an interactive run can be
    # prompted, but a ten-minute stall is too long to offer even there.
    result = _run_git(
        str(path.parent),
        "clone",
        clone_url,
        str(path.absolute()),
        check=False,
        timeout=CLONE_TIMEOUT,
        env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
    )
    if result.returncode != 0:
        raise WorkspaceError(
            f"git clone {clone_url} -> {repo_path} failed: "
            f"{result.stderr.strip()}"
        )
    return str(path)


# What issue-worm itself ever writes into a managed workspace unprompted:
# history.py's and usage_metering.py's ".issue-worm/*.jsonl" bookkeeping,
# and ".env" for anyone following the provider-credentials pattern
# documented in ensure_base_clone above. Mirrors this project's own
# .gitignore for the same two paths.
WORM_GITIGNORE_PATTERNS = ("/.issue-worm/", ".env")


def ensure_gitignored(
    repo_path: str, patterns: tuple[str, ...] = WORM_GITIGNORE_PATTERNS
) -> None:
    """Append any of `patterns` missing from `repo_path`'s local, untracked
    ``.git/info/exclude`` — never the tracked ``.gitignore``.

    A repo-in-place WORKSPACE_ROOT — the private-repo setup documented in
    :func:`ensure_base_clone`, and the Scheduler's own default of "." — is
    very often the user's real working checkout, not a throwaway clone.
    Left unignored, the bookkeeping files issue-worm writes there show up
    as untracked changes on every later pass, and ``_workspace_is_dirty``
    has the Scheduler skip the pass every single time: a self-inflicted
    lockout that previously took a manual .gitignore edit to clear.

    An earlier version of this function wrote to the tracked ``.gitignore``
    instead. That edit was itself an uncommitted change, so it was wiped
    the moment :func:`refresh_to_main`'s ``git reset --hard`` ran — turning
    the very lockout this function exists to prevent into one that
    reappears on the *second* dispatch of every pass, forever, because
    ``ensure_gitignored`` only runs once per pass while the bookkeeping
    files it's supposed to hide get rewritten after every dispatch.
    ``.git/info/exclude`` is local to this checkout and untouched by
    ``git reset``/``checkout``/``clean``, and — being untracked itself —
    never shows up in ``git status`` for :func:`_workspace_is_dirty` to
    trip on, so nothing needs to commit it.

    Best-effort: resolved via ``git rev-parse --git-path info/exclude`` so
    it works whether ``.git`` is an ordinary directory or (in a worktree)
    a file pointing elsewhere. If ``repo_path`` isn't a git checkout at all
    (or the exclude file can't be read/written), this is logged and left
    alone rather than failing the caller's pass over it.
    """
    result = _run_git(
        repo_path, "rev-parse", "--git-path", "info/exclude", check=False
    )
    if result.returncode != 0:
        logger.warning(
            "Could not resolve .git/info/exclude under %s (%s); leaving "
            "it untouched",
            repo_path,
            result.stderr.strip(),
        )
        return
    exclude_path = Path(repo_path) / result.stdout.strip()
    try:
        exclude_path.parent.mkdir(parents=True, exist_ok=True)
        existing = (
            exclude_path.read_text(encoding="utf-8") if exclude_path.exists() else ""
        )
    except OSError as exc:
        logger.warning(
            "Could not read %s (%s); leaving it untouched",
            exclude_path,
            exc,
        )
        return
    existing_lines = {line.strip() for line in existing.splitlines()}
    missing = [p for p in patterns if p not in existing_lines]
    if not missing:
        return
    prefix = "\n" if existing and not existing.endswith("\n") else ""
    try:
        with exclude_path.open("a", encoding="utf-8") as f:
            f.write(prefix + "\n".join(missing) + "\n")
    except OSError as exc:
        logger.warning(
            "Could not add %s to %s (%s); leaving it untouched",
            missing,
            exclude_path,
            exc,
        )


# --- CI-check subprocess environment (allowlist, fail closed) ---------------
#
# The target repository's test suite is arbitrary code. It must not see the
# tool's own configuration: not the API keys, endpoints and state directory
# that `config.load_config()` loads into os.environ from `.env`, and not
# whatever the parent shell exported either (a denylist that strips `.env`
# keys still leaks a `DEEPSEEK_API_KEY` exported in the user's profile, and
# leaks silently). Both worm repos have a test that asserts `.env` in the
# cwd sets DEEPSEEK_API_KEY; with the tool's real key in the inherited
# environment that test fails on every patch, correct or not, which is one
# of the two reasons no recorded run ever passed Verify (2026-09-12 probe).
#
# So the CI subprocess gets an ALLOWLIST: PATH and the handful of variables
# a process cannot run without on each platform, a throwaway HOME, the
# workspace itself first on PYTHONPATH (below), a fixed git identity, and the
# caller's explicit delta. Nothing else.
#
# PYTHONPATH=<workspace> is the other half of the same fix: a target whose
# tests do `from coder import ...` against flat top-level modules resolves
# them from site-packages when the package is installed there, so the
# Verifier was testing the INSTALLED copy and never the patch. CI gets this
# for free from `pip install -e .`; putting the checkout first on the path
# is the equivalent for a scratch clone.
_CI_ENV_PASSTHROUGH_POSIX = ("PATH", "LANG", "LC_ALL", "LC_CTYPE", "TZ")
_CI_ENV_PASSTHROUGH_WINDOWS = (
    "PATH",
    # Without SYSTEMROOT, Python itself cannot start on Windows (it is
    # needed to seed os.urandom), and git/ssl need SYSTEMDRIVE/WINDIR.
    "SYSTEMROOT",
    "SYSTEMDRIVE",
    "WINDIR",
    # cicaid's run-ci-checks executes each check with shell=True, which
    # needs COMSPEC; PATHEXT is how `pytest` resolves to `pytest.exe`.
    "COMSPEC",
    "PATHEXT",
    "PROGRAMDATA",
)
CI_GIT_IDENTITY_NAME = "issue-worm verifier"
CI_GIT_IDENTITY_EMAIL = "verifier@issue-worm.invalid"


def ci_check_env(
    repo_path: str,
    extra_env: dict[str, str] | None = None,
    *,
    home: str,
    venv_dir: str | None = None,
) -> dict[str, str]:
    """The full environment for the target repo's CI-check subprocess.

    Built up from nothing (see the module comment above), never down from
    ``os.environ``:

    - the platform pass-through set (PATH plus what the OS needs to run a
      process at all);
    - ``HOME`` (and ``USERPROFILE`` on Windows) pointing at ``home``, a
      throwaway directory the caller owns, so nothing under the user's real
      home - ``~/.gitconfig``, ``~/.issue-worm``, gh/ssh config - is visible,
      and ``TMPDIR``/``TEMP``/``TMP`` pointing there too (a deliberate
      semantic change from the target's own environment: a test suite that
      relies on its temp dir sharing a filesystem/volume with the repo, or
      being a tmpfs, sees a plain throwaway directory instead);
    - ``PYTHONPATH`` = ``repo_path`` (plus ``repo_path/src`` for a
      src-layout repo) and nothing else, so the checkout under test shadows
      any installed copy of the same modules - including a module the patch
      only just added, which an installed copy cannot have;
    - with ``venv_dir`` (the target's own verifier venv, see
      :func:`ensure_verifier_venv`), that venv's scripts directory first on
      ``PATH`` and ``VIRTUAL_ENV`` set, so ``pytest``/``python`` inside a
      check resolve to the target's own isolated install, not the tool's;
    - ``PYTHONIOENCODING=utf-8`` so the child's output decodes the way
      :func:`run_ci_checks` reads it, whatever the console codepage;
    - a fixed git author/committer identity, because a target's tests that
      commit in temporary repos would otherwise fail without ``~/.gitconfig``;
    - ``extra_env`` last, so a caller's explicit delta (the Scheduler's
      per-target endpoint/model vars, #159) wins over all of the above -
      including ``PYTHONPATH``, if a caller's delta sets it. That is
      intentional (the caller's delta always wins), not an oversight.

    ``extra_env`` is a delta, not a base environment: passing
    ``os.environ`` here reintroduces exactly the leak this exists to stop.
    A ``None`` value in ``extra_env`` is dropped rather than coerced to the
    string ``"None"``.
    """
    passthrough = (
        _CI_ENV_PASSTHROUGH_WINDOWS if os.name == "nt" else _CI_ENV_PASSTHROUGH_POSIX
    )
    env: dict[str, str] = {}
    for key in passthrough:
        value = os.environ.get(key)
        if value is not None:
            env[key] = value
    env["HOME"] = home
    if os.name == "nt":
        env["USERPROFILE"] = home
        env["TEMP"] = home
        env["TMP"] = home
    else:
        env["TMPDIR"] = home
    repo = Path(repo_path).resolve()
    python_path = [str(repo)]
    if (repo / "src").is_dir():
        python_path.append(str(repo / "src"))
    env["PYTHONPATH"] = os.pathsep.join(python_path)
    if venv_dir is not None:
        env["PATH"] = os.pathsep.join(
            part for part in (str(_venv_scripts_dir(venv_dir)), env.get("PATH")) if part
        )
        env["VIRTUAL_ENV"] = str(venv_dir)
    env["PYTHONIOENCODING"] = "utf-8"
    env["GIT_AUTHOR_NAME"] = CI_GIT_IDENTITY_NAME
    env["GIT_AUTHOR_EMAIL"] = CI_GIT_IDENTITY_EMAIL
    env["GIT_COMMITTER_NAME"] = CI_GIT_IDENTITY_NAME
    env["GIT_COMMITTER_EMAIL"] = CI_GIT_IDENTITY_EMAIL
    if extra_env:
        env.update({str(k): str(v) for k, v in extra_env.items() if v is not None})
    return env


def _reject_base_environment_kwarg(func_name: str, kwargs: dict) -> None:
    """`env=` (a full environment) is gone on purpose - a caller still
    spreading ``os.environ`` into it must fail loudly, not leak silently."""
    if "env" in kwargs:
        raise TypeError(
            f"{func_name}() no longer accepts env= (a full base environment); "
            "pass extra_env= (a delta on top of the allowlist) instead - see "
            "ci_check_env()."
        )
    if kwargs:
        raise TypeError(
            f"{func_name}() got unexpected keyword arguments: {sorted(kwargs)}"
        )


# run_ci_checks below runs the project's own test/lint suite (`cicaid
# run-ci-checks --all` by default). That suite never touches a brand-new
# GitHub Actions workflow step - so a diff that only adds/edits a
# `.github/workflows/*.yml` step whose own shell command is simply wrong
# (e.g. a grep pattern that doesn't match the repo's real file contents)
# sails through verification as a passing attempt: "CI passed" is true
# only because nothing ran the new step at all. The helpers below close
# that gap by extracting each `run:` step this diff touched - added
# wholesale, or an existing step whose body/shell was edited - from the
# diff's post-apply content and actually executing it against the repo
# before the attempt is accepted.
WORKFLOW_STEP_TIMEOUT = 120.0

_WORKFLOW_DIFF_PATH_RE = re.compile(r"^\.github/workflows/.+\.ya?ml$")
_RUN_BLOCK_RE = re.compile(r"^(\s*)(?:-\s*)?run:\s*[|>][+-]?\s*$")
_RUN_INLINE_RE = re.compile(r"^(\s*)(?:-\s*)?run:\s*(\S.*)$")
_STEP_START_RE = re.compile(r"^(\s*)-\s")
# `shell:` may be the step's first key, in which case it sits on the
# `- ` line itself - so it allows the same optional dash the two
# `run:` patterns above do. Without it such a step reads as having no
# `shell:` at all and its body runs under bash, which is the false
# rejection the unsupported-shell skip exists to avoid.
_SHELL_RE = re.compile(r"^\s*(?:-\s*)?shell:\s*(\S+)\s*$")

# Commands used to execute an extracted step body, keyed by its `shell:`
# value (GitHub Actions default, unset, is bash on Linux runners - the
# environment this pipeline actually targets). A shell with no entry here
# (pwsh, powershell, cmd, a custom `shell:` template, ...) is skipped
# rather than run under the wrong interpreter, which would just produce a
# false rejection unrelated to the step's own correctness.
_SHELL_COMMANDS: dict[str | None, list[str]] = {
    None: ["bash", "-eo", "pipefail", "-c"],
    "bash": ["bash", "-eo", "pipefail", "-c"],
    "sh": ["sh", "-e", "-c"],
    "python": [sys.executable, "-c"],
    "python3": [sys.executable, "-c"],
}

# A step body is only a fair test of *its own* correctness when this
# sandbox can supply everything it reads. Two things it cannot:
#
# `${{ ... }}`
#     GitHub Actions expands expressions before the body ever reaches a
#     shell. Handed to bash verbatim they are not merely unset, they are
#     a hard parse error (`${{ github.sha }}` -> "bad substitution"), so
#     the step fails for a reason that has nothing to do with what it
#     checks.
# an environment variable this sandbox does not define
#     the Actions runtime file vars (`$GITHUB_OUTPUT`, `$GITHUB_ENV`,
#     ...), and anything declared in a step/job/workflow `env:` block -
#     which may not even appear in the diff, so there is no reliable way
#     to reconstruct it. Unset, `echo x >> $GITHUB_OUTPUT` is an
#     ambiguous redirect and `grep -q "$WANTED" f` matches everything.
#
# Either way the honest answer is "not checkable here", so the step is
# skipped and noted, the same way an unsupported `shell:` is - a false
# rejection would send the Coder off fixing a step that was correct.
_ACTIONS_EXPRESSION_RE = re.compile(r"\$\{\{")
# `${VAR:-default}` and its `:=`/`:+` siblings (and the colon-less forms)
# supply their own fallback, so the step handles an unset name itself -
# the same reason a two-argument `os.environ.get` does not count as
# needing context. `${VAR:?msg}` is deliberately fatal when unset, so it
# still counts, as does a plain `$VAR` or `${VAR}`.
_VAR_REF_RE = re.compile(
    r"\$(?:\{(?P<braced>[A-Za-z_][A-Za-z0-9_]*)(?P<op>:?[-=+])?[^}]*\}"
    r"|(?P<bare>[A-Za-z_][A-Za-z0-9_]*)\b)"
)
# `FOO=bar` at the start of a line, whether bare, as an inline prefix to a
# command, or behind any of the declaration builtins (which may carry
# flags of their own, as in `declare -r FOO=bar`).
_VAR_ASSIGN_RE = re.compile(
    r"^\s*(?:(?:export|local|declare|readonly|typeset)\s+(?:-\S+\s+)*)?"
    r"([A-Za-z_][A-Za-z0-9_]*)=",
    re.MULTILINE,
)
_VAR_LOOP_RE = re.compile(r"\bfor\s+([A-Za-z_][A-Za-z0-9_]*)\s+in\b")
# `read` assigns every name after its options, not just the first. Options
# that take an argument (`-p "Enter: "`, `-d ''`) must not have that
# argument mistaken for one of the names.
_VAR_READ_RE = re.compile(
    r"""\bread\s+(?:-\S+\s+(?:"[^"]*"\s+|'[^']*'\s+)?)*"""
    r"""(?P<names>[A-Za-z_][A-Za-z0-9_]*(?:\s+[A-Za-z_][A-Za-z0-9_]*)*)"""
)

# The python equivalent of a `$VAR` reference: `os.environ["X"]`,
# `os.environ.get("X")` and `os.getenv("X")`. A trailing comma means the
# call supplies its own default, so an unset name is not a problem there.
_PY_ENV_READ_RE = re.compile(
    r"""os\.(?:environ\s*\[\s*|environ\s*\.\s*get\s*\(\s*|getenv\s*\(\s*)"""
    # A comma only supplies a default when something follows it:
    # `os.environ.get("X",)` is a trailing comma, which means no default.
    r"""(?P<q>['"])(?P<name>[A-Za-z_][A-Za-z0-9_]*)(?P=q)\s*(?P<default>,(?!\s*\)))?"""
)

# Names the shell itself provides, so a reference to one is not evidence
# the step needs Actions context we can't supply.
_SHELL_PROVIDED_VARS = frozenset(
    {
        "PWD", "OLDPWD", "IFS", "RANDOM", "LINENO", "SECONDS", "UID", "EUID",
        "PPID", "HOSTNAME", "HOSTTYPE", "OSTYPE", "MACHTYPE", "REPLY",
        "FUNCNAME", "SHLVL", "BASHPID", "BASH_VERSION", "BASH_SUBSHELL",
    }
)


def _unsupported_step_context(
    script: str, shell: str | None, env: dict[str, str]
) -> str | None:
    """Why ``script`` can't be judged in this sandbox, or ``None`` if it
    can - see the comment above :data:`_ACTIONS_EXPRESSION_RE`."""
    if _ACTIONS_EXPRESSION_RE.search(script):
        return "it uses a ${{ }} expression the Actions runtime would expand"
    if shell in ("python", "python3"):
        # `$VAR` isn't syntax in a python body, but a python step reads the
        # same Actions variables through `os.environ` - and unset,
        # `os.environ["GITHUB_OUTPUT"]` is a KeyError, so the step fails
        # for a reason that has nothing to do with what it checks. The
        # `.get`/`getenv` forms that pass a default handle absence
        # themselves, so they are not evidence of missing context.
        for match in _PY_ENV_READ_RE.finditer(script):
            name = match.group("name")
            if name not in env and not match.group("default"):
                return f"it reads os.environ[{name!r}], which this sandbox does not define"
        return None
    defined = (
        set(env)
        | _SHELL_PROVIDED_VARS
        | set(_VAR_ASSIGN_RE.findall(script))
        | set(_VAR_LOOP_RE.findall(script))
        | {
            name
            for match in _VAR_READ_RE.finditer(script)
            for name in match.group("names").split()
        }
    )
    missing = [
        name
        for match in _VAR_REF_RE.finditer(script)
        # A braced reference carrying a default operator supplies its own
        # value, so it is not evidence of missing context.
        if not match.group("op")
        for name in [match.group("braced") or match.group("bare")]
        if name not in defined
    ]
    if missing:
        return f"it reads ${missing[0]}, which this sandbox does not define"
    return None


def _iter_diff_files(diff_output: str) -> list[tuple[str, list[str]]]:
    """Split a unified diff (as produced by :func:`get_working_diff`) into
    ``(path, lines)`` per file, where ``lines`` are that file's hunk lines
    (including the leading +/-/space marker)."""
    files: list[tuple[str, list[str]]] = []
    current_path: str | None = None
    current_lines: list[str] = []
    for line in diff_output.splitlines():
        if line.startswith("diff --git "):
            if current_path is not None:
                files.append((current_path, current_lines))
            current_path, current_lines = None, []
        elif line.startswith("+++ b/"):
            current_path = line[len("+++ b/") :]
        elif current_path is not None:
            current_lines.append(line)
    if current_path is not None:
        files.append((current_path, current_lines))
    return files


def _post_apply_lines(hunk_lines: list[str]) -> list[tuple[str, bool]]:
    """``(text, touched)`` for a file's content as it reads *after* this
    diff applies: removed (``-``) lines are dropped, and each surviving
    line is tagged with whether this diff added it (vs. surrounding
    unchanged context the diff carries for readability)."""
    result: list[tuple[str, bool]] = []
    for line in hunk_lines:
        if line.startswith("+++") or line.startswith("---") or line.startswith("@@"):
            continue
        if line.startswith("+"):
            result.append((line[1:], True))
        elif line.startswith(" "):
            result.append((line[1:], False))
        # '-' (removed) and '\ No newline at end of file' lines: dropped.
    return result


def _step_shell(lines: list[tuple[str, bool]], run_index: int) -> str | None:
    """The `shell:` value declared on the same step as ``lines[run_index]``
    (the step's ``run:`` key), or ``None`` if it doesn't set one -
    ``shell:`` may appear before or after ``run:`` in the step mapping."""
    run_text = lines[run_index][0]
    run_indent = len(run_text) - len(run_text.lstrip())

    start = 0
    dash_indent = run_indent
    for k in range(run_index, -1, -1):
        text = lines[k][0]
        indent = len(text) - len(text.lstrip())
        if _STEP_START_RE.match(text) and indent <= run_indent:
            start, dash_indent = k, indent
            break

    end = len(lines)
    for k in range(start + 1, len(lines)):
        text = lines[k][0]
        indent = len(text) - len(text.lstrip())
        if _STEP_START_RE.match(text) and indent <= dash_indent:
            end = k
            break

    # Only the step's *own* keys count. A `shell:` nested under `with:`
    # belongs to the action being invoked, not to this step, and reading
    # it would skip a step that actually runs under the default shell.
    dash_text = lines[start][0]
    after_dash = dash_text.find("-", dash_indent) + 1
    key_indent = after_dash + len(dash_text[after_dash:]) - len(dash_text[after_dash:].lstrip())

    for k in range(start, end):
        text = lines[k][0]
        if k != start and len(text) - len(text.lstrip()) != key_indent:
            continue
        match = _SHELL_RE.match(text)
        if match:
            return match.group(1)
    return None


def _extract_new_workflow_run_scripts(
    diff_output: str,
) -> list[tuple[str, str, str | None]]:
    """``(path, script, shell)`` for each ``run:`` step this diff touched
    in a ``.github/workflows/*.yml`` file - added wholesale, or an
    existing step whose body or ``shell:`` this diff edited.

    A step is only included when at least one line of it (its ``run:``
    key, its body, or its ``shell:`` key) is a line this diff actually
    *adds* - an untouched step that merely sits near a real change (and so
    appears as surrounding context) is never re-executed.
    """
    scripts: list[tuple[str, str, str | None]] = []
    for path, hunk_lines in _iter_diff_files(diff_output):
        if not _WORKFLOW_DIFF_PATH_RE.match(path):
            continue
        lines = _post_apply_lines(hunk_lines)
        i = 0
        while i < len(lines):
            text, touched = lines[i]
            block_match = _RUN_BLOCK_RE.match(text)
            if block_match:
                # The block body is what is indented past the `run:` key -
                # which is not the same as past the start of the line when
                # `run:` is the step's first key, because the `- ` sits in
                # between. Measuring from the line start there treats the
                # step's own sibling keys (`env:`, `shell:`,
                # `working-directory:`, ...) as body lines and appends them
                # to the script, so a correct step fails on `env: command
                # not found` - a false rejection.
                indent = text.index("run:")
                step_touched = touched
                i += 1
                body_lines = []
                while i < len(lines):
                    btext, btouched = lines[i]
                    if btext.strip() == "" or len(btext) - len(btext.lstrip()) > indent:
                        body_lines.append(btext)
                        step_touched = step_touched or btouched
                        i += 1
                    else:
                        break
                script = textwrap.dedent("\n".join(body_lines)).strip("\n")
                if step_touched and script.strip():
                    shell = _step_shell(lines, i - len(body_lines) - 1)
                    scripts.append((path, script, shell))
                continue
            inline_match = _RUN_INLINE_RE.match(text)
            if inline_match and touched:
                shell = _step_shell(lines, i)
                scripts.append((path, inline_match.group(2), shell))
            i += 1
    return scripts


def _run_new_workflow_step_scripts(
    repo_path: str,
    diff_output: str,
    extra_env: dict[str, str] | None,
    timeout: float,
) -> tuple[bool, str]:
    """Execute every workflow ``run:`` step this diff touched, found in
    ``diff_output``, against ``repo_path``, in the same sandboxed
    environment :func:`run_ci_checks` uses.

    Returns ``(True, "")`` when there is nothing to check. A step's
    non-zero exit is reported the same way a failing test is - the
    Verifier treats it as a normal failed attempt, so the Coder sees the
    real failure output on the next revision instead of the check being
    silently trusted.

    A step this sandbox can't judge is noted but does not fail the
    attempt, because a false rejection would send the Coder off fixing a
    step that was correct. Two cases: a ``shell:`` this helper can't run
    (anything but bash/sh/python - see :data:`_SHELL_COMMANDS`), and a
    body that needs Actions runtime context - a ``${{ }}`` expression or
    an environment variable nothing here defines (see
    :func:`_unsupported_step_context`).
    """
    scripts = _extract_new_workflow_run_scripts(diff_output)
    if not scripts:
        return True, ""
    all_passed = True
    output_parts: list[str] = []
    with tempfile.TemporaryDirectory(
        prefix="issue-worm-ci-home-", ignore_cleanup_errors=True
    ) as home:
        env = _non_interactive_env(ci_check_env(repo_path, extra_env, home=home))
        for path, script, shell in scripts:
            command = _SHELL_COMMANDS.get(shell)
            if command is None:
                output_parts.append(
                    f"[{path}] skipped new/changed workflow step: "
                    f"unsupported shell {shell!r}"
                )
                continue
            unsupported = _unsupported_step_context(script, shell, env)
            if unsupported:
                output_parts.append(
                    f"[{path}] skipped new/changed workflow step: {unsupported}"
                )
                continue
            try:
                result = subprocess.run(
                    [*command, script],
                    cwd=repo_path,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    # A step is free to print whatever bytes it likes;
                    # a strict decode would raise UnicodeDecodeError out
                    # of here and kill run_revision_attempt instead of
                    # reporting the step as a normal failed attempt.
                    errors="replace",
                    env=env,
                    timeout=timeout,
                )
            except OSError as exc:
                all_passed = False
                output_parts.append(f"[{path}] failed to execute new workflow step: {exc}")
                continue
            except subprocess.TimeoutExpired:
                all_passed = False
                output_parts.append(f"[{path}] new workflow step timed out after {timeout}s")
                continue
            status = "passed" if result.returncode == 0 else "FAILED"
            output_parts.append(
                f"[{path}] new workflow step {status}:\n{result.stdout}{result.stderr}"
            )
            if result.returncode != 0:
                all_passed = False
    return all_passed, "\n".join(output_parts)


# --- the target's own verifier venv -----------------------------------------
#
# PYTHONPATH=<workspace> (ci_check_env) makes the patch shadow an installed
# copy, but the checks still ran in the TOOL's interpreter, with the tool's
# own site-packages: everything issue-worm itself depends on (cicaid, the
# pro extensions, their entry points) was importable in the target's test
# run whether the target declared it or not. A target whose tests assert on
# what is installed - cicaid's `test_cli.py` checks the commands it
# discovers "with nothing installed" - failed there on every patch, correct
# or not; a target missing a declared test dependency the tool happens to
# carry passed when CI would not. CI has neither problem: it installs the
# target alone, `pip install -e ".[test]"`, into a fresh environment.
#
# ensure_verifier_venv() is that, once per workspace: an isolated venv (no
# system site-packages) holding the target installed editable plus its test
# extras. It lives outside the checkout - reset_to_commit's `git clean -fd`
# would delete it between attempts - and is rebuilt only when a dependency
# manifest changes. Editable, so each attempt's patch is live without a
# reinstall. One scheduler worker per workspace (#301), so no locking.

VERIFIER_VENV_ENV = "ISSUE_WORM_VERIFIER_VENV"
VERIFIER_VENV_DIR_ENV = "ISSUE_WORM_VERIFIER_VENV_DIR"
VERIFIER_VENV_SETUP_TIMEOUT = 900.0
_VERIFIER_VENV_DISABLED = ("0", "false", "no", "off")
_VERIFIER_VENV_STAMP = "issue-worm-deps.sha256"
# Optional-dependency groups CI conventionally installs for a test run, in
# the order they are requested.
_TEST_EXTRAS = ("test", "tests", "testing", "dev")
_PROJECT_MANIFESTS = ("pyproject.toml", "setup.py", "setup.cfg")
# Only the base and test/dev requirement files - not every
# requirements-*.txt, some of which pull whole optional stacks (video,
# automation) no test run needs.
_REQUIREMENTS_FILES = (
    "requirements.txt",
    "requirements-dev.txt",
    "requirements_dev.txt",
    "requirements-test.txt",
    "dev-requirements.txt",
    "test-requirements.txt",
)
# What pip itself needs from the real environment: a cache and config under
# the user's profile, proxies, and its own PIP_* settings. Still not the
# tool's API keys or state directory.
_VENV_SETUP_PASSTHROUGH = (
    "HOME",
    "USERPROFILE",
    "APPDATA",
    "LOCALAPPDATA",
    "XDG_CACHE_HOME",
    "XDG_CONFIG_HOME",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "NO_PROXY",
    "http_proxy",
    "https_proxy",
    "no_proxy",
)


def _venv_scripts_dir(venv_dir: str | Path) -> Path:
    return Path(venv_dir) / ("Scripts" if os.name == "nt" else "bin")


def _venv_python(venv_dir: str | Path) -> Path:
    return _venv_scripts_dir(venv_dir) / ("python.exe" if os.name == "nt" else "python")


def _is_installable_project(repo: Path) -> bool:
    if (repo / "setup.py").is_file() or (repo / "setup.cfg").is_file():
        return True
    pyproject = repo / "pyproject.toml"
    if not pyproject.is_file():
        return False
    try:
        data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return False
    # A pyproject.toml holding only [tool.*] config is not a package.
    return "project" in data or "build-system" in data


def _test_extras(repo: Path) -> list[str]:
    try:
        data = tomllib.loads((repo / "pyproject.toml").read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return []
    declared = (data.get("project") or {}).get("optional-dependencies") or {}
    return [name for name in _TEST_EXTRAS if name in declared]


def verifier_venv_install_args(repo_path: str) -> list[list[str]]:
    """The ``pip`` argument lists that install ``repo_path`` for its tests.

    An installable project is installed editable with whichever of the
    conventional test extras (:data:`_TEST_EXTRAS`) it declares; its base
    and test requirement files, if any, are installed too. Empty when the
    repo is not a Python project at all - :func:`ensure_verifier_venv`
    then builds nothing.
    """
    repo = Path(repo_path).resolve()
    args: list[list[str]] = []
    if _is_installable_project(repo):
        extras = _test_extras(repo)
        args.append(["install", "-e", "." + (f"[{','.join(extras)}]" if extras else "")])
    requirements = [name for name in _REQUIREMENTS_FILES if (repo / name).is_file()]
    if requirements:
        requirement_args = ["install"]
        for name in requirements:
            requirement_args += ["-r", name]
        args.append(requirement_args)
    return args


def verifier_venv_dir(repo_path: str) -> Path:
    """Where :func:`ensure_verifier_venv` keeps ``repo_path``'s venv.

    Under ``ISSUE_WORM_VERIFIER_VENV_DIR`` if set, else
    ``~/.issue-worm/verifier-venvs``; one directory per checkout path.
    """
    repo = Path(repo_path).resolve()
    root = os.environ.get(VERIFIER_VENV_DIR_ENV) or str(
        Path.home() / ".issue-worm" / "verifier-venvs"
    )
    key = hashlib.sha1(str(repo).lower().encode("utf-8")).hexdigest()[:10]
    return Path(root) / f"{repo.name}-{key}"


def _dependency_digest(repo: Path, install_args: list[list[str]]) -> str:
    digest = hashlib.sha256()
    digest.update(sys.version.encode("utf-8"))
    digest.update(repr(install_args).encode("utf-8"))
    for name in (*_PROJECT_MANIFESTS, *_REQUIREMENTS_FILES):
        path = repo / name
        if path.is_file():
            digest.update(name.encode("utf-8"))
            digest.update(path.read_bytes())
    return digest.hexdigest()


def _venv_setup_env() -> dict[str, str]:
    passthrough = (
        _CI_ENV_PASSTHROUGH_WINDOWS if os.name == "nt" else _CI_ENV_PASSTHROUGH_POSIX
    )
    env = {
        key: os.environ[key]
        for key in (*passthrough, *_VENV_SETUP_PASSTHROUGH)
        if key in os.environ
    }
    env.update({key: value for key, value in os.environ.items() if key.startswith("PIP_")})
    env["PYTHONIOENCODING"] = "utf-8"
    return env


def _run_venv_setup_step(command: list[str], cwd: Path, env: dict[str, str]) -> None:
    try:
        result = subprocess.run(
            command,
            cwd=cwd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
            timeout=VERIFIER_VENV_SETUP_TIMEOUT,
        )
    except OSError as exc:
        raise WorkspaceError(f"{' '.join(command)} could not run: {exc}") from exc
    except subprocess.TimeoutExpired as exc:
        raise WorkspaceError(
            f"{' '.join(command)} timed out after {VERIFIER_VENV_SETUP_TIMEOUT}s"
        ) from exc
    if result.returncode != 0:
        output = (result.stdout + result.stderr).strip()
        raise WorkspaceError(
            f"{' '.join(command)} exited {result.returncode}: {output[-2000:]}"
        )


def ensure_verifier_venv(repo_path: str) -> Path | None:
    """The target's own isolated venv for its CI checks, built if needed.

    Returns None - no venv, the checks run in this process's interpreter
    exactly as before - when ``ISSUE_WORM_VERIFIER_VENV`` is set to
    ``0``/``false``/``no``/``off``, or when ``repo_path`` is not a Python
    project (:func:`verifier_venv_install_args` is empty).

    Otherwise returns the venv directory, reusing the cached one when its
    stamp still matches the repo's dependency manifests and this Python,
    and rebuilding it from scratch when not (a stale pin must not linger).
    Raises :class:`WorkspaceError` if creating it or installing into it
    fails; :func:`run_ci_checks` falls back to the tool's own environment
    in that case rather than failing the attempt.
    """
    if os.environ.get(VERIFIER_VENV_ENV, "").strip().lower() in _VERIFIER_VENV_DISABLED:
        return None
    install_args = verifier_venv_install_args(repo_path)
    if not install_args:
        return None

    repo = Path(repo_path).resolve()
    venv_dir = verifier_venv_dir(repo_path)
    stamp_path = venv_dir / _VERIFIER_VENV_STAMP
    digest = _dependency_digest(repo, install_args)
    try:
        if _venv_python(venv_dir).is_file() and stamp_path.read_text(encoding="utf-8") == digest:
            return venv_dir
    except OSError:
        pass

    logger.info("verifier venv: building %s for %s", venv_dir, repo)
    shutil.rmtree(venv_dir, ignore_errors=True)
    env = _venv_setup_env()
    _run_venv_setup_step([sys.executable, "-m", "venv", str(venv_dir)], repo, env)
    python = str(_venv_python(venv_dir))
    for args in install_args:
        _run_venv_setup_step(
            [python, "-m", "pip", "--disable-pip-version-check", "--no-input", *args],
            repo,
            env,
        )
    stamp_path.write_text(digest, encoding="utf-8")
    return venv_dir


def run_ci_checks(
    repo_path: str,
    command: list[str] | None = None,
    extra_env: dict[str, str] | None = None,
    timeout: float | None = None,
    **kwargs,
) -> tuple[bool, str]:
    """Run the configured CI-check command (default: `cicaid run-ci-checks
    --all`, which reads .cicaid-checks.toml - see design.md's "Relationship
    to cicaid") and capture pass/fail plus combined output.

    The subprocess never inherits this process's environment: it runs in
    the allowlisted environment :func:`ci_check_env` builds, with a fresh
    throwaway HOME for the duration of the call. ``extra_env`` is the
    caller's explicit delta on top of that (the Scheduler passes its
    target's endpoint/model vars, #159); it is NOT a base environment, and
    a caller must not spread ``os.environ`` into it.

    For a Python target the checks run inside the target's own isolated
    venv (:func:`ensure_verifier_venv`), the way CI installs it; if that
    venv cannot be prepared they run in this process's interpreter as
    before, with a note saying so prefixed to the returned output.

    ``timeout`` defaults to :data:`DEFAULT_CI_TIMEOUT` (a real test suite
    takes minutes; the bound exists so a hung CI command surfaces as a
    :class:`WorkspaceError` instead of blocking the orchestrator forever,
    #45).

    Returns (passed, output) rather than raising, so a missing/failing CI
    tool is reported to the caller (and, on the next revision, the
    Analyser) the same way a real test failure is. A command that exceeds
    its timeout is different - it raises :class:`WorkspaceError` so the
    stall is not mistaken for a test failure.
    """
    _reject_base_environment_kwarg("run_ci_checks", kwargs)
    command = list(command) if command else list(DEFAULT_CI_COMMAND)
    effective_timeout = DEFAULT_CI_TIMEOUT if timeout is None else timeout

    note = ""
    try:
        venv_dir = ensure_verifier_venv(repo_path)
    except (WorkspaceError, OSError) as exc:
        # Degrade to the old behaviour rather than fail an attempt the patch
        # may well have passed: no network, a broken build backend, etc.
        logger.warning("verifier venv unavailable for %s: %s", repo_path, exc)
        note = (
            f"note: issue-worm could not prepare an isolated verifier venv "
            f"({exc}); these checks ran in issue-worm's own environment "
            f"instead, where its installed packages are visible.\n\n"
        )
        venv_dir = None
    if venv_dir is not None and os.path.basename(command[0]) == command[0]:
        # The runner (`cicaid run-ci-checks`) is issue-worm's tool, not the
        # target's: resolve it against this process's PATH before the
        # venv's scripts directory goes first, so a target that ships a
        # same-named script (cicaid itself) does not swap in the code under
        # test as its own verifier. The checks it spawns still see the venv.
        runner = shutil.which(command[0])
        if runner:
            command[0] = runner

    with tempfile.TemporaryDirectory(
        prefix="issue-worm-ci-home-", ignore_cleanup_errors=True
    ) as home:
        env = _non_interactive_env(
            ci_check_env(
                repo_path,
                extra_env,
                home=home,
                venv_dir=str(venv_dir) if venv_dir is not None else None,
            )
        )
        try:
            result = subprocess.run(
                command,
                cwd=repo_path,
                capture_output=True,
                text=True,
                encoding="utf-8",
                env=env,
                timeout=effective_timeout,
            )
        except OSError as exc:
            return False, f"{note}failed to run CI command {command}: {exc}"
        except subprocess.TimeoutExpired as exc:
            raise WorkspaceError(
                f"CI command {' '.join(command)} timed out after "
                f"{effective_timeout}s"
            ) from exc
    return result.returncode == 0, note + result.stdout + result.stderr


def run_revision_attempt(
    repo_path: str,
    coder_output: str,
    declared_files: list[str],
    start_commit: str | None = None,
    ci_command: list[str] | None = None,
    extra_env: dict[str, str] | None = None,
    **kwargs,
) -> WorkspaceResult:
    """Apply one bounded revision attempt and run CI checks, rolling back
    to `start_commit` on any failure or interruption.

    This is the unit orchestrator.py calls once per attempt in the Coder ->
    Verifier loop (see design.md): reset to a known-good commit, apply this
    attempt's diff/full-file output, run CI checks, and leave the repo
    clean again unless the attempt fully passed. ``extra_env`` is the
    caller's delta for the CI-check subprocess, which otherwise runs in
    the allowlisted environment :func:`ci_check_env` builds (see
    :func:`run_ci_checks`) - never in this process's own.

    Normally returns a :class:`WorkspaceResult` even on failure - but if
    the post-failure rollback to ``start_commit`` itself fails, a
    ``WorkspaceError`` propagates instead (see :class:`_RollbackGuard`):
    that leaves the workspace in an unknown state, which is worse than
    the failure being reported and must not be swallowed.
    """
    _reject_base_environment_kwarg("run_revision_attempt", kwargs)
    if start_commit is None:
        start_commit = get_current_commit(repo_path)

    reset_to_commit(repo_path, start_commit)

    with _RollbackGuard(repo_path, start_commit) as guard:
        try:
            changes = parse_coder_output(coder_output, declared_files)
        except MalformedOutputError as exc:
            return WorkspaceResult(
                success=False,
                error=f"{MALFORMED_OUTPUT_ERROR_PREFIX} {exc}",
                category=CATEGORY_OUTPUT_SHAPE,
            )

        recovery = [
            f"{change.path}: {change.recovery}" for change in changes if change.recovery
        ]
        try:
            for change in changes:
                rung = apply_file_change(repo_path, change)
                if rung is not None:
                    recovery.append(f"{change.path}: applied with {rung}")
        except MalformedOutputError as exc:
            return WorkspaceResult(
                success=False,
                error=f"{APPLY_FAILED_ERROR_PREFIX} {exc}",
                category=CATEGORY_OUTPUT_SHAPE,
                recovery=recovery,
            )

        # Stage the sanitized paths parse_coder_output actually applied,
        # not the raw declared_files - Triage's FILES: entries are often
        # decorated (`` `a.py` ``, `** a.py`) and would fail as a git
        # pathspec if handed to `git add` unsanitized.
        diff_output = get_working_diff(repo_path, [change.path for change in changes])

        passed, test_output = run_ci_checks(repo_path, ci_command, extra_env=extra_env)
        if not passed:
            return WorkspaceResult(
                success=False,
                test_output=test_output,
                diff_output=diff_output,
                error="CI checks failed",
                category=CATEGORY_TEST_FAILURE,
                recovery=recovery,
            )

        workflow_passed, workflow_output = _run_new_workflow_step_scripts(
            repo_path, diff_output, extra_env, WORKFLOW_STEP_TIMEOUT
        )
        if not workflow_passed:
            combined_output = "\n\n".join(part for part in (test_output, workflow_output) if part)
            return WorkspaceResult(
                success=False,
                test_output=combined_output,
                diff_output=diff_output,
                error="newly added GitHub Actions workflow step failed when run against the repo",
                category=CATEGORY_TEST_FAILURE,
                recovery=recovery,
            )

        guard.disarm()
        return WorkspaceResult(
            success=True,
            test_output=test_output,
            diff_output=diff_output,
            recovery=recovery,
        )


def run_advisory_attempt(
    repo_path: str,
    start_commit: str,
    ci_command: list[str] | None = None,
    extra_env: dict[str, str] | None = None,
) -> WorkspaceResult:
    """Verify one attempt by a coder that edited the workspace itself.

    The advisory-scope counterpart of :func:`run_revision_attempt`
    (leonarduk/issue-worm-pro#1917). An agentic coder's ``FILES:`` list is a
    hint, not a lock: it may touch other files, and those edits must reach
    the diff. So there is no coder output to parse or apply here - the edits
    are already on disk - and the diff stages every non-ignored change
    (``stage_all=True``) instead of only the declared paths.

    That is only sound because the CALLER resets the tree to
    ``start_commit`` with :func:`reset_to_commit` BEFORE the coder runs.
    This function cannot do it (the reset would wipe the edits it is here to
    verify), and the reset is what keeps a previous attempt's leftovers and
    stray build output out of the diff - the job the declared-path filter
    does on the locked path.

    If the coder committed its own work, HEAD is moved back to
    ``start_commit`` with ``git reset --soft`` first. That keeps the
    committed changes staged and leaves any uncommitted edits where they
    were, and the stage-everything step below picks both up, so the diff
    covers the whole attempt. CI then runs on the same tree that was staged.

    Rolls back to ``start_commit`` on any failure or interruption, exactly
    like :func:`run_revision_attempt` - including the no-changes result,
    which still hard-resets the tree - and a passing attempt's changes are
    left staged for commit-and-push.

    Unlike :func:`run_revision_attempt` and :func:`run_ci_checks` there is
    no ``**kwargs``: this function is new, so no legacy caller passes the
    retired ``env=`` and needs the tailored error; any unknown keyword,
    ``env=`` included, is Python's own ``TypeError``.
    """
    with _RollbackGuard(repo_path, start_commit) as guard:
        head = get_current_commit(repo_path)
        if head != start_commit:
            logger.info(
                "Advisory coder moved HEAD from %s to %s; folding its commits "
                "back into the index",
                start_commit,
                head,
            )
            _run_git(repo_path, "reset", "--soft", start_commit)

        diff_output = get_working_diff(repo_path, stage_all=True)
        if not diff_output.strip():
            return WorkspaceResult(
                success=False,
                error="advisory coder made no changes to the workspace",
                category=CATEGORY_OUTPUT_SHAPE,
            )

        passed, test_output = run_ci_checks(repo_path, ci_command, extra_env=extra_env)
        if not passed:
            return WorkspaceResult(
                success=False,
                test_output=test_output,
                diff_output=diff_output,
                error="CI checks failed",
                category=CATEGORY_TEST_FAILURE,
            )

        workflow_passed, workflow_output = _run_new_workflow_step_scripts(
            repo_path, diff_output, extra_env, WORKFLOW_STEP_TIMEOUT
        )
        if not workflow_passed:
            combined_output = "\n\n".join(part for part in (test_output, workflow_output) if part)
            return WorkspaceResult(
                success=False,
                test_output=combined_output,
                diff_output=diff_output,
                error="newly added GitHub Actions workflow step failed when run against the repo",
                category=CATEGORY_TEST_FAILURE,
            )

        guard.disarm()
        return WorkspaceResult(
            success=True,
            test_output=test_output,
            diff_output=diff_output,
        )
