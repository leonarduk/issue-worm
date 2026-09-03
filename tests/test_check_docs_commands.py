"""Tests for scripts/check_docs_commands.py's doc/CLI drift check."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from check_docs_commands import _referenced_commands, _valid_commands  # noqa: E402


def test_valid_commands_matches_known_cli_subcommands():
    assert _valid_commands() == {
        "history",
        "create",
        "triage",
        "build",
        "poll",
        "status",
    }


def test_referenced_commands_extracts_backtick_quoted_names(tmp_path):
    md_file = tmp_path / "doc.md"
    md_file.write_text(
        "Run `issue-worm build <issue>` then check `issue-worm history`.\n"
        "Also see issue-worm build (no backticks, not matched).\n",
        encoding="utf-8",
    )

    assert _referenced_commands(md_file) == {"build", "history"}


def test_referenced_commands_ignores_files_with_no_matches(tmp_path):
    md_file = tmp_path / "doc.md"
    md_file.write_text("Nothing relevant here.\n", encoding="utf-8")

    assert _referenced_commands(md_file) == set()
