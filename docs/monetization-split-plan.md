# Open-core split: what's public vs private

`issue-worm` is the free, open-source shell of a larger pipeline. The
automated review/implement/verify loop lives in a separate private package,
`issue-worm-pro` (renamed from `issue-worm-core`; this document originally
described the split under that name).

The split is capability-based: filing issues and inspecting local run history
remain useful without a subscription, while the automation that consumes an
issue, changes a repository, and produces a verified pull request is the paid
component. The public shell does not silently fall back to a reduced or remote
automation service.

## What's here (public, MIT)

- `cli.py` — the `issue-worm` command-line entry point
- `config.py` — configuration loading
- `workspace.py` — local workspace/repo management
- `review.py` — deterministic (non-LLM) check that an issue is scoped
  enough to dispatch
- `coder.py` — a local-Ollama-only coder for the free-tier `build` flow
- `history.py` — local run history (`issue-worm history`)
- `version_checker.py` — update checks

This shell works standalone for:
- `issue-worm create` — file a new issue (delegates to `cicaid create-issue`,
  supplied by this package's `cicaid-devtools` dependency)
- `issue-worm build <issue> --repo owner/name` — heuristic review, then a
  single pass through a local Ollama coder; no verifier/retry loop
- `issue-worm history` — list or inspect past runs

## What's in `issue-worm-pro` (private)

The multi-agent pipeline that turns an issue into a verified PR:

- **Orchestration** — a Coder → Verifier → Analyser retry loop
- **Scheduling** — polling, dispatch, and label-lifecycle management across
  a repo's open issues
- **Agents** — the coder, aider-backed coder, analyser, and triage
  implementations
- Integration with [`cicaid-pro`](https://github.com/leonarduk/cicaid-pro)
  for the underlying LLM calls

`issue-worm triage` and `issue-worm poll` are part of this private
package. Running them from the public shell without `issue-worm-pro`
installed will tell you they aren't available. (`issue-worm build` is a
public-shell command now — see above; it stays a single-pass,
non-scheduled flow, unlike the pro package's full retry loop.)

Keeping the `triage`/`poll` stubs in the public CLI makes the boundary
visible and keeps `--help` useful; it is not a promise that installing
this repository alone enables those commands.

## Access

`issue-worm-pro` is a private repository — see
[leonarduk/issue-worm-pro](https://github.com/leonarduk/issue-worm-pro)
for access. Until a purchase link is published, contact the maintainer through
that repository to ask about availability. No price or access term is implied
by this document.
