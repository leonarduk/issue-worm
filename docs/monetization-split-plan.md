# Open-core split: what's public vs private

`issue-worm` is the free, open-source shell of a larger pipeline. The
automated review/implement/verify loop lives in a separate private package,
`issue-worm-core`.

## What's here (public, MIT)

- `cli.py` — the `issue-worm` command-line entry point
- `config.py` — configuration loading
- `workspace.py` — local workspace/repo management
- `history.py` — local run history (`issue-worm history`)
- `version_checker.py` — update checks

This shell works standalone for:
- `issue-worm create` — file a new issue (delegates to `cicaid create-issue`,
  which needs [`cicaid`](https://github.com/leonarduk/cicaid) installed)
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

## Access

`issue-worm-core` is a private repository — see
[leonarduk/issue-worm-core](https://github.com/leonarduk/issue-worm-core)
for access.
