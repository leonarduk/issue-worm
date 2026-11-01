"""Structural checks on the composite action's own manifest, action.yml."""

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ACTION = Path(__file__).resolve().parent.parent / "action.yml"


def _steps() -> list[dict]:
    """Every step of the composite action, as YAML parses it."""
    return yaml.safe_load(ACTION.read_text(encoding="utf-8"))["runs"]["steps"]


def _run_steps() -> list[dict]:
    """Each step that has a `run:` script: its `name` and that script."""
    return [{"name": s.get("name", "?"), "run": s["run"]} for s in _steps() if "run" in s]


def test_no_expression_is_spliced_into_a_run_script():
    """GitHub substitutes `${{ }}` into the script text before bash parses
    it, so any value there - above all a caller-supplied input - is an
    injection surface. Values reach scripts through `env:` instead (#366)."""
    offenders = [
        step.get("name", "?")
        for step in _run_steps()
        if "${{" in step["run"]
    ]
    assert offenders == []


def _close_issue_line() -> str:
    (step,) = [s for s in _run_steps() if "CLOSE_ISSUE" in s["run"]]
    (line,) = [
        ln.strip() for ln in step["run"].splitlines() if ln.strip().startswith("close_issue=")
    ]
    return line


@pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")
@pytest.mark.parametrize(
    ("raw", "expected"),
    [("true", "true"), ("True", "true"), ("TRUE", "true"), ("false", "false"), ("", "")],
)
def test_close_issue_input_is_case_insensitive(raw, expected):
    """Any casing of 'true' closes the issue on merge (#324); the step
    compares this normalized value, never CLOSE_ISSUE itself."""
    result = subprocess.run(
        ["bash", "-c", _close_issue_line() + '\nprintf "%s" "$close_issue"'],
        capture_output=True,
        text=True,
        env={"CLOSE_ISSUE": raw, "PATH": "/usr/bin:/bin"},
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout == expected


def test_close_issue_comparisons_use_the_normalized_value():
    (step,) = [s for s in _run_steps() if "CLOSE_ISSUE" in s["run"]]
    script = step["run"]
    assert '"$CLOSE_ISSUE" =' not in script
    assert '"$CLOSE_ISSUE" !=' not in script
    assert script.count('"$close_issue"') == 2


def _pro_cli_check() -> str:
    """The `if ... import pro_cli ...; fi` block from the install step."""
    (step,) = [s for s in _run_steps() if "import pro_cli" in s["run"]]
    lines = step["run"].splitlines()
    start = next(i for i, ln in enumerate(lines) if "import pro_cli" in ln)
    end = next(i for i in range(start, len(lines)) if lines[i].strip() == "fi")
    return "\n".join(ln.strip() for ln in lines[start : end + 1])


@pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")
@pytest.mark.parametrize("importable", [True, False], ids=["leaked", "isolated"])
def test_install_step_fails_when_pro_cli_is_importable(tmp_path, importable):
    """The venv exists to keep pro_cli out of reach, or `issue-worm build`
    silently delegates to the pro engine; the install step checks that
    isolation held (#291)."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    # -S: no site-packages, so a pro_cli installed in the developer's own
    # environment (e.g. alongside issue-worm-pro) can't leak into the
    # "isolated" case; PYTHONPATH below is still honoured for "leaked".
    fake_python = bin_dir / "python"
    fake_python.write_text(f'#!/bin/sh\nexec "{sys.executable}" -S "$@"\n')
    fake_python.chmod(0o755)
    site = tmp_path / "site"
    site.mkdir()
    if importable:
        (site / "pro_cli.py").write_text("")

    result = subprocess.run(
        ["bash", "-c", f'set -euo pipefail\nvenv_bin="{bin_dir}"\n{_pro_cli_check()}'],
        capture_output=True,
        text=True,
        env={"PATH": os.environ.get("PATH", ""), "PYTHONPATH": str(site)},
        check=False,
    )

    if importable:
        assert result.returncode == 1
        assert "::error::pro_cli is importable" in result.stdout
    else:
        assert result.returncode == 0, result.stderr


def _step(name: str) -> str:
    (step,) = [s for s in _run_steps() if s["name"] == name]
    return step["run"]


# --- Validate issue input (#365) -------------------------------------------


def _step_names() -> list[str]:
    return [s["name"] for s in _run_steps()]


@pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")
@pytest.mark.parametrize(
    ("value", "ok"),
    [
        ("42", True),
        ("007", True),
        ("1; rm -rf /", False),
        ("$(id)", False),
        ("12 ", False),
        ("", False),
        ("-1", False),
    ],
)
def test_validate_issue_input_step_accepts_only_digits(tmp_path, value, ok):
    """The guard rejects anything but a run of digits (#365). Run in an
    empty cwd so even a guard that let `rm -rf /`-style input through to a
    shell would have nothing of ours to touch."""
    result = subprocess.run(
        ["bash", "-c", _step("Validate issue input")],
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env={"ISSUE_INPUT": value, "PATH": "/usr/bin:/bin"},
        check=False,
    )

    if ok:
        assert result.returncode == 0, result.stdout + result.stderr
        assert "::error::" not in result.stdout
    else:
        assert result.returncode == 1
        assert "::error::The 'issue' input must be a numeric issue number" in result.stdout


def test_issue_input_is_validated_before_any_other_step_uses_it():
    """The guard is only worth anything if it runs first: every step that
    receives `inputs.issue` comes after it (#365)."""
    steps = _steps()
    users = [
        step["name"]
        for step in steps
        if "inputs.issue" in yaml.safe_dump({k: v for k, v in step.items() if k != "name"})
    ]
    assert steps[0]["name"] == "Validate issue input"
    assert users[0] == "Validate issue input"
    assert len(users) > 1  # the check is vacuous if nothing else used it


# --- Commit, push, verify, and publish (#272, #283, #306, #325) ------------

COMMIT_STEP = "Commit, push, verify, and publish the PR via cicaid"
ISSUE_BODY = (
    "## What\nMake the widget faster.\n\n"
    "## Implementation notes\nFILES: app.py\nDONE: the widget renders in under 1s\n"
)
PR_FOOTER = (
    "\n\n---\n<p align=\"center\"><img src=\"https://github.com/leonarduk/issue-worm/raw/"
    "main/docs/logo.jpg\" width=\"48\" alt=\"issue-worm logo\" /><br/><sub>🐛 Opened "
    "automatically by <a href=\"https://github.com/leonarduk/issue-worm\">issue-worm</a>"
    "</sub></p>"
)

_GH_STUB = r"""#!/bin/sh
# Records every call; answers just what the commit step asks for.
printf '%s\n' "$*" >> "$STUB_LOG/gh.calls"
case "$*" in
  "issue view"*"-q .title"*) echo "Speed up the widget" ;;
  "issue view"*"-q .body"*) cat "$STUB_LOG/issue_body" ;;
  "api -X GET repos/"*"/pulls"*) echo 7 ;;
  "api repos/"*"/pulls/7 -q .body") cat "$STUB_LOG/pr_body" ;;
  "api -X PATCH"*)
    for arg in "$@"; do
      case "$arg" in body=@*) cp "${arg#body=@}" "$STUB_LOG/patched_body" ;; esac
    done ;;
  "api -X POST repos/"*"/issues/7/labels"*) ;;
  *)
    # A call the step didn't make when this stub was written: fail loudly
    # so the test is updated, not quietly fed an empty answer (#756).
    echo "gh stub: unexpected call: $*" >&2
    exit 97 ;;
esac
exit 0
"""

_CICAID_STUB = r"""#!/bin/sh
printf '%s\n' "$*" >> "$STUB_LOG/cicaid.calls"
case "$*" in
  "run-ci-checks --all") exit 0 ;;
  "publish-pr --body-file "*|"publish-pr --draft --body-file "*)
    # publish-pr appends `Closes #N` to the body it was given.
    for arg in "$@"; do last="$arg"; done
    { cat "$last"; printf '\nCloses #%s\n' "$ISSUE"; } > "$STUB_LOG/pr_body" ;;
  *)
    echo "cicaid stub: unexpected call: $*" >&2
    exit 97 ;;
esac
exit 0
"""


def _git(cwd, *args):
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout


@pytest.fixture
def action_checkout(tmp_path):
    """A clone of a bare `origin` with one commit, plus PATH stubs for gh,
    cicaid and issue-worm. Real git does the committing and pushing."""
    origin = tmp_path / "origin.git"
    _git(tmp_path, "init", "-q", "--bare", "-b", "main", str(origin))
    work = tmp_path / "work"
    _git(tmp_path, "clone", "-q", str(origin), str(work))
    _git(work, "config", "user.name", "t")
    _git(work, "config", "user.email", "t@example.com")
    (work / "app.py").write_text("x = 1\n")
    _git(work, "add", "app.py")
    _git(work, "commit", "-q", "-m", "init")
    _git(work, "push", "-q", "origin", "HEAD:main")

    stubs = tmp_path / "stubs"
    stubs.mkdir()
    for name, body in (
        ("gh", _GH_STUB),
        ("cicaid", _CICAID_STUB),
        ("issue-worm", "#!/bin/sh\nexit 0\n"),
        ("python", f'#!/bin/sh\nexec "{sys.executable}" "$@"\n'),
    ):
        (stubs / name).write_text(body)
        (stubs / name).chmod(0o755)
    # cicaid's slugify, stubbed so the test doesn't depend on cicaid being
    # installed; `review` (for extract_done_line) is this repo's own.
    pkg = tmp_path / "pypath" / "cicaid_devtools"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("")
    (pkg / "work_on_issue.py").write_text(
        "import re\n"
        "def slugify(text):\n"
        "    return re.sub(r'[^a-z0-9]+', '-', text.lower()).strip('-')\n"
    )
    log = tmp_path / "log"
    log.mkdir()
    (log / "issue_body").write_text(ISSUE_BODY)
    return {"work": work, "stubs": stubs, "log": log, "pypath": pkg.parent}


def _run_commit_step(checkout, close_issue="true"):
    env = {
        "PATH": f"{checkout['stubs']}{os.pathsep}{os.environ.get('PATH', '')}",
        "PYTHONPATH": os.pathsep.join([str(checkout["pypath"]), str(ACTION.parent)]),
        "HOME": str(checkout["work"].parent),
        "GITHUB_WORKSPACE": str(checkout["work"]),
        "ISSUE": "5",
        "REPO": "owner/name",
        "CLOSE_ISSUE": close_issue,
        "GH_TOKEN": "x",
        "STUB_LOG": str(checkout["log"]),
    }
    return subprocess.run(
        ["bash", "-c", _step(COMMIT_STEP)],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )


needs_bash_and_git = pytest.mark.skipif(
    shutil.which("bash") is None or shutil.which("git") is None, reason="needs bash and git"
)


@needs_bash_and_git
@pytest.mark.parametrize("bookkeeping_exists", [False, True], ids=["never-created", "created"])
def test_commit_step_succeeds_with_issue_worm_gitignored(action_checkout, bookkeeping_exists):
    """The dogfood case (#272): the consumer repo gitignores `.issue-worm`,
    so `git reset -- .issue-worm` targets a path git doesn't track - and in
    the never-created case, one that doesn't exist at all. Under `set -e`
    neither may abort the step."""
    work = action_checkout["work"]
    (work / ".gitignore").write_text(".issue-worm/\n")
    _git(work, "add", ".gitignore")
    _git(work, "commit", "-q", "-m", "ignore bookkeeping")
    if bookkeeping_exists:
        (work / ".issue-worm").mkdir()
        (work / ".issue-worm" / "history.jsonl").write_text("{}\n")
    (work / "app.py").write_text("x = 2\n")

    result = _run_commit_step(action_checkout)

    assert result.returncode == 0, result.stdout + result.stderr
    assert _git(work, "show", "--name-only", "--format=", "HEAD").split() == ["app.py"]
    assert _git(work, "ls-remote", "origin", "fix/issue-5-speed-up-the-widget")


@needs_bash_and_git
def test_commit_step_keeps_a_tracked_issue_worm_file_out_of_commit_and_pr_body(action_checkout):
    """A consumer that tracks `.issue-worm/` gets its modification unstaged
    by the reset and left dirty on disk (#272's pinned behaviour). The PR
    body's file list comes from `git diff <base> HEAD`, so that dirty file
    must not appear in it (#283).

    Two independent layers keep it out: the `HEAD` in that diff, and the
    skip-worktree bit the step sets on tracked `.issue-worm` files, which
    hides their working-tree changes from `git diff` too. This test fails
    only when both go (checked by removing them); the next test pins the
    `HEAD` argument on its own."""
    work = action_checkout["work"]
    (work / ".issue-worm").mkdir()
    (work / ".issue-worm" / "history.jsonl").write_text("old\n")
    _git(work, "add", ".issue-worm")
    _git(work, "commit", "-q", "-m", "tracked bookkeeping")
    base = _git(work, "rev-parse", "HEAD").strip()
    (work / ".issue-worm" / "history.jsonl").write_text("new\n")
    (work / "app.py").write_text("x = 2\n")

    result = _run_commit_step(action_checkout)

    assert result.returncode == 0, result.stdout + result.stderr
    committed = _git(work, "diff", "--name-only", base, "HEAD").split()
    assert committed == ["app.py"]
    assert (work / ".issue-worm" / "history.jsonl").read_text() == "new\n"  # left dirty
    body = (action_checkout["log"] / "pr_body").read_text()
    files_section = body.split("**Files changed:**\n", 1)[1].split("\n\n", 1)[0]
    assert [line.removeprefix("- ") for line in files_section.splitlines()] == committed


def test_commit_step_runs_in_strict_mode():
    """The harness below runs the step's own text, so it only exercises
    `set -e` failure semantics - a non-zero `git reset` aborting the step,
    say (#272) - while the step itself turns them on (#757)."""
    first = next(ln.strip() for ln in _step(COMMIT_STEP).splitlines() if ln.strip())
    assert first == "set -euo pipefail"


def test_pr_body_file_list_diffs_against_head_not_the_working_tree():
    """`git diff --name-only <base>` alone compares against the working
    tree, so anything a reset left dirty would be listed as changed; the
    PR lists what was committed (#281, #283)."""
    (line,) = [ln.strip() for ln in _step(COMMIT_STEP).splitlines() if ln.strip().startswith("changed_files=")]
    assert 'git diff --name-only "$base_commit" HEAD' in line


@needs_bash_and_git
@pytest.mark.parametrize(
    ("issue_body", "what"),
    [
        (ISSUE_BODY, "the widget renders in under 1s\n\n**Files changed:**\n- app.py"),
        ("Just make it faster.\n", "**Files changed:**\n- app.py"),
    ],
    ids=["with-done-line", "without-done-line"],
)
def test_commit_step_pr_body_is_exact(action_checkout, issue_body, what):
    """The whole PR body, byte for byte (#306): section order, the blank
    line before the footer, and the footer HTML. publish-pr then appends
    `Closes #N` (stubbed here exactly as it behaves)."""
    work = action_checkout["work"]
    (action_checkout["log"] / "issue_body").write_text(issue_body)
    (work / "app.py").write_text("x = 2\n")

    result = _run_commit_step(action_checkout)

    assert result.returncode == 0, result.stdout + result.stderr
    # The Why excerpt is the issue minus its headings and its
    # ## Implementation notes section.
    why = "Make the widget faster." if issue_body == ISSUE_BODY else "Just make it faster."
    expected = (
        f"## What\n{what}\n\n"
        f"## Why\n{why}\n\n"
        "## Approach\nSingle-pass free-engine build: the coder applied the change and "
        "the Action published it without an LLM-generated summary.\n\n"
        "## Testing\n✓ `cicaid run-ci-checks --all` passed.\n\n"
        "## Checklist\n- [ ] Tests added/updated\n- [ ] Docs updated if needed\n"
        # The checklist's own newline plus the footer's leading "\n\n":
        # two blank lines before the rule, which Markdown renders as one.
        f"- [ ] No breaking changes\n{PR_FOOTER}\n"
        "\nCloses #5\n"
    )
    assert (action_checkout["log"] / "pr_body").read_text() == expected


@needs_bash_and_git
@pytest.mark.parametrize(
    ("close_issue", "closes"),
    [("true", True), ("True", True), ("false", False), ("FALSE", False)],
)
def test_commit_step_close_issue_input_controls_closes_trailer(
    action_checkout, close_issue, closes
):
    """`close-issue: 'false'` keeps `Closes #N` out of both the commit
    message and the PR body publish-pr wrote it into (#325)."""
    work = action_checkout["work"]
    (work / "app.py").write_text("x = 2\n")

    result = _run_commit_step(action_checkout, close_issue=close_issue)

    assert result.returncode == 0, result.stdout + result.stderr
    message = _git(work, "log", "-1", "--pretty=%B")
    assert ("Closes #5" in message) is closes
    patched = action_checkout["log"] / "patched_body"
    if closes:
        assert not patched.exists()
    else:
        assert "Closes #" not in patched.read_text()
        assert "## What" in patched.read_text()
