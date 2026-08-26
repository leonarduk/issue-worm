"""Command-line interface for issue-worm (free shell).

Issue in, PR out: create files an issue (guided), history reports what
was done, and build runs one standalone, single-pass review+implement
attempt via a local Ollama coder. The full automated review/implement/
verify pipeline with a verifier/retry loop and a scheduler (`triage`,
`poll`) is part of `issue-worm-pro`, a private package — see the README
for access.
"""

import argparse
import json
import logging
import os
import re
import sys

import requests

from config import load_config
from coder import LocalOllamaCoder
from history import DEFAULT_HISTORY_PATH, get_run, load_runs
from review import review_issue
from version_checker import PACKAGE_NAME, check_and_prompt, installed_version
from workspace import (
    FileChange,
    MalformedOutputError,
    WorkspaceError,
    apply_file_change,
    ensure_base_clone,
    parse_coder_output,
)

CREATE_ISSUE_TIMEOUT = 600
GITHUB_API_TIMEOUT = 30
# GitHub owner/repo names: alphanumerics, hyphens, underscores, and dots.
_REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")

LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s %(message)s"

# Commands handed to issue-worm-pro when it is installed. `build` belongs
# here even though this shell implements it too (#372): pro's build is the
# full Coder -> Verifier -> Analyser loop that both repos' READMEs already
# describe, and installing pro has to upgrade `build` the way it already
# upgrades triage/poll. Leaving it out made pro users silently get the
# free-tier single pass instead - the same "ran the wrong implementation"
# failure #352 was about.
_PRO_COMMANDS = ("triage", "build", "poll")

# The subset of _PRO_COMMANDS with no free-tier implementation to fall back
# on: `triage` and `poll` need the scheduler/label-lifecycle machinery that
# only exists in issue-worm-pro, so without it they report themselves
# unavailable. `build` instead falls back to this shell's `_run_build`
# (heuristic review + local Ollama coder, single pass).
_CORE_COMMANDS = ("triage", "poll")

# issue-worm-pro, once installed, ships its implementation as a top-level
# `pro_cli` module (not `cli` - #352: both packages used to declare a
# top-level `cli` module and the same `issue-worm` console script, so
# installing/reinstalling either one silently overwrote the other's files
# on disk with no warning from pip). `_dispatch_to_pro` probes for it and
# delegates when present; when it's genuinely not installed, this message
# explains why.
_CORE_MISSING_MESSAGE = (
    "✗ `issue-worm {command}` is part of issue-worm-pro (the automated "
    "review/implement/verify pipeline), a private package not yet "
    "installable here — see https://github.com/leonarduk/issue-worm-pro "
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


def _try_import_pro_cli():
    """The installed issue-worm-pro `pro_cli` module, or None if absent.

    A plain ``import pro_cli`` wrapped for two reasons: to let `main`
    decide *before* building its own parser whether a core command should
    be dispatched (real flags, real execution) or handled by this shell's
    placeholder subparser (accurate-enough flags for `--help`, "not
    installed" on a real invocation) - see the dispatch note in `main` -
    and to tell "pro_cli itself isn't installed" apart from "pro_cli is
    installed but one of *its* imports is missing." Only the former is a
    `ModuleNotFoundError` naming ``pro_cli``; re-raising anything else
    keeps a genuine installation problem from being reported as "not
    installed" and pointed at the wrong fix.
    """
    try:
        import pro_cli
    except ModuleNotFoundError as exc:
        if exc.name != "pro_cli":
            raise
        return None
    return pro_cli


def _dispatch_to_pro(pro_cli) -> None:
    """Delegate the current invocation to `pro_cli`, already confirmed importable.

    Re-parses the original ``sys.argv`` from scratch via ``pro_cli.main()``
    (which has its own full argparse setup, including flags this shell's
    own placeholder subparsers don't know about) so a genuinely-installed
    issue-worm-pro handles the command end to end (#352).

    ``pro_cli.main()`` always calls ``sys.exit`` itself (with a real exit
    code) rather than returning; ``sys.exit(pro_cli.main())`` forwards
    whatever it returns instead, so a future ``pro_cli.main()`` that
    returns an int instead of exiting doesn't get silently reported as
    success.
    """
    sys.exit(pro_cli.main())


def _core_command_unavailable(command: str) -> int:
    """Report that ``command`` needs issue-worm-pro, not installed here.

    Reached only once this shell's own parser has already resolved
    ``command`` - i.e. `main`'s earlier ``_try_import_pro_cli`` probe came
    back empty, so this shell's placeholder subparser is what actually
    handled ``--help`` and the rest of this command's flags.
    """
    print(_CORE_MISSING_MESSAGE.format(command=command), file=sys.stderr)
    return 2


def _fetch_issue_body(repo: str, issue_number: int) -> str | None:
    """Fetch an issue's body text from the GitHub REST API.

    Uses GITHUB_TOKEN when set (higher rate limit, required for private
    repos); works unauthenticated against public repos otherwise. Returns
    None on any failure rather than raising, so callers can report a clean
    error. Prints a specific message itself for the cases a generic
    "could not fetch" would mislead on (404, rate limiting) — the caller
    only adds its own generic message when this returns None without
    having explained why.
    """
    headers = {"Accept": "application/vnd.github+json"}
    token = os.getenv("GITHUB_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    url = f"https://api.github.com/repos/{repo}/issues/{issue_number}"
    try:
        response = requests.get(url, headers=headers, timeout=GITHUB_API_TIMEOUT)
    except requests.RequestException as exc:
        print(f"✗ GitHub request failed: {exc}", file=sys.stderr)
        return None

    if response.status_code == 404:
        print(f"✗ Issue {repo}#{issue_number} not found", file=sys.stderr)
        return None
    if response.status_code == 403:
        if response.headers.get("X-RateLimit-Remaining") == "0":
            print(
                "✗ GitHub API rate limit exceeded — set GITHUB_TOKEN for a "
                "higher limit, or wait and retry",
                file=sys.stderr,
            )
        else:
            print(
                f"✗ GitHub denied access to {repo}#{issue_number} (403) — "
                "for a private repo, GITHUB_TOKEN may be missing or lack "
                "the required scope",
                file=sys.stderr,
            )
        return None
    try:
        response.raise_for_status()
        return response.json().get("body") or ""
    except (requests.RequestException, ValueError) as exc:
        print(f"✗ GitHub request failed: {exc}", file=sys.stderr)
        return None


def _run_build(args, config: dict) -> int:
    """Single-pass, free-tier `build`: heuristic review + local Ollama
    coder, writing changes straight to the working tree. No verifier/
    retry loop, no scheduler — matches the free shell's scope (#2).
    """
    issue_numbers = list(args.issues) + list(args.issue or [])
    if not issue_numbers:
        print("✗ `build` needs an issue number", file=sys.stderr)
        return 2
    if not args.repo:
        print("✗ `build` needs --repo owner/name", file=sys.stderr)
        return 2
    if not _REPO_RE.fullmatch(args.repo):
        print(
            f"✗ Invalid --repo {args.repo!r} — expected 'owner/name' "
            "(e.g. 'octocat/Hello-World')",
            file=sys.stderr,
        )
        return 2
    issue_number = issue_numbers[0]
    if len(issue_numbers) > 1:
        print(
            f"  (build handles one issue per run; using #{issue_number}, "
            f"ignoring {issue_numbers[1:]})",
            file=sys.stderr,
        )

    body = _fetch_issue_body(args.repo, issue_number)
    if body is None:
        # _fetch_issue_body already printed a specific reason (not found,
        # rate-limited, or the request error) — nothing more to add here.
        return 1

    review = review_issue(body)
    if not review.ready:
        print(f"✗ {review.message}", file=sys.stderr)
        return 1

    if args.dry_run:
        print(f"✓ Issue {args.repo}#{issue_number} is ready to dispatch")
        print(f"  FILES: {', '.join(review.files)}")
        print(f"  DONE: {review.done}")
        return 0

    # config's "workspace_root" defaults to "." (today's cwd) — fine for a
    # repo-in-place run of triage/poll (issue-worm-pro's territory), but
    # `ensure_base_clone` reuses *any* existing git checkout at that path
    # without checking it's actually a checkout of --repo. Since `build`
    # can be invoked from inside an unrelated repo (including issue-worm
    # itself), only trust an explicit --workspace/WORKSPACE_ROOT; otherwise
    # clone into a repo-scoped subdirectory rather than risk writing
    # generated files into whatever repo the CLI happened to be run from.
    # Precedence: --workspace flag > WORKSPACE_ROOT env var > default.
    workspace_root = (
        args.workspace
        or os.getenv("WORKSPACE_ROOT")
        or os.path.join(".issue-worm-workspace", args.repo.replace("/", "_"))
    )
    try:
        repo_path = ensure_base_clone(workspace_root, args.repo)
    except Exception as exc:  # noqa: BLE001 - report, don't crash the CLI
        print(f"✗ Could not prepare workspace: {exc}", file=sys.stderr)
        return 1

    coder_config = config.get("coder_config")
    coder = LocalOllamaCoder(
        endpoint=getattr(coder_config, "ollama_endpoint", None),
        model=getattr(coder_config, "ollama_model", None),
    )
    task = f"FILES: {', '.join(review.files)}\nDONE: {review.done}\n\n{body}"
    output = coder.propose(repo_path, task, review.files)
    if not output:
        print(
            "✗ The Coder produced no output — is Ollama reachable?",
            file=sys.stderr,
        )
        return 1

    try:
        changes: list[FileChange] = parse_coder_output(output, review.files)
        for change in changes:
            apply_file_change(repo_path, change)
    except (MalformedOutputError, WorkspaceError, OSError) as exc:
        print(f"✗ {exc}", file=sys.stderr)
        return 1

    print(f"✓ Applied changes to {len(changes)} file(s) in {repo_path}:")
    for change in changes:
        print(f"  {change.path}")
    return 0


def _build_parser() -> argparse.ArgumentParser:
    """Build the top-level argument parser.

    Factored out of `main()` so `scripts/check_docs_commands.py` can
    introspect the real set of subcommands without duplicating this list.
    """
    parser = argparse.ArgumentParser(
        description="issue-worm: Local LLM issue-to-PR pipeline (free shell)"
    )
    version = installed_version() or "unknown (source checkout)"
    parser.add_argument(
        "--version",
        action="version",
        version=f"{PACKAGE_NAME} {version} (public shell — "
        "triage/poll require issue-worm-pro)",
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
        help="[requires issue-worm-pro] Manual triage pass over open issues",
    )
    triage_parser.add_argument("--repo")
    triage_parser.add_argument("--issue", action="append", type=int)
    triage_parser.add_argument("--repo-path", default=".")
    triage_parser.add_argument("--limit", type=int, default=200)
    triage_parser.add_argument("--dry-run", action="store_true")

    # Only ever reached when issue-worm-pro is absent: with pro installed,
    # `build` is dispatched before this parser is built (#372), so pro's
    # own subparser is what answers --help and defines the real flag set.
    build_parser = subparsers.add_parser(
        "build",
        help="Heuristic review + local Ollama coder, single pass "
        "(no verifier/retry loop — that needs issue-worm-pro)",
    )
    build_parser.add_argument("issues", nargs="*", type=int, metavar="ISSUE")
    build_parser.add_argument("--repo", help="owner/name of the target GitHub repo")
    build_parser.add_argument("--issue", action="append", type=int)
    build_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Only run the review check; don't call the Coder or touch files",
    )
    build_parser.add_argument(
        "--workspace",
        help="Directory to clone --repo into (overrides WORKSPACE_ROOT; "
        "default: .issue-worm-workspace/<repo>). Free-tier only — with "
        "issue-worm-pro installed, `build` runs pro's implementation, "
        "which resolves its own workspace and rejects this flag",
    )

    poll_parser = subparsers.add_parser(
        "poll",
        help="[requires issue-worm-pro] Poll open issues and run the "
        "unified flow forever",
    )
    poll_parser.add_argument("issues", nargs="*", type=int, metavar="ISSUE")
    poll_parser.add_argument("--repo")
    poll_parser.add_argument("--issue", action="append", type=int)
    poll_parser.add_argument("--interval", type=int, default=60)

    return parser


def main():
    """Main CLI entry point."""
    _reconfigure_utf8()

    # triage/poll are issue-worm-pro-only, and `build` is pro-upgraded
    # (#372). When pro is genuinely installed, all three are dispatched
    # here - before this shell's own argparse parser is even
    # built - rather than after parse_args() picks _PRO_COMMANDS out of
    # args.command: that would mean any flag this shell's placeholder
    # subparser for the command doesn't know - most visibly --help - is
    # handled by the *wrong* parser and reports the wrong flag set instead
    # of ever reaching the dispatch (the exact way #352 says this bug
    # survived review). When it's not installed, falls through to this
    # shell's own parser as before, so --help still shows the placeholder
    # subparser's flags and a real invocation reports "not installed" via
    # the args.command branch below - except `build`, which falls through
    # to this shell's own single-pass implementation instead.
    #
    # ``sys.argv[1]`` exactly, not a scan for the first non-flag token:
    # `--version` is the only flag this parser accepts before the
    # subcommand, it takes no value, and argparse's own version/help
    # actions (plus abbreviations like `--vers`, and top-level `-h`) must
    # keep winning over dispatch regardless of what follows them - a scan
    # would send `--vers triage` or `--help triage` to pro_cli instead of
    # printing this shell's own version/help as they always have.
    #
    # check_and_prompt() (this shell's self-update check) is skipped on
    # the dispatch path below, but not lost: pro_cli.main() imports and
    # calls the same check_and_prompt against the same PACKAGE_NAME
    # ("issue-worm"), so the update check still runs exactly once either
    # way.
    if sys.argv[1:2] and sys.argv[1] in _PRO_COMMANDS:
        pro_cli = _try_import_pro_cli()
        if pro_cli is not None:
            _dispatch_to_pro(pro_cli)

    check_and_prompt()

    parser = _build_parser()
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
        # Only reached when issue-worm-pro isn't installed - main's earlier
        # _try_import_pro_cli probe already dispatched and exited if it
        # were (see the note there).
        sys.exit(_core_command_unavailable(args.command))

    elif args.command == "build":
        sys.exit(_run_build(args, config))

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
