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

import logging
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

logger = logging.getLogger(__name__)

# Coder output modes: a full-file write (MODE_FULL) or a `git apply`-able
# unified diff (MODE_DIFF). Defined here rather than in agents/coder.py
# since this module — not the Coder — decides how each mode is applied;
# agents/coder.py imports these back for the instructions it gives the LLM.
MODE_FULL = "FULL"
MODE_DIFF = "DIFF"

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
#   === MODE: FULL or DIFF ===
#   <content>
#   === END FILE ===
_FILE_SECTION_RE = re.compile(
    r"=== FILE: (?P<path>.+?) ===\r?\n"
    r"=== MODE: (?P<mode>FULL|DIFF) ===\r?\n"
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

# A unified diff's own syntax always starts with one of these; used to find
# where a MODE: DIFF section's diff body starts amid surrounding prose.
_DIFF_START_RE = re.compile(r"^(diff --git |--- )", re.MULTILINE)
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


@dataclass
class FileChange:
    """One file's worth of a parsed Coder response."""
    path: str
    mode: str  # MODE_FULL | MODE_DIFF
    body: str


@dataclass
class WorkspaceResult:
    """Result of applying one revision attempt and running CI checks."""
    success: bool
    test_output: str = ""
    diff_output: str = ""
    error: str | None = None


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


def _extract_diff(body: str) -> str | None:
    """Pull the unified-diff hunk out of a MODE: DIFF section's body.

    NativeCoder's prompt asks for "a unified diff ... with a brief
    explanation" (agents/coder.py), so the body can have prose before
    and/or after the actual diff. Finds where diff syntax starts, then
    stops at the first line that no longer looks diff-shaped.
    """
    match = _DIFF_START_RE.search(body)
    if not match:
        return None

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


def parse_coder_output(output: str, declared_files: list[str]) -> list[FileChange]:
    """Split a Coder response into per-file changes, rejecting anything
    that isn't safe to hand to git apply / a direct file write.

    Raises MalformedOutputError (not a generic exception) so callers can
    tell "the Coder's patch was never actually tried" apart from a real
    test failure - see design.md's "Malformed diffs".
    """
    if not output or not output.strip():
        raise MalformedOutputError("coder output is empty")

    matches = list(_FILE_SECTION_RE.finditer(output))
    if not matches:
        raise MalformedOutputError(
            "no '=== FILE: ... === / === MODE: ... === / === END FILE ===' "
            "sections found in coder output"
        )

    # Declared files are normalized the same way the FILE marker path is,
    # so a Coder that echoes back a decorated path (``** `x.py` **``) still
    # matches its declared, sanitized file.
    declared = {sanitize_file_path(f) for f in declared_files}
    declared.discard(None)
    changes: list[FileChange] = []
    seen_paths: set[str] = set()

    for match in matches:
        path = match.group("path").strip()
        mode = match.group("mode").strip()
        body = match.group("body").strip("\n")

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
        if path in seen_paths:
            raise MalformedOutputError(f"duplicate FILE section for {path!r}")
        seen_paths.add(path)

        if mode == MODE_DIFF:
            diff_text = _extract_diff(body)
            if diff_text is None:
                raise MalformedOutputError(
                    f"MODE: DIFF section for {path!r} contains no parseable unified diff"
                )
            body = diff_text
        else:
            # A full-file rewrite wrapped in a Markdown fence (```python /
            # bare ``` / ...) is not part of the file's real content, and
            # there's no verifier here (free-tier build, #401) to catch the
            # resulting SyntaxError the way issue-worm-pro's loop would -
            # strip it before writing, the same way MODE_DIFF already does
            # for its own fencing (issue #248).
            body = _strip_edge_fences(body)

        changes.append(FileChange(path=path, mode=mode, body=body))

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


def apply_file_change(repo_path: str, change: FileChange) -> None:
    """Apply one parsed file change: a direct write for MODE_FULL, or
    `git apply` (pre-checked with --check) for MODE_DIFF.

    Raises MalformedOutputError if a diff doesn't apply cleanly against the
    current tree - checked before the real apply so a bad patch can't
    partially land.
    """
    if change.mode == MODE_FULL:
        target = Path(repo_path) / change.path
        target.parent.mkdir(parents=True, exist_ok=True)
        content = change.body
        if content and not content.endswith("\n"):
            content += "\n"
        target.write_text(content, encoding="utf-8")
        return

    check_result = _run_git(
        repo_path, "apply", "--check", "--recount", "-",
        check=False, input_text=change.body,
    )
    if check_result.returncode != 0:
        raise MalformedOutputError(
            f"diff for {change.path!r} does not apply: {check_result.stderr.strip()}"
        )
    _run_git(repo_path, "apply", "--recount", "-", input_text=change.body)


def get_working_diff(repo_path: str, declared_files: list[str] | None = None) -> str:
    """Stage changes (including new/deleted files) and return the
    resulting diff against HEAD - the patch a passing attempt hands back
    to the caller for commit-and-push.

    When ``declared_files`` is given, only those paths are staged, so a
    stray untracked/modified file already sitting in the working tree
    (left over from a previous attempt, a build artifact, etc.) is never
    swept into the diff. Falls back to staging everything when no paths
    are declared.
    """
    if declared_files:
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


def run_ci_checks(
    repo_path: str,
    command: list[str] | None = None,
    env: dict[str, str] | None = None,
    timeout: float | None = None,
) -> tuple[bool, str]:
    """Run the configured CI-check command (default: `cicaid run-ci-checks
    --all`, which reads .cicaid-checks.toml - see design.md's "Relationship
    to cicaid") and capture pass/fail plus combined output.

    ``env`` is the subprocess's full environment (None = inherit the
    parent's, as before); the Scheduler passes its target's env explicitly
    so parallel workers never rely on a mutated ``os.environ`` (#159).
    ``timeout`` defaults to :data:`DEFAULT_CI_TIMEOUT` (a real test suite
    takes minutes; the bound exists so a hung CI command surfaces as a
    :class:`WorkspaceError` instead of blocking the orchestrator forever,
    #45).

    Returns (passed, output) rather than raising, so a missing/failing CI
    tool is reported to the caller (and, on the next revision, the
    Analyser) the same way a real test failure is. A command that exceeds
    its timeout is different — it raises :class:`WorkspaceError` so the
    stall is not mistaken for a test failure.
    """
    command = list(command) if command else list(DEFAULT_CI_COMMAND)
    effective_timeout = DEFAULT_CI_TIMEOUT if timeout is None else timeout
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
        return False, f"failed to run CI command {command}: {exc}"
    except subprocess.TimeoutExpired as exc:
        raise WorkspaceError(
            f"CI command {' '.join(command)} timed out after "
            f"{effective_timeout}s"
        ) from exc
    return result.returncode == 0, result.stdout + result.stderr


def run_revision_attempt(
    repo_path: str,
    coder_output: str,
    declared_files: list[str],
    start_commit: str | None = None,
    ci_command: list[str] | None = None,
    env: dict[str, str] | None = None,
) -> WorkspaceResult:
    """Apply one bounded revision attempt and run CI checks, rolling back
    to `start_commit` on any failure or interruption.

    This is the unit orchestrator.py calls once per attempt in the Coder ->
    Verifier loop (see design.md): reset to a known-good commit, apply this
    attempt's diff/full-file output, run CI checks, and leave the repo
    clean again unless the attempt fully passed. ``env`` is forwarded to
    the CI-check subprocess (see :func:`run_ci_checks`).

    Normally returns a :class:`WorkspaceResult` even on failure - but if
    the post-failure rollback to ``start_commit`` itself fails, a
    ``WorkspaceError`` propagates instead (see :class:`_RollbackGuard`):
    that leaves the workspace in an unknown state, which is worse than
    the failure being reported and must not be swallowed.
    """
    if start_commit is None:
        start_commit = get_current_commit(repo_path)

    reset_to_commit(repo_path, start_commit)

    with _RollbackGuard(repo_path, start_commit) as guard:
        try:
            changes = parse_coder_output(coder_output, declared_files)
        except MalformedOutputError as exc:
            return WorkspaceResult(success=False, error=f"{MALFORMED_OUTPUT_ERROR_PREFIX} {exc}")

        try:
            for change in changes:
                apply_file_change(repo_path, change)
        except MalformedOutputError as exc:
            return WorkspaceResult(success=False, error=f"{APPLY_FAILED_ERROR_PREFIX} {exc}")

        # Stage the sanitized paths parse_coder_output actually applied,
        # not the raw declared_files - Triage's FILES: entries are often
        # decorated (`` `a.py` ``, `** a.py`) and would fail as a git
        # pathspec if handed to `git add` unsanitized.
        diff_output = get_working_diff(repo_path, [change.path for change in changes])

        passed, test_output = run_ci_checks(repo_path, ci_command, env=env)
        if not passed:
            return WorkspaceResult(
                success=False,
                test_output=test_output,
                diff_output=diff_output,
                error="CI checks failed",
            )

        guard.disarm()
        return WorkspaceResult(success=True, test_output=test_output, diff_output=diff_output)
