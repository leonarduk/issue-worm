"""Tests for the non-AI review check: does an issue body declare enough
(`## Implementation notes` with `FILES:`/`DONE:`) to dispatch to the
free-tier Coder?
"""

from review import review_issue


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
