"""Fail CI if a Markdown doc references an `issue-worm <command>` that
doesn't exist in the CLI (docs drift protection, see issue #17).

Only matches the backtick-quoted style already used throughout this
repo's docs (`` `issue-worm build` ``) — deliberately narrow, so prose
that merely mentions a word doesn't produce false positives.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
_COMMAND_REF_RE = re.compile(r"`issue-worm ([a-z][a-z-]*)")


def _valid_commands() -> set[str]:
    sys.path.insert(0, str(REPO_ROOT))
    import cli  # noqa: E402 - path must be set up first

    parser = cli._build_parser()
    subparsers_action = next(
        action
        for action in parser._actions
        if action.dest == "command"
    )
    return set(subparsers_action.choices)


def _referenced_commands(md_path: Path) -> set[str]:
    text = md_path.read_text(encoding="utf-8")
    return set(_COMMAND_REF_RE.findall(text))


def main() -> int:
    valid = _valid_commands()
    problems: list[str] = []

    for md_path in sorted(REPO_ROOT.rglob("*.md")):
        for command in sorted(_referenced_commands(md_path)):
            if command not in valid:
                problems.append(
                    f"{md_path.relative_to(REPO_ROOT)}: references "
                    f"`issue-worm {command}`, which is not a real command "
                    f"(known commands: {', '.join(sorted(valid))})"
                )

    if problems:
        print("Doc/CLI drift detected:", file=sys.stderr)
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
        return 1

    print(f"All documented commands exist in the CLI ({', '.join(sorted(valid))}).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
