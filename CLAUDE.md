# CLAUDE.md

This file gives Claude-style coding agents a fast, practical overview for working in issue-worm.

## What this is
issue-worm turns GitHub issues into pull requests using LLMs. This repo is the **free shell**
only — issue filing (`create`) and run history (`history`), with no LLM/agent code of its own.
The automated review → implement → verify pipeline (`triage`, `build`, `poll`) lives in the
private `issue-worm-core` package; those subcommands parse their flags here but always report
themselves unavailable.

## Install / run
```bash
pip install issue-worm
issue-worm create    # file a new issue, interactively via cicaid
issue-worm history   # list/inspect past runs from .issue-worm/history.jsonl
```
The `issue-worm` entry point maps to `cli:main` (see `pyproject.toml`). `create` shells out to
the `cicaid` CLI (from `cicaid-devtools`), which must be on PATH.

## Tests
```bash
pip install -e ".[dev]"   # or: pip install -r requirements.txt pytest
pytest
```
`pyproject.toml` configures pytest with `testpaths = ["tests"]` and `python_files = ["test_*.py"]`.
Tests exist for each module: `test_cli.py`, `test_config.py`, `test_history.py`,
`test_version_checker.py`, `test_workspace.py`.

## Structure
- `cli.py` — argparse CLI entry point (`main`); dispatches `create`/`history`, and stubs
  `triage`/`build`/`poll` with an "unavailable, see issue-worm-core" message.
- `config.py` — loads config from env vars / `.env` (via `python-dotenv`): coder backend,
  role configs (model source, Ollama endpoint/model), MCP doc-lookup settings, workspace paths.
- `history.py` — append-only JSONL log of TaskRuns at `.issue-worm/history.jsonl` (single
  writer, lock-guarded appends, no schema/migration).
- `version_checker.py` — checks GitHub releases for a newer issue-worm version and can install
  it; adapted from cicaid's own version checker but scoped to this repo's releases.
- `workspace.py` — workspace reset/retry between revision attempts, applying Coder output
  (diff or full-file write), invoking CI checks, and rollback on failure/interruption. No LLM
  calls happen in this module; branch creation is out of scope (handled by cicaid beforehand).

## CI
`.github/workflows/` includes `pr-lint.yml` (every PR body must reference an issue, e.g.
`Closes #123`), `workflow-lint.yml` (actionlint over the workflow files), plus `codeql.yml`,
`dependency-review.yml`, and AI PR-review workflows (`_ai-pr-review.yml`, `gpt-pr-review.yml`,
`deepseek-pr-review.yml`). There is no CI workflow that runs pytest — run it locally.

## The `cicaid` CLI

This repo uses the `cicaid` CLI (package `cicaid-devtools`) for GitHub issue/PR
plumbing and CI checks — commands like `cicaid sync-issues`, `work-on-issue`,
`run-ci-checks`, `local-review`, `pr-review`, etc.

If you need to check how a `cicaid` command actually behaves, its source is
checked out locally at:
```
C:\Users\steph\workspace\GitHub\cicaid\cicaid        (free/public commands)
C:\Users\steph\workspace\GitHub\cicaid\cicaid-core   (LLM-backed commands:
  triage-issues, review-issue, create-issue, local-review, pr-review,
  commit-and-push, implement-issue-with-aider, clear-ai-slop-issues)
C:\Users\steph\workspace\GitHub\cicaid\cicaid.wiki   (wiki docs)
```
Each of those has its own CLAUDE.md with an accurate command/module map —
read the relevant one before guessing at flags or behavior instead of
inferring from this repo's usage alone. Note: `cicaid` and `cicaid-core`
install as the same package name/entry point, so only one is ever active in
a given venv at a time — don't assume both are simultaneously importable.

This repo has no `.cicaid-checks.toml` of its own, so `cicaid run-ci-checks`
here falls back to `DEFAULT_CHECKS` (allotmint's own pytest/npm/CDK check
list) — which does not describe this repo's actual checks. Don't trust
`cicaid run-ci-checks` output here as a description of this repo's CI; use
the `flake8`/`pytest` commands above instead.

## Watch out for
- `config.py` loads secrets/settings from a local `.env` via `load_dotenv()` — never commit
  `.env` or print its contents.
- Don't implement `triage`/`build`/`poll` logic here; that pipeline intentionally lives in the
  private `issue-worm-core` repo and this shell only stubs the flags.
- PRs need an issue reference in the body (`Closes #N`, `Fixes #N`, `Resolves #N`, `Refs #N`,
  or `Relates to #N`) or `pr-lint.yml` will fail.
