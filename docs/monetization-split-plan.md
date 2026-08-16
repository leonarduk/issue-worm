# Open-core split: what's public vs private

`issue-worm` is the free, open-source shell of a larger pipeline. The
automated review/implement/verify loop lives in a separate private package,
`issue-worm-core`.

The split is capability-based: filing issues and inspecting local run history
remain useful without a subscription, while the automation that consumes an
issue, changes a repository, and produces a verified pull request is the paid
component. The public shell does not silently fall back to a reduced or remote
automation service.

## What's here (public, MIT)

- `cli.py` — the `issue-worm` command-line entry point
- `config.py` — configuration loading
- `workspace.py` — local workspace/repo management
- `history.py` — local run history (`issue-worm history`)
- `version_checker.py` — update checks

This shell works standalone for:
- `issue-worm create` — file a new issue (delegates to `cicaid create-issue`,
  supplied by this package's `cicaid-devtools` dependency)
- `issue-worm history` — list or inspect past runs

## What's in `issue-worm-core` (private)

The multi-agent pipeline that turns an issue into a verified PR:

- **Orchestration** — a Coder → Verifier → Analyser retry loop
- **Scheduling** — polling, dispatch, and label-lifecycle management across
  a repo's open issues
- **Agents** — the coder, aider-backed coder, analyser, and triage
  implementations
- Integration with [`cicaid-core`](https://github.com/leonarduk/cicaid-core)
  for the underlying LLM calls

`issue-worm triage`, `issue-worm build`, and `issue-worm poll` are part of
this private package. Running them from the public shell without
`issue-worm-core` installed will tell you it isn't available.

Keeping the command stubs in the public CLI makes the boundary visible and
keeps `--help` useful; it is not a promise that installing this repository
alone enables those commands.

## Access

`issue-worm-core` is a private repository — see
[leonarduk/issue-worm-core](https://github.com/leonarduk/issue-worm-core)
for access. Until a purchase link is published, contact the maintainer through
that repository to ask about availability. No price or access term is implied
by this document.
