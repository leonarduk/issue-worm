"""Tests for the non-AI review check: does an issue body declare enough
(`## Implementation notes` with `FILES:`/`DONE:`) to dispatch to the
free-tier Coder?
"""

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from review import (
    draft_implementation_notes,
    extract_done_line,
    merge_implementation_notes,
    review_issue,
)


def test_well_scoped_issue_is_ready():
    body = (
        "Some description.\n\n"
        "## Implementation notes\n"
        "FILES: cli.py, tests/test_cli.py\n"
        "DONE: --version prints the package name and exits 0\n"
    )
    result = review_issue(body)

    assert result.ready is True
    assert result.files == ["cli.py", "tests/test_cli.py"]
    assert result.done == "--version prints the package name and exits 0"
    assert result.message == ""


def test_missing_section_is_not_ready():
    result = review_issue("Just a plain description, no structured section.")

    assert result.ready is False
    assert result.files == []
    assert "Implementation notes" in result.message


def test_empty_body_is_not_ready():
    result = review_issue("")

    assert result.ready is False
    assert "Implementation notes" in result.message


def test_none_body_is_not_ready():
    result = review_issue(None)

    assert result.ready is False


def test_section_missing_files_is_not_ready():
    body = "## Implementation notes\nDONE: something\n"

    result = review_issue(body)

    assert result.ready is False


def test_section_missing_done_is_not_ready():
    body = "## Implementation notes\nFILES: a.py\n"

    result = review_issue(body)

    assert result.ready is False


def test_section_with_blank_files_value_is_not_ready():
    body = "## Implementation notes\nFILES: \nDONE: something\n"

    result = review_issue(body)

    assert result.ready is False


def test_section_stops_at_next_heading():
    body = (
        "## Implementation notes\n"
        "FILES: a.py\n"
        "DONE: it works\n"
        "## Something else\n"
        "FILES: b.py\n"
    )

    result = review_issue(body)

    assert result.ready is True
    assert result.files == ["a.py"]


def test_case_insensitive_heading_and_labels():
    body = "## implementation notes\nfiles: a.py, b.py\ndone: works\n"

    result = review_issue(body)

    assert result.ready is True
    assert result.files == ["a.py", "b.py"]


def test_heading_mid_line_is_not_a_section_marker():
    body = "Some prose that mentions ## Implementation notes mid-sentence.\nFILES: a.py\nDONE: works\n"

    result = review_issue(body)

    assert result.ready is False


def test_third_level_heading_is_not_matched():
    body = "### Implementation notes\nFILES: a.py\nDONE: works\n"

    result = review_issue(body)

    assert result.ready is False


def test_second_well_formed_section_is_found_after_an_empty_first_one():
    body = (
        "## Implementation notes\n"
        "(placeholder, fill in before dispatch)\n\n"
        "## Background\n"
        "some context\n\n"
        "## Implementation notes\n"
        "FILES: a.py\n"
        "DONE: works\n"
    )

    result = review_issue(body)

    assert result.ready is True
    assert result.files == ["a.py"]


def test_crlf_line_endings_are_handled():
    body = "## Implementation notes\r\nFILES: a.py\r\nDONE: works\r\n"

    result = review_issue(body)

    assert result.ready is True
    assert result.files == ["a.py"]
    assert result.done == "works"


class _StubCoder:
    def __init__(self, response):
        self.response = response
        self.prompts = []

    def complete(self, prompt):
        self.prompts.append(prompt)
        return self.response


def test_draft_implementation_notes_returns_well_formed_section():
    coder = _StubCoder(
        "## Implementation notes\nFILES: cli.py\nDONE: --version exits 0\n"
    )

    section = draft_implementation_notes("Add a --version flag.", ["cli.py"], coder)

    assert section is not None
    assert "FILES: cli.py" in section
    assert "cli.py" in coder.prompts[0]
    assert "Add a --version flag." in coder.prompts[0]


def test_draft_implementation_notes_rejects_unparseable_response():
    coder = _StubCoder("Sure, I'll get right on that!")

    section = draft_implementation_notes("Add a --version flag.", [], coder)

    assert section is None


def test_draft_implementation_notes_rejects_empty_response():
    coder = _StubCoder("")

    section = draft_implementation_notes("Add a --version flag.", [], coder)

    assert section is None


def test_draft_implementation_notes_returns_none_without_a_coder():
    assert draft_implementation_notes("Add a --version flag.", [], None) is None


def test_draft_implementation_notes_returns_none_for_empty_body():
    assert draft_implementation_notes("", [], _StubCoder("anything")) is None


def test_draft_implementation_notes_survives_coder_exception():
    class _RaisingCoder:
        def complete(self, prompt):
            raise RuntimeError("network exploded")

    assert draft_implementation_notes("task", [], _RaisingCoder()) is None


def test_draft_implementation_notes_heals_past_a_malformed_existing_section():
    """A body whose own `## Implementation notes` is malformed (e.g. FILES:
    present but DONE: missing) must still end up ready once the coder's
    section is appended - `review_issue` already picks the first *ready*
    section from `_SECTION_RE.finditer` (see
    test_second_well_formed_section_is_found_after_an_empty_first_one), so
    the appended section winning over the malformed original is existing
    behaviour, not something `draft_implementation_notes` has to special-
    case. This test pins that the self-heal path actually benefits from it.
    """
    body = "## Implementation notes\nFILES: a.py\n"  # DONE: missing
    coder = _StubCoder("## Implementation notes\nFILES: a.py\nDONE: it works\n")

    section = draft_implementation_notes(body, ["a.py"], coder)

    assert section is not None
    merged = review_issue(f"{body}\n\n{section}")
    assert merged.ready is True
    assert merged.done == "it works"


# --- extract_done_line: the PR goal line in action.yml (#297, #301-#303) ---

_FENCE = "`" * 3


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ("## Implementation notes\nFILES: a.py\nDONE: it works\n", "it works"),
        # Heading and label both match case-insensitively; value is trimmed.
        ("## IMPLEMENTATION NOTES\ndone:   it works  \n", "it works"),
        ("## Implementation notes\r\nDONE: crlf\r\n", "crlf"),
        # Level 3 is not the section; neither is a DONE outside it.
        ("### Implementation notes\nDONE: h3\n", ""),
        ("DONE: top level\n## Implementation notes\nFILES: a.py\n", ""),
        # Only a DONE at the start of a line counts.
        ("## Implementation notes\n- DONE: bullet\n  DONE: indented\n", ""),
        # The section ends at the next level-2 heading.
        ("## Implementation notes\nFILES: a.py\n## Other\nDONE: later\n", ""),
        # First section with a non-empty DONE wins.
        (
            "## Implementation notes\nDONE:\n## Implementation notes\nDONE: second\n",
            "second",
        ),
        (
            "## Implementation notes\nDONE: first\n## Implementation notes\nDONE: second\n",
            "first",
        ),
        ("", ""),
        (None, ""),
    ],
    ids=[
        "basic", "case-insensitive", "crlf", "h3-ignored", "outside-section",
        "indented-ignored", "stops-at-next-heading", "skips-empty-first",
        "first-wins", "empty", "none",
    ],
)
def test_extract_done_line(body, expected):
    assert extract_done_line(body) == expected


@pytest.mark.parametrize(
    "fenced",
    [
        f"{_FENCE}\nDONE: example\n{_FENCE}\n",
        f"{_FENCE}markdown\nDONE: example\n{_FENCE}\n",
        "~~~\nDONE: example\n~~~\n",
        # A shorter or different fence does not close a longer one.
        f"{_FENCE}`\nDONE: example\n{_FENCE}\n~~~~\nDONE: still example\n{_FENCE}`\n",
    ],
    ids=["backticks", "info-string", "tildes", "long-fence"],
)
def test_extract_done_line_skips_fenced_examples(fenced):
    """A DONE: inside a pasted template or example is not the goal (#303)."""
    body = "## Implementation notes\n" + fenced + "DONE: the real goal\n"
    assert extract_done_line(body) == "the real goal"


def test_extract_done_line_unclosed_fence_hides_the_rest():
    body = f"## Implementation notes\n{_FENCE}\nDONE: example\nDONE: also example\n"
    assert extract_done_line(body) == ""


def _action_done_line_command() -> str:
    """The `done_line=` line exactly as action.yml runs it."""
    action = Path(__file__).resolve().parent.parent / "action.yml"
    (line,) = [
        ln.strip()
        for ln in action.read_text(encoding="utf-8").splitlines()
        if ln.strip().startswith('done_line="$(')
    ]
    return line


@pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")
@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ("## Implementation notes\nFILES: a.py\nDONE: Done: it works\n", "Done: it works"),
        (f"## Implementation notes\n{_FENCE}\nDONE: x\n{_FENCE}\n", ""),
        ("no notes at all", ""),
        ("", ""),
    ],
    ids=["extracts", "fenced", "no-section", "empty-body"],
)
def test_action_yml_done_line_runs_under_bash(tmp_path, body, expected):
    """Runs action.yml's own line in bash, with this interpreter as
    `python` (as the action's venv is), so the shell wiring - quoting,
    stdin, the import - is tested, not just the helper (#297, #301)."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "python").symlink_to(sys.executable)
    script = "set -euo pipefail\n" + _action_done_line_command() + '\nprintf "%s" "$done_line"\n'
    env = {
        **os.environ,
        "PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}",
        "PYTHONPATH": str(Path(__file__).resolve().parent.parent),
        "issue_body": body,
    }

    result = subprocess.run(
        ["bash", "-c", 'issue_body="$issue_body"\n' + script],
        capture_output=True, text=True, env=env, check=False,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout == expected


# --- merge_implementation_notes: self-heal without duplicate headings (#264) ---

_HEALED = "## Implementation notes\nFILES: a.py\nDONE: it works\n"


def test_merge_implementation_notes_appends_when_there_is_no_section():
    assert merge_implementation_notes("plain body", _HEALED) == f"plain body\n\n{_HEALED}"


def test_merge_implementation_notes_replaces_a_malformed_section_in_place():
    body = "Intro.\n\n## Implementation notes\nFILES: a.py\n\n## Other\nKeep me.\n"

    merged = merge_implementation_notes(body, _HEALED)

    assert merged == (
        "Intro.\n\n## Implementation notes\nFILES: a.py\nDONE: it works\n## Other\nKeep me.\n"
    )
    assert merged.lower().count("## implementation notes") == 1
    assert review_issue(merged).done == "it works"


def test_merge_implementation_notes_replaces_a_trailing_section():
    body = "Intro.\n\n## IMPLEMENTATION NOTES\nDONE:\n"

    merged = merge_implementation_notes(body, _HEALED)

    # Ends with the drafted section's own newline, as the append path does.
    assert merged == "Intro.\n\n" + _HEALED
    assert review_issue(merged).ready is True


def test_merge_implementation_notes_collapses_every_malformed_section():
    """review_issue looks at every section, so a body can carry several
    malformed ones; all of them give way to the single drafted section."""
    body = (
        "Intro.\n\n## Implementation notes\nFILES: a.py\n\n## Context\nKeep me.\n\n"
        "## Implementation notes\nDONE:\n\n## Trailer\nKeep me too.\n"
    )

    merged = merge_implementation_notes(body, _HEALED)

    assert merged == (
        "Intro.\n\n## Implementation notes\nFILES: a.py\nDONE: it works\n"
        "## Context\nKeep me.\n## Trailer\nKeep me too.\n"
    )
    assert merged.lower().count("## implementation notes") == 1
    assert review_issue(merged).done == "it works"
