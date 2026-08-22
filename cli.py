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

# `build` is implemented standalone in this shell (heuristic review + local
# Ollama coder, single pass, no verifier/retry loop); `triage` and `poll`
# still require the scheduler/label-lifecycle machinery that only exists in
# issue-worm-pro.
_CORE_COMMANDS = ("triage", "poll")

# issue-worm-pro is not yet published as an installable package with a
# stable import contract this shell can call into (it is still the private
# leonarduk/issue-worm-pro repo). Until that contract exists, these
# commands are placeholders: they parse their flags (so --help stays
# accurate) but always report themselves unavailable rather than claiming
# to dispatch into an integration that does not exist yet.
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


def _core_command_unavailable(command: str) -> int:
    """Report that ``command`` needs issue-worm-pro, not installed here.

    See the _CORE_COMMANDS comment above: there is no integration to call
    into yet, so this prints the same message every time rather than
    probing for a package that doesn't exist.
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
    if not _REPO_RE.match(args.repo):
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
        "default: .issue-worm-workspace/<repo>)",
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
