"""Tests for progress_reporter.py (#345).

Every GitHub write (`post_issue_comment`, `_find_progress_comment_id`,
`_update_progress_comment`) is mocked in every test here - this module's
own contract (see its module docstring) is that a real `gh` call never
happens from a test, only from a genuine build/Action run.
"""

from unittest.mock import patch

import pytest

import progress_reporter as pr


@pytest.fixture(autouse=True)
def _isolated_state_dir(tmp_path, monkeypatch):
    """Point progress_reporter at an isolated state dir under tmp_path,
    so tests never read or write the real `~/.issue-worm/progress/`."""
    monkeypatch.setenv(pr.STATE_DIR_ENV, str(tmp_path / "progress"))


@pytest.fixture
def gh(monkeypatch):
    """Mocks for the three GitHub-facing calls, plus a `body()` helper
    that returns the most recent comment body written (post, or the
    latest update)."""
    post = patch.object(pr, "post_issue_comment", return_value=True).start()
    find = patch.object(pr, "_find_progress_comment_id", return_value=99).start()
    update = patch.object(pr, "_update_progress_comment", return_value=True).start()

    def body():
        if update.call_args_list:
            return update.call_args_list[-1][0][2]
        return post.call_args_list[-1][0][2]

    yield type("GH", (), {"post": post, "find": find, "update": update, "body": staticmethod(body)})
    patch.stopall()


def test_start_posts_the_initial_working_comment(gh):
    pr.start("owner/repo", 1)

    gh.post.assert_called_once()
    assert gh.post.call_args[0][0] == "owner/repo"
    assert gh.post.call_args[0][1] == 1
    body = gh.body()
    assert body.startswith(pr.PROGRESS_MARKER)
    assert "working" in body
    assert "**Result:**" not in body


def test_stage_start_then_done_renders_the_checklist(gh):
    pr.start("owner/repo", 1)
    pr.record_stage_start("owner/repo", 1, "coder")
    assert "- [ ] coder" in gh.body()
    assert "current" in gh.body()

    pr.record_stage_done("owner/repo", 1, "coder", 5.25)
    assert "- [x] coder (5.2s)" in gh.body()
    assert "current" not in gh.body()


def test_first_write_posts_later_writes_update_the_same_comment(gh):
    pr.start("owner/repo", 1)
    pr.record_stage_start("owner/repo", 1, "coder")
    pr.record_stage_done("owner/repo", 1, "coder", 1.0)
    pr.finish("owner/repo", 1, pr_url="https://example.com/pr/1")

    gh.post.assert_called_once()
    assert gh.update.call_count == 3
    # Every update targets the id resolved after the initial post.
    assert all(call.args[1] == 99 for call in gh.update.call_args_list)


def test_finish_with_pr_url_renders_success(gh):
    pr.start("owner/repo", 1)
    pr.finish("owner/repo", 1, pr_url="https://github.com/owner/repo/pull/7")

    body = gh.body()
    assert "· done" in body
    assert "**Result:** ✅ https://github.com/owner/repo/pull/7" in body


def test_finish_with_failure_detail_renders_failure(gh):
    pr.start("owner/repo", 1)
    pr.finish("owner/repo", 1, failure_detail="The Coder produced no output")

    body = gh.body()
    assert "· failed" in body
    assert "**Result:** ❌ The Coder produced no output" in body


def test_finish_is_idempotent(gh):
    pr.start("owner/repo", 1)
    pr.finish("owner/repo", 1, pr_url="https://example.com/pr/1")
    calls_after_first_finish = gh.update.call_count

    # A second finish() - defensive code, an unexpected retry - must not
    # overwrite the already-final record.
    pr.finish("owner/repo", 1, failure_detail="should never appear")

    assert gh.update.call_count == calls_after_first_finish
    assert "should never appear" not in gh.body()


def test_record_stage_start_without_a_prior_start_creates_fresh_state(gh):
    """An out-of-order call (e.g. `start()` itself failed to post, or was
    never called) must not drop the stage - it should still post/update
    with a fresh state rather than raising."""
    pr.record_stage_start("owner/repo", 42, "verifier")

    gh.post.assert_called_once()
    assert "- [ ] verifier" in gh.body()


def test_record_stage_done_without_a_matching_start_appends_a_closed_row(gh):
    pr.start("owner/repo", 1)

    pr.record_stage_done("owner/repo", 1, "verifier", 12.0)

    assert "- [x] verifier (12.0s)" in gh.body()


def test_a_second_stage_with_the_same_label_opens_a_new_row(gh):
    """Two distinct stages sharing a label (shouldn't happen in practice,
    but must not silently merge) each get their own checklist row."""
    pr.start("owner/repo", 1)
    pr.record_stage_start("owner/repo", 1, "coder")
    pr.record_stage_done("owner/repo", 1, "coder", 1.0)
    pr.record_stage_start("owner/repo", 1, "coder")

    body = gh.body()
    assert body.count("coder") == 2


def test_no_further_writes_happen_after_finish(gh):
    pr.start("owner/repo", 1)
    pr.finish("owner/repo", 1, pr_url="https://example.com/pr/1")

    pr.record_stage_start("owner/repo", 1, "late-stage")
    pr.record_stage_done("owner/repo", 1, "late-stage", 1.0)

    # Only the 2 writes from start()+finish() - the post-finish calls are
    # silently ignored per record_stage_start/done's `if state.finished:
    # return` guard, matching pro's semantics (#345/#591).
    assert gh.update.call_count == 1


def test_post_failure_is_swallowed_and_never_raises(gh):
    gh.post.return_value = False

    pr.start("owner/repo", 1)  # must not raise

    gh.find.assert_not_called()


def test_a_raising_gh_call_is_swallowed_and_never_raises(gh):
    gh.post.side_effect = OSError("gh not on PATH")

    pr.start("owner/repo", 1)  # must not raise
    pr.record_stage_start("owner/repo", 1, "coder")  # must not raise either


def test_missing_comment_id_after_post_skips_later_updates_but_never_raises(gh):
    gh.find.return_value = None

    pr.start("owner/repo", 1)
    pr.record_stage_start("owner/repo", 1, "coder")  # must not raise

    gh.update.assert_not_called()


def test_dry_run_never_resolves_a_comment_id_or_updates(gh):
    """dry_run=True is forwarded to `post_issue_comment` (which honours it
    without a real GitHub write - that's cicaid-devtools' own contract,
    not this module's), but this module must never try to resolve a real
    comment id or PATCH a comment while dry_run is set - there is nothing
    to resolve or edit, since nothing was actually posted."""
    pr.start("owner/repo", 1, dry_run=True)
    pr.record_stage_start("owner/repo", 1, "coder", dry_run=True)
    pr.record_stage_done("owner/repo", 1, "coder", 1.0, dry_run=True)
    pr.finish("owner/repo", 1, pr_url="https://example.com/pr/1", dry_run=True)

    assert gh.post.called
    gh.find.assert_not_called()
    gh.update.assert_not_called()
