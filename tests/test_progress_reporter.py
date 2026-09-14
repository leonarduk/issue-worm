"""Tests for progress_reporter.py (#345).

Every GitHub write (`post_issue_comment`, `_find_progress_comment_id`,
`_update_progress_comment`) is mocked in every test here - this module's
own contract (see its module docstring) is that a real `gh` call never
happens from a test, only from a genuine build/Action run.
"""

import json
import sys
from unittest.mock import patch

import pytest

import progress_reporter as pr


@pytest.fixture(autouse=True)
def _isolated_state_dir(tmp_path, monkeypatch):
    """Point progress_reporter at an isolated state dir under tmp_path,
    so tests never read or write the real `~/.issue-worm/progress/`."""
    monkeypatch.setenv(pr.STATE_DIR_ENV, str(tmp_path / "progress"))
    # This suite itself runs inside GitHub Actions in CI - without this,
    # every test would see a real run URL and take the #383 "adopt this
    # run's announce comment" path. Tests that want it opt in via
    # `_in_actions`.
    for name in ("GITHUB_RUN_ID", "GITHUB_RUN_ATTEMPT", "GITHUB_REPOSITORY", "GITHUB_SERVER_URL"):
        monkeypatch.delenv(name, raising=False)


def _in_actions(monkeypatch, run_id="42", attempt="1"):
    monkeypatch.setenv("GITHUB_SERVER_URL", "https://github.com")
    monkeypatch.setenv("GITHUB_REPOSITORY", "owner/repo")
    monkeypatch.setenv("GITHUB_RUN_ID", run_id)
    monkeypatch.setenv("GITHUB_RUN_ATTEMPT", attempt)
    return f"https://github.com/owner/repo/actions/runs/{run_id}"


@pytest.fixture
def gh(monkeypatch):
    """Mocks for the GitHub-facing calls, plus a `body()` helper that
    returns the most recent comment body written (post, or the latest
    update)."""
    post = patch.object(pr, "post_issue_comment", return_value=True).start()
    find = patch.object(pr, "_find_progress_comment_id", return_value=99).start()
    update = patch.object(pr, "_update_progress_comment", return_value=True).start()
    label = patch.object(pr, "_set_in_progress_label").start()

    def body():
        if update.call_args_list:
            return update.call_args_list[-1][0][2]
        return post.call_args_list[-1][0][2]

    yield type(
        "GH",
        (),
        {"post": post, "find": find, "update": update, "label": label, "body": staticmethod(body)},
    )
    patch.stopall()


def test_no_run_link_outside_actions(gh):
    pr.start("owner/repo", 1)

    assert "Actions run" not in gh.body()


def test_run_link_is_rendered_under_the_header_in_actions(gh, monkeypatch):
    url = _in_actions(monkeypatch)
    gh.find.side_effect = [None, 99]  # no announce comment to adopt; then the posted one

    pr.start("owner/repo", 1)

    gh.post.assert_called_once()
    assert gh.body().splitlines()[2] == f"[Actions run]({url})"


def test_a_rerun_links_to_its_own_attempt(gh, monkeypatch):
    url = _in_actions(monkeypatch, attempt="2")
    gh.find.side_effect = [None, 99]

    pr.start("owner/repo", 1)

    assert f"[Actions run]({url}/attempts/2)" in gh.body()


def test_start_adopts_this_runs_announce_comment_instead_of_posting(gh, monkeypatch):
    url = _in_actions(monkeypatch)

    pr.start("owner/repo", 1)

    gh.post.assert_not_called()
    assert gh.find.call_args.kwargs["must_contain"] == f"[Actions run]({url})"
    assert gh.update.call_args.args[1] == 99


def test_state_left_by_a_different_run_is_ignored(gh, monkeypatch):
    _in_actions(monkeypatch, run_id="42")
    pr.start("owner/repo", 1)
    pr.finish("owner/repo", 1, pr_url="https://example.com/pr/1")
    writes = gh.update.call_count

    # The next run never reaches start() (e.g. "not ready"), so only
    # action.yml's failure step calls finish(). The previous run's
    # finished record must not turn that into a no-op.
    url = _in_actions(monkeypatch, run_id="43")
    pr.finish("owner/repo", 1, failure_detail="not ready")

    assert gh.update.call_count == writes + 1
    assert f"[Actions run]({url})" in gh.body()
    assert "**Result:** ❌ not ready" in gh.body()


def test_start_adds_and_finish_removes_the_in_progress_label(gh):
    pr.start("owner/repo", 1)
    pr.finish("owner/repo", 1, pr_url="https://example.com/pr/1")

    assert [c.args for c in gh.label.call_args_list] == [
        ("owner/repo", 1, True),
        ("owner/repo", 1, False),
    ]


def test_dry_run_never_touches_the_label(gh):
    pr.start("owner/repo", 1, dry_run=True)
    pr.finish("owner/repo", 1, pr_url="https://example.com/pr/1", dry_run=True)

    gh.label.assert_not_called()


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


def test_state_is_scoped_by_repo_not_just_issue_number(gh):
    """Two different repos can each have their own issue #5 - without the
    repo in the state path, a run against one would silently read/write
    the other's in-flight state (DeepSeek review of #359, a real risk on
    any machine that dispatches issue-worm against more than one repo)."""
    pr.start("owner/repo-a", 5)
    pr.record_stage_start("owner/repo-a", 5, "coder")
    pr.record_stage_done("owner/repo-a", 5, "coder", 1.0)

    repo_a_body = gh.update.call_args_list[-1].args[2]
    assert "coder" in repo_a_body

    # A fresh dispatch for a *different* repo's own issue #5 must not see
    # repo-a's stages, and must not clobber repo-a's state file either.
    pr.start("owner/repo-b", 5)

    pr.record_stage_start("owner/repo-b", 5, "verifier")
    assert "coder" not in gh.body()
    assert "verifier" in gh.body()


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


class _FakeCompletedProcess:
    def __init__(self, stdout="", returncode=0, stderr=""):
        self.stdout = stdout
        self.returncode = returncode
        self.stderr = stderr


def test_decode_paginated_json_concatenates_multiple_page_arrays():
    """`gh api --paginate` writes one JSON array per page back-to-back
    with no separator - a plain `json.loads` raises past the first page.
    Regression test for the exact bug DeepSeek's review of #359 flagged:
    the previous implementation used `json.loads` directly and would
    silently stop updating the comment on any issue with more than one
    page of comments (default page size 30)."""
    text = '[{"id": 1}, {"id": 2}][{"id": 3}]'

    assert pr._decode_paginated_json(text) == [{"id": 1}, {"id": 2}, {"id": 3}]


def test_decode_paginated_json_handles_a_single_page():
    assert pr._decode_paginated_json('[{"id": 1}]') == [{"id": 1}]


def test_decode_paginated_json_handles_empty_output():
    assert pr._decode_paginated_json("") == []


def test_find_progress_comment_id_across_multiple_pages(monkeypatch):
    """End-to-end: `_find_progress_comment_id` must find the marker
    comment even when it's on a later page than the first, which is where
    it actually lives on a long-running issue (comments are oldest-first,
    and the progress comment is posted well after the issue was opened)."""
    page_1 = [{"id": 10, "created_at": "2026-01-01T00:00:00Z", "body": "unrelated"}]
    page_2 = [
        {
            "id": 11,
            "created_at": "2026-01-02T00:00:00Z",
            "body": f"{pr.PROGRESS_MARKER}\nsome progress",
        }
    ]
    stdout = json.dumps(page_1) + json.dumps(page_2)

    monkeypatch.setattr(
        pr.subprocess,
        "run",
        lambda *a, **k: _FakeCompletedProcess(stdout=stdout, returncode=0),
    )

    assert pr._find_progress_comment_id("owner/repo", 1) == 11


def test_find_progress_comment_id_returns_none_on_a_failed_gh_call(monkeypatch):
    monkeypatch.setattr(
        pr.subprocess,
        "run",
        lambda *a, **k: _FakeCompletedProcess(returncode=1, stderr="boom"),
    )

    assert pr._find_progress_comment_id("owner/repo", 1) is None


def test_find_progress_comment_id_returns_none_when_no_comment_matches(monkeypatch):
    stdout = json.dumps(
        [{"id": 1, "created_at": "2026-01-01T00:00:00Z", "body": "unrelated"}]
    )
    monkeypatch.setattr(
        pr.subprocess, "run", lambda *a, **k: _FakeCompletedProcess(stdout=stdout)
    )

    assert pr._find_progress_comment_id("owner/repo", 1) is None


def test_find_progress_comment_id_with_must_contain_skips_other_runs(monkeypatch):
    link = "[Actions run](https://github.com/owner/repo/actions/runs/12)"
    stdout = json.dumps(
        [
            {"id": 1, "created_at": "2026-01-01T00:00:00Z", "body": f"{pr.PROGRESS_MARKER}\n{link}"},
            # Newer, but another run's - and run 123's link contains
            # run 12's URL as a plain prefix.
            {
                "id": 2,
                "created_at": "2026-01-02T00:00:00Z",
                "body": f"{pr.PROGRESS_MARKER}\n[Actions run](https://github.com/owner/repo/actions/runs/123)",
            },
        ]
    )
    monkeypatch.setattr(
        pr.subprocess, "run", lambda *a, **k: _FakeCompletedProcess(stdout=stdout)
    )

    assert pr._find_progress_comment_id("owner/repo", 1, must_contain=link) == 1


@pytest.mark.parametrize("present", [True, False])
def test_set_in_progress_label_never_raises(monkeypatch, present):
    monkeypatch.setattr(
        pr.subprocess, "run", lambda *a, **k: _FakeCompletedProcess(returncode=1, stderr="404")
    )
    pr._set_in_progress_label("owner/repo", 1, present)

    def _raise(*a, **k):
        raise OSError("gh not on PATH")

    monkeypatch.setattr(pr.subprocess, "run", _raise)
    pr._set_in_progress_label("owner/repo", 1, present)


def test_load_post_issue_comment_raises_cleanly_when_cicaid_devtools_is_absent(
    monkeypatch,
):
    """`progress_reporter.py` must not require `cicaid_devtools` at module
    import time - `cli.py` imports this module unconditionally for every
    subcommand (`history`, `status`, `poll`, not just `build`/`progress`),
    so a hard top-level import here would take down the whole CLI on a
    broken `cicaid-devtools` install (DeepSeek review of #359). Simulates
    it genuinely absent via the same `sys.modules` sentinel idiom already
    used elsewhere in this codebase for `pro_cli`."""
    monkeypatch.setattr(pr, "post_issue_comment", None)
    monkeypatch.setitem(sys.modules, "cicaid_devtools", None)

    with pytest.raises(ImportError, match="cicaid-devtools is not installed"):
        pr._load_post_issue_comment()


def test_start_degrades_gracefully_when_cicaid_devtools_is_absent(monkeypatch):
    monkeypatch.setattr(pr, "post_issue_comment", None)
    monkeypatch.setitem(sys.modules, "cicaid_devtools", None)

    pr.start("owner/repo", 1)  # must not raise
