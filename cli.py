"""Command-line interface for issue-worm (free shell).

Issue in, PR out: create files an issue (guided), history reports what
was done. The automated review/implement/verify pipeline (`triage`,
`build`, `poll`) is part of `issue-worm-core`, a private package — see
the README for access. This shell works standalone for `create` and
`history` with no private dependency installed.
"""

import argparse
import json
import logging
import sys

from config import load_config
from history import DEFAULT_HISTORY_PATH, get_run, load_runs
from version_checker import check_and_prompt

CREATE_ISSUE_TIMEOUT = 600

LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s %(message)s"

_CORE_COMMANDS = ("triage", "build", "poll")

# issue-worm-core is not yet published as an installable package with a
# stable import contract this shell can call into (it is still the private
# leonarduk/issue-worm-core repo). Until that contract exists, these
# commands are placeholders: they parse their flags (so --help stays
# accurate) but always report themselves unavailable rather than claiming
# to dispatch into an integration that does not exist yet.
_CORE_MISSING_MESSAGE = (
    "✗ `issue-worm {command}` is part of issue-worm-core (the automated "
    "review/implement/verify pipeline), a private package not yet "
    "installable here — see https://github.com/leonarduk/issue-worm-core "
    "for access."
)


def _configure_logging(config: dict) -> None:
    """Configure stdlib logging once per CLI invocation.

    Timestamped records go to stderr — never stdout, which is reserved
    for command output that tests and scripts parse.
    """
    level_name = (config.get("log_level") or "INFO").upper()
    level = getattr(logging, level_name, None)
    if not isinstance(level, int):
        level = logging.INFO
    logging.basicConfig(level=level, format=LOG_FORMAT, force=True)


def _reconfigure_utf8() -> None:
    """Force UTF-8 on stdout/stderr so Unicode glyphs survive Windows pipes."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass


def _core_command_unavailable(command: str) -> int:
    """Report that ``command`` needs issue-worm-core, not installed here.

    See the _CORE_COMMANDS comment above: there is no integration to call
    into yet, so this prints the same message every time rather than
    probing for a package that doesn't exist.
    """
    print(_CORE_MISSING_MESSAGE.format(command=command), file=sys.stderr)
    return 2


def main():
    """Main CLI entry point."""
    _reconfigure_utf8()
    check_and_prompt()

    parser = argparse.ArgumentParser(
        description="issue-worm: Local LLM issue-to-PR pipeline (free shell)"
    )
    subparsers = parser.add_subparsers(dest="command", help="Subcommand to run")

    history_parser = subparsers.add_parser(
        "history", help="List or inspect past runs"
    )
    history_parser.add_argument(
        "--task-id", help="Show full detail for a specific task_id"
    )
    history_parser.add_argument(
        "--limit", type=int, default=20, help="Max runs to list, most recent first"
    )
    history_parser.add_argument(
        "--history-path",
        default=DEFAULT_HISTORY_PATH,
        help="Path to the JSONL run history file",
    )

    create_parser = subparsers.add_parser(
        "create", help="File a new issue (interactive, guided by cicaid)"
    )
    create_parser.add_argument(
        "--model-source",
        choices=["local", "cloud", "remote", "claude"],
        help="LLM to use for drafting; omitted chooses interactively",
    )

    triage_parser = subparsers.add_parser(
        "triage",
        help="[requires issue-worm-core] Manual triage pass over open issues",
    )
    triage_parser.add_argument("--repo")
    triage_parser.add_argument("--issue", action="append", type=int)
    triage_parser.add_argument("--repo-path", default=".")
    triage_parser.add_argument("--limit", type=int, default=200)
    triage_parser.add_argument("--dry-run", action="store_true")

    build_parser = subparsers.add_parser(
        "build",
        help="[requires issue-worm-core] Run the unified review/implement/"
        "verify flow once",
    )
    build_parser.add_argument("issues", nargs="*", type=int, metavar="ISSUE")
    build_parser.add_argument("--repo")
    build_parser.add_argument("--issue", action="append", type=int)
    build_parser.add_argument("--dry-run", action="store_true")

    poll_parser = subparsers.add_parser(
        "poll",
        help="[requires issue-worm-core] Poll open issues and run the "
        "unified flow forever",
    )
    poll_parser.add_argument("issues", nargs="*", type=int, metavar="ISSUE")
    poll_parser.add_argument("--repo")
    poll_parser.add_argument("--issue", action="append", type=int)
    poll_parser.add_argument("--interval", type=int, default=60)

    args = parser.parse_args()

    config = load_config()
    _configure_logging(config)

    if args.command == "create":
        import subprocess

        repo_path = config.get("workspace_root", ".")
        command = ["cicaid", "create-issue"]
        if args.model_source:
            command += ["--model-source", args.model_source]
        try:
            result = subprocess.run(
                command, cwd=repo_path, timeout=CREATE_ISSUE_TIMEOUT
            )
        except subprocess.TimeoutExpired:
            print(
                f"✗ cicaid create-issue timed out after {CREATE_ISSUE_TIMEOUT}s "
                "— is it waiting for input?",
                file=sys.stderr,
            )
            sys.exit(1)
        except OSError as exc:
            print(
                f"✗ Failed to run cicaid create-issue: {exc}. "
                "Install cicaid (`pip install cicaid-devtools`) first.",
                file=sys.stderr,
            )
            sys.exit(1)
        if result.returncode != 0:
            print(
                f"✗ cicaid create-issue exited with code {result.returncode}",
                file=sys.stderr,
            )
        sys.exit(result.returncode)

    elif args.command in _CORE_COMMANDS:
        sys.exit(_core_command_unavailable(args.command))

    elif args.command == "history":
        if args.task_id:
            run = get_run(args.task_id, history_path=args.history_path)
            if run is None:
                print(f"✗ No run found with task_id '{args.task_id}'", file=sys.stderr)
                sys.exit(1)
            print(json.dumps(run, indent=2))
            sys.exit(0)

        runs = load_runs(args.history_path)
        if not runs:
            print("No runs recorded yet.")
            sys.exit(0)

        for run in runs[-args.limit :]:
            icon = "✓" if run.get("status") == "completed" else "✗"
            print(
                f"{icon} {run.get('task_id')}  [{run.get('source')}]  "
                f"{run.get('status')}  {run.get('description')}"
            )
        sys.exit(0)
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
