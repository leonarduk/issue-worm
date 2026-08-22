"""Tests for workspace.py: reset-and-retry, diff/full-file apply, CI checks,
and rollback on failure or interruption."""

import logging
import subprocess
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from workspace import (
    DEFAULT_CI_TIMEOUT,
    DEFAULT_GIT_TIMEOUT,
    CLONE_TIMEOUT,
    FileChange,
    MalformedOutputError,
    WorkspaceError,
    _redact_url,
    _repo_identity,
    _run_git,
    apply_file_change,
    ensure_base_clone,
    get_current_commit,
    get_working_diff,
    parse_coder_output,
    refresh_to_main,
    reset_to_commit,
    run_ci_checks,
    run_revision_attempt,
    sanitize_file_path,
)


def _git(repo_path, *args, **kwargs):
    return subprocess.run(["git", *args], cwd=repo_path, capture_output=True, text=True, check=True, **kwargs)


@pytest.fixture
def repo():
    with tempfile.TemporaryDirectory() as tmpdir:
        _git(tmpdir, "init", "-q")
        _git(tmpdir, "config", "user.email", "test@example.com")
        _git(tmpdir, "config", "user.name", "Test")
        (Path(tmpdir) / "a.py").write_text("value = 1\n")
        _git(tmpdir, "add", "-A")
        _git(tmpdir, "commit", "-q", "-m", "init")
        yield tmpdir


def _full_file_output(path, content):
    return f"=== FILE: {path} ===\n=== MODE: FULL ===\n{content}\n=== END FILE ===\n"


def _diff_output(path, diff, explanation="This changes the value."):
    return f"=== FILE: {path} ===\n=== MODE: DIFF ===\n{diff}\n{explanation}\n=== END FILE ===\n"


def _make_diff(repo_path, old_content, new_content, path="a.py"):
    """Produce a real, applicable unified diff by writing new_content and
    diffing against the file's actual current (committed) content, then
    restoring that original content so the working tree is left clean."""
    target = Path(repo_path) / path
    original = target.read_text()
    target.write_text(new_content)
    result = subprocess.run(["git", "diff", "--", path], cwd=repo_path, capture_output=True, text=True)
    target.write_text(original)
    return result.stdout


_STALE_DIFF = """diff --git a/a.py b/a.py
index aaaaaaa..bbbbbbb 100644
--- a/a.py
+++ b/a.py
@@ -1 +1 @@
-value = 999
+value = 2
"""


# --- reset_to_commit / get_current_commit ---------------------------------


def test_get_current_commit_returns_head(repo):
    commit = get_current_commit(repo)
    assert len(commit) == 40


def test_reset_to_commit_discards_tracked_changes(repo):
    start = get_current_commit(repo)
    (Path(repo) / "a.py").write_text("value = 999\n")

    reset_to_commit(repo, start)

    assert (Path(repo) / "a.py").read_text() == "value = 1\n"


def test_reset_to_commit_removes_untracked_files(repo):
    start = get_current_commit(repo)
    (Path(repo) / "new_file.py").write_text("junk")

    reset_to_commit(repo, start)

    assert not (Path(repo) / "new_file.py").exists()


# --- refresh_to_main -------------------------------------------------------


def _with_origin(repo, tmpdir):
    """Give the repo a bare `origin` with a `main` branch, then return
    the origin path. The repo's own branch is renamed to main and pushed,
    so origin/main exists for refresh_to_main to reset against; the bare
    repo's HEAD is pointed at main so clones default to it."""
    origin_dir = str(Path(tmpdir) / "origin.git")
    _git(repo, "init", "--bare", "-q", origin_dir)
    _git(repo, "branch", "-M", "main")
    _git(repo, "remote", "add", "origin", origin_dir)
    _git(repo, "push", "-q", "origin", "main")
    _git(origin_dir, "symbolic-ref", "HEAD", "refs/heads/main")
    return origin_dir


def test_refresh_to_main_fetches_and_hard_resets_to_origin_main(repo, tmp_path):
    """Local commits on main are discarded: the checkout lands exactly on
    origin/main after the refresh (#301)."""
    _with_origin(repo, tmp_path)
    # A local commit that was never pushed: refresh must discard it.
    (Path(repo) / "a.py").write_text("value = 2\n")
    _git(repo, "commit", "-aqm", "local-only change")
    origin_head = _git(repo, "rev-parse", "origin/main").stdout.strip()

    refresh_to_main(repo)

    assert _git(repo, "rev-parse", "HEAD").stdout.strip() == origin_head
    assert _git(repo, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip() == "main"
    assert (Path(repo) / "a.py").read_text() == "value = 1\n"
    assert _git(repo, "status", "--porcelain").stdout.strip() == ""


def test_refresh_to_main_switches_back_to_main_from_fix_branch(repo, tmp_path):
    """A leftover fix branch (crashed run) does not survive the refresh."""
    _with_origin(repo, tmp_path)
    _git(repo, "checkout", "-q", "-b", "fix/issue-999")
    (Path(repo) / "a.py").write_text("value = 2\n")
    _git(repo, "commit", "-aqm", "fix work")

    refresh_to_main(repo)

    assert _git(repo, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip() == "main"
    assert (Path(repo) / "a.py").read_text() == "value = 1\n"


def test_refresh_to_main_picks_up_pushed_upstream_changes(repo, tmp_path):
    """The fetch is real: commits pushed to origin since the last refresh
    are pulled in."""
    origin_dir = _with_origin(repo, tmp_path)
    # Push a newer commit from a second checkout of the origin repo.
    upstream = str(Path(tmp_path) / "upstream")
    _git(repo, "clone", "-q", origin_dir, upstream)
    _git(upstream, "config", "user.email", "test@example.com")
    _git(upstream, "config", "user.name", "Test")
    (Path(upstream) / "a.py").write_text("value = 3\n")
    _git(upstream, "commit", "-aqm", "upstream change")
    _git(upstream, "push", "-q", "origin", "main")

    refresh_to_main(repo)

    assert (Path(repo) / "a.py").read_text() == "value = 3\n"


def test_refresh_to_main_without_origin_raises(repo, tmp_path):
    """A repo with no origin cannot be refreshed: WorkspaceError, and the
    caller skips the pass."""
    with pytest.raises(WorkspaceError):
        refresh_to_main(repo)


# --- parse_coder_output -----------------------------------------------------


def test_parse_full_file_section():
    output = _full_file_output("a.py", "value = 2")
    changes = parse_coder_output(output, ["a.py"])

    assert len(changes) == 1
    assert changes[0] == FileChange(path="a.py", mode="FULL", body="value = 2")


def test_parse_diff_section_extracts_diff_and_drops_explanation(repo):
    diff = _make_diff(repo, "value = 1\n", "value = 2\n")
    output = _diff_output("a.py", diff)

    changes = parse_coder_output(output, ["a.py"])

    assert len(changes) == 1
    assert changes[0].mode == "DIFF"
    assert "diff --git" in changes[0].body
    assert "This changes the value." not in changes[0].body


def test_parse_mixed_full_and_diff_sections(repo):
    diff = _make_diff(repo, "value = 1\n", "value = 2\n")
    output = _full_file_output("b.py", "x = 1") + "\n" + _diff_output("a.py", diff)

    changes = parse_coder_output(output, ["a.py", "b.py"])

    modes = {c.path: c.mode for c in changes}
    assert modes == {"b.py": "FULL", "a.py": "DIFF"}


def test_parse_diff_section_wrapped_in_markdown_fence_is_not_corrupt(repo):
    """Issue #248: the Coder may wrap the raw diff in a Markdown fenced code
    block (```diff / ```) on top of the FILE/MODE delimiters. The fences are
    not diff syntax, so _extract_diff must strip them or git apply fails with
    a 'corrupt patch' error."""
    diff = _make_diff(repo, "value = 1\n", "value = 2\n")
    output = "=== FILE: a.py ===\n=== MODE: DIFF ===\n```diff\n" + diff + "```\n=== END FILE ===\n"

    changes = parse_coder_output(output, ["a.py"])

    assert len(changes) == 1
    assert changes[0].mode == "DIFF"
    assert "```" not in changes[0].body
    assert changes[0].body.startswith("diff --git")
    assert changes[0].body.endswith("+value = 2\n")


def test_apply_diff_change_wrapped_in_markdown_fence_still_applies(repo):
    """End-to-end: a fenced DIFF body must apply cleanly, both in the syntax
    check and the real apply - the exact failure mode seen on issue #248."""
    diff = _make_diff(repo, "value = 1\n", "value = 2\n")
    output = "=== FILE: a.py ===\n=== MODE: DIFF ===\n```diff\n" + diff + "```\n=== END FILE ===\n"
    (changes,) = parse_coder_output(output, ["a.py"])

    apply_file_change(repo, changes)

    assert (Path(repo) / "a.py").read_text() == "value = 2\n"
def test_parse_full_section_marker_inside_content_does_not_truncate():
    """Issue #254: `=== END FILE ===` inside file content (e.g. the
    malformed-diff prompt text in agents/analyser.py) must not terminate the
    section. Only a marker on its own line ends a section; a bare substring
    match silently dropped every change after the lookalike text."""
    body = (
        "value = 1\n"
        "    prompt = f\"\"\"...\n"
        "correct `=== FILE: ... === / === MODE: FULL|DIFF === / === END FILE ===` sections\n"
        '    """\n'
        "value = 2\n"
    )
    output = _full_file_output("a.py", body) + _full_file_output("b.py", "x = 1")

    changes = parse_coder_output(output, ["a.py", "b.py"])

    assert [c.path for c in changes] == ["a.py", "b.py"]
    assert "=== END FILE ===` sections" in changes[0].body
    assert changes[0].body.endswith("value = 2")
    assert changes[1].body == "x = 1"


def test_parse_full_section_marker_at_eof_without_trailing_newline():
    """The final `=== END FILE ===` may sit at EOF with no trailing newline
    (the leniency the old `\r?\n?` prefix provided) without being mistaken
    for content."""
    output = "=== FILE: a.py ===\n=== MODE: FULL ===\nvalue = 2\n=== END FILE ==="

    changes = parse_coder_output(output, ["a.py"])

    assert changes[0].body == "value = 2"


def test_parse_full_section_marker_mid_line_with_trailing_text_does_not_truncate():
    """A lookalike marker that is NOT the whole line - i.e. `... === END FILE
    === more text` - must never terminate the section, even when the marker
    is followed only by trailing content before the newline. Only a marker
    that occupies its own line ends a section."""
    body = (
        "value = 1\n"
        "some text === END FILE === more text\n"
        "value = 2\n"
    )
    output = _full_file_output("a.py", body)

    changes = parse_coder_output(output, ["a.py"])

    assert len(changes) == 1
    assert "some text === END FILE === more text" in changes[0].body
    assert changes[0].body.endswith("value = 2")


def test_apply_diff_change_with_wrong_hunk_counts_still_applies(repo):
    """Issue #248: the Coder emits hunk headers with wrong line counts (e.g.
    @@ -5 +1 @@ for a one-line hunk). git apply --recount ignores the stated
    counts and deduces them from the patch, so the diff must still apply."""
    diff = _make_diff(repo, "value = 1\n", "value = 2\n")
    corrupted = diff.replace("@@ -1 +1 @@", "@@ -5 +1 @@")
    assert corrupted != diff
    change = FileChange(path="a.py", mode="DIFF", body=corrupted)

    apply_file_change(repo, change)

    assert (Path(repo) / "a.py").read_text() == "value = 2\n"


def test_parse_diff_section_preserves_fence_like_context_lines():
    """A diff editing Markdown can legitimately contain fence-looking
    context lines (" ```", " ```python"). Only a fence wrapping the whole
    block may be stripped - interior fence-like lines are real content and
    must survive extraction (issue #248 review)."""
    diff = (
        "diff --git a/README.md b/README.md\n"
        "index 1111111..2222222 100644\n"
        "--- a/README.md\n"
        "+++ b/README.md\n"
        "@@ -1,4 +1,4 @@\n"
        " ```\n"
        "-old line\n"
        "+new line\n"
        " ```python\n"
        "```\n"
    )
    output = "=== FILE: README.md ===\n=== MODE: DIFF ===\n```diff\n" + diff + "=== END FILE ===\n"

    (change,) = parse_coder_output(output, ["README.md"])

    # The two interior fence-looking context lines survive; the wrapper
    # fences (```diff before the header, ``` after the last hunk) are gone.
    assert " ```" in change.body
    assert " ```python" in change.body
    assert change.body.count("```") == 2


def test_apply_diff_change_with_fence_like_context_lines_still_applies(repo):
    """End-to-end for the same shape: a real diff whose context contains
    fence lines must extract and apply cleanly."""
    (Path(repo) / "README.md").write_text("title\n```\n```python\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "add readme")
    diff = _make_diff(
        repo,
        "title\n```\n```python\n",
        "updated\n```\n```python\n",
        path="README.md",
    )
    output = "=== FILE: README.md ===\n=== MODE: DIFF ===\n```diff\n" + diff + "```\n=== END FILE ===\n"

    (change,) = parse_coder_output(output, ["README.md"])

    apply_file_change(repo, change)

    assert (Path(repo) / "README.md").read_text() == "updated\n```\n```python\n"


def test_parse_rejects_empty_output():
    with pytest.raises(MalformedOutputError, match="empty"):
        parse_coder_output("", ["a.py"])


def test_parse_rejects_output_with_no_sections():
    with pytest.raises(MalformedOutputError, match="no.*sections"):
        parse_coder_output("just some prose, no delimiters", ["a.py"])


def test_parse_rejects_undeclared_file():
    output = _full_file_output("secret.py", "steal_data()")

    with pytest.raises(MalformedOutputError, match="undeclared file"):
        parse_coder_output(output, ["a.py"])


def test_parse_rejects_path_escaping_repo():
    output = _full_file_output("../../etc/passwd", "hacked")

    with pytest.raises(MalformedOutputError, match="escapes"):
        parse_coder_output(output, ["../../etc/passwd"])


def test_parse_rejects_absolute_path():
    output = _full_file_output("/etc/passwd", "hacked")

    # Sanitization rejects absolute paths up front (before _escapes_repo).
    with pytest.raises(MalformedOutputError, match="invalid file path"):
        parse_coder_output(output, ["/etc/passwd"])


def test_parse_rejects_duplicate_file_sections():
    output = _full_file_output("a.py", "x = 1") + "\n" + _full_file_output("a.py", "x = 2")

    with pytest.raises(MalformedOutputError, match="duplicate"):
        parse_coder_output(output, ["a.py"])


def test_parse_rejects_diff_section_with_no_diff_content():
    output = _diff_output("a.py", "not a real diff, just words")

    with pytest.raises(MalformedOutputError, match="no parseable unified diff"):
        parse_coder_output(output, ["a.py"])


# --- apply_file_change -------------------------------------------------------


def test_apply_full_file_change_writes_file(repo):
    change = FileChange(path="a.py", mode="FULL", body="value = 2")

    apply_file_change(repo, change)

    assert (Path(repo) / "a.py").read_text() == "value = 2\n"


def test_apply_full_file_change_creates_new_file_with_parents(repo):
    change = FileChange(path="pkg/new.py", mode="FULL", body="x = 1")

    apply_file_change(repo, change)

    assert (Path(repo) / "pkg" / "new.py").read_text() == "x = 1\n"


def test_apply_diff_change_applies_cleanly(repo):
    diff = _make_diff(repo, "value = 1\n", "value = 2\n")
    change = FileChange(path="a.py", mode="DIFF", body=diff)

    apply_file_change(repo, change)

    assert (Path(repo) / "a.py").read_text() == "value = 2\n"


def test_apply_diff_change_raises_on_non_applying_diff(repo):
    # This diff's context ("value = 999") doesn't match the committed
    # content ("value = 1"), so it can't apply.
    change = FileChange(path="a.py", mode="DIFF", body=_STALE_DIFF)

    with pytest.raises(MalformedOutputError, match="does not apply"):
        apply_file_change(repo, change)

    # File must be untouched by the failed apply attempt.
    assert (Path(repo) / "a.py").read_text() == "value = 1\n"


# --- run_ci_checks -----------------------------------------------------------


def test_run_ci_checks_passes_on_zero_exit(repo):
    passed, output = run_ci_checks(repo, ["python", "-c", "print('all good')"])

    assert passed is True
    assert "all good" in output


def test_run_ci_checks_fails_on_nonzero_exit(repo):
    passed, output = run_ci_checks(repo, ["python", "-c", "import sys; print('boom'); sys.exit(1)"])

    assert passed is False
    assert "boom" in output


def test_run_ci_checks_handles_missing_command(repo):
    passed, output = run_ci_checks(repo, ["definitely-not-a-real-command-xyz"])

    assert passed is False
    assert "definitely-not-a-real-command-xyz" in output


# --- get_working_diff ---------------------------------------------------------


def test_get_working_diff_includes_new_and_modified_files(repo):
    (Path(repo) / "a.py").write_text("value = 2\n")
    (Path(repo) / "new.py").write_text("x = 1\n")

    diff = get_working_diff(repo)

    assert "a.py" in diff
    assert "new.py" in diff


# --- run_revision_attempt: success -------------------------------------------


def test_run_revision_attempt_success_leaves_changes_in_place(repo):
    start = get_current_commit(repo)
    output = _full_file_output("a.py", "value = 2")

    with patch("workspace.run_ci_checks", return_value=(True, "tests passed")):
        result = run_revision_attempt(repo, output, ["a.py"], start_commit=start)

    assert result.success is True
    assert result.test_output == "tests passed"
    assert "a.py" in result.diff_output
    assert (Path(repo) / "a.py").read_text() == "value = 2\n"


def test_run_revision_attempt_uses_head_when_start_commit_omitted(repo):
    output = _full_file_output("a.py", "value = 2")

    with patch("workspace.run_ci_checks", return_value=(True, "ok")):
        result = run_revision_attempt(repo, output, ["a.py"])

    assert result.success is True


# --- run_revision_attempt: rollback on malformed output ----------------------


def test_run_revision_attempt_rolls_back_on_malformed_output(repo):
    start = get_current_commit(repo)

    result = run_revision_attempt(repo, "not delimited at all", ["a.py"], start_commit=start)

    assert result.success is False
    assert "malformed" in result.error
    assert get_current_commit(repo) == start
    assert (Path(repo) / "a.py").read_text() == "value = 1\n"


# --- run_revision_attempt: rollback on apply failure -------------------------


def test_run_revision_attempt_rolls_back_on_apply_failure(repo):
    start = get_current_commit(repo)
    output = _diff_output("a.py", _STALE_DIFF)

    result = run_revision_attempt(repo, output, ["a.py"], start_commit=start)

    assert result.success is False
    assert "apply failed" in result.error
    assert get_current_commit(repo) == start
    assert (Path(repo) / "a.py").read_text() == "value = 1\n"


def test_run_revision_attempt_rolls_back_partial_apply_when_second_file_fails(repo):
    """First file's full-file write lands, second file's diff fails - both
    must be rolled back, not just the failing one."""
    start = get_current_commit(repo)
    (Path(repo) / "b.py").write_text("y = 1\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "add b.py")
    start = get_current_commit(repo)

    stale_diff_b = """diff --git a/b.py b/b.py
index aaaaaaa..bbbbbbb 100644
--- a/b.py
+++ b/b.py
@@ -1 +1 @@
-y = 999
+y = 2
"""
    output = _full_file_output("a.py", "value = 2") + "\n" + _diff_output("b.py", stale_diff_b)

    result = run_revision_attempt(repo, output, ["a.py", "b.py"], start_commit=start)

    assert result.success is False
    assert (Path(repo) / "a.py").read_text() == "value = 1\n"
    assert (Path(repo) / "b.py").read_text() == "y = 1\n"
    assert get_current_commit(repo) == start


# --- run_revision_attempt: rollback on CI failure ----------------------------


def test_run_revision_attempt_rolls_back_on_ci_failure(repo):
    start = get_current_commit(repo)
    output = _full_file_output("a.py", "value = 2")

    with patch("workspace.run_ci_checks", return_value=(False, "AssertionError: expected 2 got 1")):
        result = run_revision_attempt(repo, output, ["a.py"], start_commit=start)

    assert result.success is False
    assert result.error == "CI checks failed"
    assert "AssertionError" in result.test_output
    assert get_current_commit(repo) == start
    assert (Path(repo) / "a.py").read_text() == "value = 1\n"


def test_failed_attempt_does_not_bleed_into_next_attempt(repo):
    """The scenario design.md calls out explicitly: a failed attempt's
    changes must not be visible to the next revision attempt."""
    start = get_current_commit(repo)
    bad_output = _full_file_output("a.py", "value = 2")

    with patch("workspace.run_ci_checks", return_value=(False, "fail")):
        first = run_revision_attempt(repo, bad_output, ["a.py"], start_commit=start)
    assert first.success is False

    # Second attempt starts from a prompt built without any trace of the
    # first attempt's rejected content still on disk.
    assert (Path(repo) / "a.py").read_text() == "value = 1\n"

    good_output = _full_file_output("a.py", "value = 3")
    with patch("workspace.run_ci_checks", return_value=(True, "ok")):
        second = run_revision_attempt(repo, good_output, ["a.py"], start_commit=start)

    assert second.success is True
    assert (Path(repo) / "a.py").read_text() == "value = 3\n"


# --- run_revision_attempt: rollback on interruption --------------------------


def test_run_revision_attempt_rolls_back_and_reraises_on_interruption(repo):
    start = get_current_commit(repo)
    output = _full_file_output("a.py", "value = 2")

    with patch("workspace.run_ci_checks", side_effect=KeyboardInterrupt):
        with pytest.raises(KeyboardInterrupt):
            run_revision_attempt(repo, output, ["a.py"], start_commit=start)

    assert get_current_commit(repo) == start
    assert (Path(repo) / "a.py").read_text() == "value = 1\n"


def test_run_revision_attempt_rolls_back_on_unexpected_exception(repo):
    start = get_current_commit(repo)
    output = _full_file_output("a.py", "value = 2")

    with patch("workspace.run_ci_checks", side_effect=RuntimeError("disk full")):
        with pytest.raises(RuntimeError, match="disk full"):
            run_revision_attempt(repo, output, ["a.py"], start_commit=start)

    assert get_current_commit(repo) == start
    assert (Path(repo) / "a.py").read_text() == "value = 1\n"


# --- WorkspaceError -----------------------------------------------------------


def test_reset_to_commit_raises_workspace_error_on_bad_commit(repo):
    with pytest.raises(WorkspaceError):
        reset_to_commit(repo, "not-a-real-commit-sha")


# --- run_ci_checks / run_revision_attempt: explicit env (#159) --------------


def test_run_ci_checks_passes_env_to_subprocess(repo):
    """The Scheduler's target env reaches the CI subprocess explicitly."""
    env = {"OLLAMA_ENDPOINT": "http://pc-a:11434", "OLLAMA_MODEL": "qwen:7b"}
    with patch("workspace.subprocess.run") as m_run:
        m_run.return_value = subprocess.CompletedProcess(
            [], 0, stdout="ok", stderr=""
        )
        passed, output = run_ci_checks(repo, ["echo", "hi"], env=env)

    assert passed is True
    assert "ok" in output
    assert m_run.call_args.kwargs["env"] == env


def test_run_revision_attempt_forwards_env_to_ci_checks(repo):
    """run_revision_attempt forwards env to the CI-check subprocess."""
    start = get_current_commit(repo)
    env = {"OLLAMA_ENDPOINT": "http://pc-a:11434"}
    output = _full_file_output("a.py", "value = 2")

    with patch("workspace.run_ci_checks", return_value=(True, "ok")) as m_ci:
        result = run_revision_attempt(
            repo, output, ["a.py"], start_commit=start, env=env
        )

    assert result.success is True
    assert m_ci.call_args.kwargs["env"] == env


# --- base clone (per-issue workspaces, #160) -------------------------------


def test_ensure_base_clone_reuses_existing_checkout(repo):
    """An existing checkout is reused as-is: no clone is attempted."""
    head = get_current_commit(repo)
    with patch(
        "workspace._clone_url",
        side_effect=AssertionError("existing checkout must not be cloned"),
    ):
        assert ensure_base_clone(repo, "owner/repo") == repo
    assert get_current_commit(repo) == head


def test_ensure_base_clone_clones_when_missing(tmp_path, repo):
    """A missing WORKSPACE_ROOT — nested parents included — is fresh-cloned."""
    target = tmp_path / "a" / "b" / "base"
    with patch("workspace._clone_url", return_value=repo):
        result = ensure_base_clone(str(target), "owner/repo")

    assert result == str(target)
    assert (target / ".git").exists()
    assert get_current_commit(str(target)) == get_current_commit(repo)
    assert (target / "a.py").read_text() == "value = 1\n"


def test_ensure_base_clone_clones_into_empty_directory(tmp_path, repo):
    """An empty WORKSPACE_ROOT directory is a valid clone target."""
    target = tmp_path / "base"
    target.mkdir()
    with patch("workspace._clone_url", return_value=repo):
        ensure_base_clone(str(target), "owner/repo")

    assert (target / ".git").exists()
    assert get_current_commit(str(target)) == get_current_commit(repo)


def test_ensure_base_clone_refuses_non_empty_non_git_directory(tmp_path):
    """A non-empty non-git WORKSPACE_ROOT is user data: refused, untouched."""
    target = tmp_path / "base"
    target.mkdir()
    (target / "precious.txt").write_text("keep me")

    with pytest.raises(WorkspaceError, match="non-empty"):
        ensure_base_clone(str(target), "owner/repo")

    assert (target / "precious.txt").read_text() == "keep me"


def test_ensure_base_clone_refuses_non_directory_path(tmp_path):
    """A file at WORKSPACE_ROOT is refused, never clobbered."""
    target = tmp_path / "base"
    target.write_text("I am a file")

    with pytest.raises(WorkspaceError, match="not a directory"):
        ensure_base_clone(str(target), "owner/repo")

    assert target.read_text() == "I am a file"


def test_ensure_base_clone_raises_when_clone_fails(tmp_path):
    """A failed clone is a WorkspaceError, so the caller can skip the pass."""
    with patch(
        "workspace._clone_url", return_value=str(tmp_path / "no-such-source")
    ):
        with pytest.raises(WorkspaceError, match="clone"):
            ensure_base_clone(str(tmp_path / "base"), "owner/repo")


# --- subprocess timeouts (#45) ---------------------------------------------


def test_run_git_passes_default_timeout(repo):
    """Every git call is bounded by DEFAULT_GIT_TIMEOUT unless overridden."""
    with patch("workspace.subprocess.run") as m_run:
        m_run.return_value = subprocess.CompletedProcess([], 0, stdout="", stderr="")
        _run_git(repo, "rev-parse", "HEAD")
    assert m_run.call_args.kwargs["timeout"] == DEFAULT_GIT_TIMEOUT


def test_run_git_passes_custom_timeout(repo):
    """Callers can override the git timeout per call."""
    with patch("workspace.subprocess.run") as m_run:
        m_run.return_value = subprocess.CompletedProcess([], 0, stdout="", stderr="")
        _run_git(repo, "status", timeout=7)
    assert m_run.call_args.kwargs["timeout"] == 7


def test_run_git_timeout_raises_workspace_error(repo):
    """A hung git process surfaces as WorkspaceError, not an endless block."""
    with patch(
        "workspace.subprocess.run",
        side_effect=subprocess.TimeoutExpired(["git", "fetch"], DEFAULT_GIT_TIMEOUT),
    ):
        with pytest.raises(WorkspaceError, match="timed out"):
            _run_git(repo, "fetch")


def test_ensure_base_clone_clone_uses_generous_timeout(tmp_path):
    """A fresh clone gets CLONE_TIMEOUT, not the tight git default (#45)."""
    with patch("workspace._run_git") as m_git:
        m_git.return_value = subprocess.CompletedProcess([], 0, stdout="", stderr="")
        ensure_base_clone(str(tmp_path / "clone"), "owner/repo")
    assert m_git.call_args.kwargs["timeout"] == CLONE_TIMEOUT


def test_run_ci_checks_passes_default_timeout(repo):
    """CI checks are bounded by DEFAULT_CI_TIMEOUT unless overridden."""
    with patch("workspace.subprocess.run") as m_run:
        m_run.return_value = subprocess.CompletedProcess(
            [], 0, stdout="ok", stderr=""
        )
        passed, output = run_ci_checks(repo, ["echo", "hi"])
    assert passed is True
    assert m_run.call_args.kwargs["timeout"] == DEFAULT_CI_TIMEOUT


def test_run_ci_checks_timeout_raises_workspace_error(repo):
    """A hung CI command raises WorkspaceError (not a fake test failure)."""
    with patch(
        "workspace.subprocess.run",
        side_effect=subprocess.TimeoutExpired(["pytest"], DEFAULT_CI_TIMEOUT),
    ):
        with pytest.raises(WorkspaceError, match="timed out"):
            run_ci_checks(repo, ["pytest"])


# --- path sanitization (LLM-decorated paths) --------------------------------


@pytest.mark.parametrize(
    "raw, expected",
    [
        # Plain paths pass through untouched.
        ("scheduler.py", "scheduler.py"),
        ("tests/test_scheduler.py", "tests/test_scheduler.py"),
        # Markdown backticks are stripped.
        ("`tests/test_analyser.py`", "tests/test_analyser.py"),
        ("`.github/workflows/ci.yml`", ".github/workflows/ci.yml"),
        # Glob prefixes from issue-body bullet lists are stripped.
        ("** README.md", "README.md"),
        ("** `target_pool.py`", "target_pool.py"),
        ("**  `.github/workflows/python-ci.yml`", ".github/workflows/python-ci.yml"),
        # Windows separators normalize to forward slashes.
        (r"backend\agent\trading_agent.py", "backend/agent/trading_agent.py"),
        (r"`backend\pyproject.toml`", "backend/pyproject.toml"),
        # Trailing parenthetical asides are dropped.
        ("docs/ (if applicable)", "docs/"),
        ("cli.py (or the module containing the report generation logic)", "cli.py"),
        # Underscores are valid filename characters and are kept.
        ("** `tests/test_native_coder.py`", "tests/test_native_coder.py"),
    ],
)
def test_sanitize_file_path_strips_decoration(raw, expected):
    assert sanitize_file_path(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "   ",
        "**",
        "`**`",
        ".",
        "..",
        "/abs/path.py",
        "C:/abs/path.py",
        "C:\abs\path.py",
        "a?b.py",
        "a*b.py",
        'a"b.py',
        "a<b.py",
        "a|b.py",
    ],
)
def test_sanitize_file_path_rejects_non_paths(raw):
    assert sanitize_file_path(raw) is None


def test_parse_coder_output_matches_decorated_path_to_declared_file():
    """A Coder that echoes back a backticked/globbed path still matches its
    declared, sanitized file — the junk never reaches the filesystem."""
    output = (
        "=== FILE: ** `scheduler.py` ===\n"
        "=== MODE: FULL ===\n"
        "x = 1\n"
        "=== END FILE ===\n"
    )
    changes = parse_coder_output(output, declared_files=["scheduler.py"])
    assert [c.path for c in changes] == ["scheduler.py"]


def test_parse_coder_output_rejects_path_that_sanitizes_to_nothing():
    """A FILE marker carrying only decoration (e.g. ``**``) is malformed."""
    output = (
        "=== FILE: ** ===\n"
        "=== MODE: FULL ===\n"
        "x = 1\n"
        "=== END FILE ===\n"
    )
    with pytest.raises(MalformedOutputError, match="invalid file path"):
        parse_coder_output(output, declared_files=["scheduler.py"])


def test_sanitize_file_path_keeps_legitimate_parentheses():
    """Whitespace-separated asides are stripped, but real filename
    parentheses are kept (review finding)."""
    assert sanitize_file_path("src/foo(bar).py") == "src/foo(bar).py"
    assert sanitize_file_path("docs/api(v2)/index.md") == "docs/api(v2)/index.md"
    assert sanitize_file_path("docs/ (if applicable)") == "docs/"


# --- #208: run_ci_checks honours a caller-supplied timeout ----------------


def test_run_ci_checks_passes_custom_timeout(repo):
    """An explicit timeout overrides DEFAULT_CI_TIMEOUT (#208).

    The parameter wiring is the same shape as _run_git's, which is
    already covered; this pins the public method too, so a refactor that
    drops the argument before it reaches subprocess.run is caught.
    """
    with patch("workspace.subprocess.run") as m_run:
        m_run.return_value = subprocess.CompletedProcess([], 0, stdout="ok", stderr="")
        passed, _ = run_ci_checks(repo, ["echo", "hi"], timeout=123)

    assert passed is True
    assert m_run.call_args.kwargs["timeout"] == 123


def test_run_ci_checks_custom_timeout_appears_in_the_error(repo):
    """The WorkspaceError quotes the effective timeout, not the default."""
    with patch(
        "workspace.subprocess.run",
        side_effect=subprocess.TimeoutExpired(["pytest"], 123),
    ):
        with pytest.raises(WorkspaceError, match="timed out after 123s"):
            run_ci_checks(repo, ["pytest"], timeout=123)


# --- #178: an existing checkout must be the scheduled repository ----------


@pytest.mark.parametrize(
    ("url", "identity"),
    [
        ("https://github.com/owner/name.git", "github.com/owner/name"),
        ("https://github.com/owner/name", "github.com/owner/name"),
        ("https://github.com/owner/name/", "github.com/owner/name"),
        ("git@github.com:owner/name.git", "github.com/owner/name"),
        ("git@github.com:owner/name", "github.com/owner/name"),
        ("ssh://git@github.com/owner/name.git", "github.com/owner/name"),
        (
            "https://x-access-token:TOKEN@github.com/owner/name.git",
            "github.com/owner/name",
        ),
        # Case is not significant on GitHub.
        ("https://github.com/OWNER/Name.git", "github.com/owner/name"),
        # GitHub's SSH-over-443 endpoint and the www alias are github.com.
        ("ssh://git@ssh.github.com:443/owner/name.git", "github.com/owner/name"),
        ("git@ssh.github.com:owner/name.git", "github.com/owner/name"),
        ("https://www.github.com/owner/name", "github.com/owner/name"),
        # A different forge is a different repository, same owner/name.
        ("https://gitlab.com/owner/name.git", "gitlab.com/owner/name"),
        # Unrecognised shapes are "cannot verify", not a mismatch. The
        # file:// cases matter most: an authority-less URL must not have
        # its first path segment mistaken for an owner.
        ("file:///srv/repo", None),
        ("file:///data/mirror", None),
        ("/plain/local/path", None),
        ("../relative", None),
        ("C:\\path\\to\\repo", None),
        ("", None),
        ("   ", None),
        ("not a url", None),
        ("https://github.com/owner", None),
        ("https://github.com/owner/name/extra", None),
        # A port on a non-aliased host is stripped, the host is not.
        ("https://github.com:8443/owner/name.git", "github.com/owner/name"),
        ("https://git.example.com:8443/owner/name.git", "git.example.com/owner/name"),
        # scp-style needs a real owner/name: a Windows path and a
        # host:port pair are not repositories.
        ("example.com:8080", None),
        ("C:/path/to/repo", None),
    ],
)
def test_repo_identity_normalises_remote_urls(url, identity):
    """Identity, not URL spelling — SSH and HTTPS name the same repo."""
    assert _repo_identity(url) == identity


def test_ensure_base_clone_rejects_a_checkout_of_another_repo(repo):
    """A misconfigured WORKSPACE_ROOT fails loudly instead of silently (#178)."""
    with patch("workspace._run_git") as m_git:

        def _fake(path, *args, **kwargs):
            if args[:1] == ("rev-parse",):
                return subprocess.CompletedProcess([], 0, stdout="", stderr="")
            if args[:3] == ("remote", "get-url", "origin"):
                return subprocess.CompletedProcess(
                    [], 0, stdout="https://github.com/someone/other.git\n", stderr=""
                )
            raise AssertionError(f"unexpected git call: {args}")

        m_git.side_effect = _fake
        with pytest.raises(WorkspaceError, match="not 'github.com/owner/name'"):
            ensure_base_clone(repo, "owner/name")


def test_ensure_base_clone_accepts_an_ssh_remote_of_the_same_repo(repo):
    """The pre-clone route documented for private repos must keep working.

    Private repos are pre-cloned by hand (#177), very often over SSH. A
    strict URL-string comparison would reject exactly that setup.
    """
    with patch("workspace._run_git") as m_git:

        def _fake(path, *args, **kwargs):
            if args[:1] == ("rev-parse",):
                return subprocess.CompletedProcess([], 0, stdout="", stderr="")
            if args[:3] == ("remote", "get-url", "origin"):
                return subprocess.CompletedProcess(
                    [], 0, stdout="git@github.com:Owner/Name.git\n", stderr=""
                )
            raise AssertionError(f"unexpected git call: {args}")

        m_git.side_effect = _fake
        assert ensure_base_clone(repo, "owner/name") == repo


def test_ensure_base_clone_allows_a_checkout_with_no_origin(repo, caplog):
    """No origin means "cannot verify", not "wrong repo"."""
    with patch("workspace._run_git") as m_git:

        def _fake(path, *args, **kwargs):
            if args[:1] == ("rev-parse",):
                return subprocess.CompletedProcess([], 0, stdout="", stderr="")
            if args[:3] == ("remote", "get-url", "origin"):
                return subprocess.CompletedProcess(
                    [], 2, stdout="", stderr="error: No such remote 'origin'"
                )
            raise AssertionError(f"unexpected git call: {args}")

        m_git.side_effect = _fake
        with caplog.at_level(logging.WARNING):
            assert ensure_base_clone(repo, "owner/name") == repo

    assert "skipping the base-clone repository check" in caplog.text


def _origin_only(url: str):
    """A _run_git fake answering rev-parse and `remote get-url origin`."""

    def _fake(path, *args, **kwargs):
        if args[:1] == ("rev-parse",):
            return subprocess.CompletedProcess([], 0, stdout="", stderr="")
        if args[:3] == ("remote", "get-url", "origin"):
            return subprocess.CompletedProcess([], 0, stdout=url + "\n", stderr="")
        raise AssertionError(f"unexpected git call: {args}")

    return _fake


def test_ensure_base_clone_mismatch_never_echoes_a_token(repo):
    """A credential-bearing origin must not reach the error text (#178).

    The message goes to stderr and to log files, so a token in the remote
    URL would leak there.
    """
    token_url = "https://x-access-token:ghp_SECRETVALUE@github.com/someone/other.git"
    with patch("workspace._run_git", side_effect=_origin_only(token_url)):
        with pytest.raises(WorkspaceError) as exc:
            ensure_base_clone(repo, "owner/name")

    assert "ghp_SECRETVALUE" not in str(exc.value)
    assert "x-access-token" not in str(exc.value)
    assert "***@github.com" in str(exc.value)


def test_ensure_base_clone_mismatch_can_be_downgraded_by_env(repo, monkeypatch, caplog):
    """A deliberate fork-origin workspace has an escape hatch (#178)."""
    monkeypatch.setenv("WORM_SKIP_REMOTE_CHECK", "1")
    with patch(
        "workspace._run_git",
        side_effect=_origin_only("https://github.com/myfork/name.git"),
    ):
        with caplog.at_level(logging.WARNING):
            assert ensure_base_clone(repo, "owner/name") == repo

    assert "WORM_SKIP_REMOTE_CHECK=1" in caplog.text


def test_ensure_base_clone_survives_a_git_timeout_reading_origin(repo, caplog):
    """The check fails open even when _run_git itself raises (#178)."""

    def _fake(path, *args, **kwargs):
        if args[:1] == ("rev-parse",):
            return subprocess.CompletedProcess([], 0, stdout="", stderr="")
        raise WorkspaceError("git remote get-url origin timed out after 30.0s")

    with patch("workspace._run_git", side_effect=_fake):
        with caplog.at_level(logging.WARNING):
            assert ensure_base_clone(repo, "owner/name") == repo

    assert "skipping the base-clone repository check" in caplog.text


def test_ensure_base_clone_disables_git_credential_prompts(tmp_path):
    """A private repo must fail fast, not sit on a prompt for 600s (#177)."""
    with patch("workspace._run_git") as m_git:
        m_git.return_value = subprocess.CompletedProcess([], 0, stdout="", stderr="")
        ensure_base_clone(str(tmp_path / "clone"), "owner/repo")

    assert m_git.call_args.kwargs["env"]["GIT_TERMINAL_PROMPT"] == "0"


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        # Nothing to redact.
        ("https://github.com/owner/name.git", "https://github.com/owner/name.git"),
        ("git@github.com:owner/name.git", "git@github.com:owner/name.git"),
        ("", ""),
        # user:password and token forms.
        (
            "https://x-access-token:ghp_SECRET@github.com/owner/name.git",
            "https://***@github.com/owner/name.git",
        ),
        (
            "https://user:pass@github.com/owner/name",
            "https://***@github.com/owner/name",
        ),
        ("https://ghp_SECRET@github.com/o/n", "https://***@github.com/o/n"),
        # An @ later in the path is not userinfo and must survive.
        (
            "https://github.com/owner/name@v2.git",
            "https://github.com/owner/name@v2.git",
        ),
    ],
)
def test_redact_url_strips_userinfo(url, expected):
    """Credentials never reach an error message or a log line (#178)."""
    assert _redact_url(url) == expected
    if "SECRET" in url or "pass@" in url:
        assert "SECRET" not in _redact_url(url)
        assert "pass" not in _redact_url(url)


def test_ensure_base_clone_skips_the_check_for_a_malformed_repo(repo, caplog):
    """A repo argument that is not owner/name cannot be compared (#178).

    Fail-open, like every other case the check cannot resolve: it must
    not invent a mismatch out of an argument it could not parse.
    """
    with patch(
        "workspace._run_git",
        side_effect=_origin_only("https://github.com/someone/other.git"),
    ):
        with caplog.at_level(logging.WARNING):
            assert ensure_base_clone(repo, "owner/name/extra") == repo

    # No mismatch was reported — the check simply did not run.
    assert "refusing to run against the wrong repository" not in caplog.text
