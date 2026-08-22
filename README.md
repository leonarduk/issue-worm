# issue-worm

**Give it an issue. It sorts it out.**

issue-worm turns GitHub issues into pull requests using LLMs. This repo is
the free shell: issue filing, a standalone single-pass `build`, and run
history. The full automated review → implement → verify pipeline with a
verifier/retry loop and a scheduler (`triage`, `poll`) is
[issue-worm-pro](https://github.com/leonarduk/issue-worm-pro), a private
package — see [Access](#access) below.

## What this package does today

- `issue-worm create` — file a new issue, guided interactively (via
  [cicaid](https://github.com/leonarduk/cicaid)).
- `issue-worm build <issue> --repo owner/name` — a deterministic
  (non-LLM) check that the issue is scoped enough to dispatch (an
  `## Implementation notes` section with `FILES:`/`DONE:`), then a single
  pass through a local Ollama coder that writes the proposed changes to
  the working tree. No verifier/retry loop, no scheduler.
- `issue-worm history` — list or inspect past runs recorded by the
  pipeline.
- `issue-worm triage` / `poll` — parse their flags (so `--help` stays
  accurate) but report themselves unavailable, since the scheduler and
  LLM-driven triage that implement them live in issue-worm-pro.

## Install

```bash
pip install issue-worm
```

## Working with private repositories

`issue-worm build` works in a checkout it calls the workspace, chosen by
`--workspace`, else `WORKSPACE_ROOT`, else a default under
`.issue-worm-workspace/`. When that path is missing or empty it is
fresh-cloned over **HTTPS with no credentials** — `ensure_base_clone`
builds `https://github.com/<owner>/<name>.git` and nothing attaches a
token.

Git may still satisfy that from the machine's own configuration — a
credential helper, `gh auth setup-git`, or an `insteadOf` rewrite to SSH
— so private HTTPS cloning does work on a host set up that way. Without
one, the clone fails to authenticate.

The setup that does not depend on any of that is to create the checkout
yourself and point the workspace at it; an existing checkout is reused
as-is, whatever protocol it was cloned with:

```bash
git clone git@github.com:owner/private-repo.git /srv/worm/private-repo
```

```bash
# .env
WORKSPACE_ROOT=/srv/worm/private-repo
```

The credentials that clone was made with — an SSH key, a stored HTTPS
token, a credential helper — are what the later fetches and pushes use,
since they run inside that checkout.

A reused checkout is checked against the repo you asked for: if its
`origin` names a different project the run stops rather than committing
to the wrong codebase. SSH and HTTPS remotes of the same repo count as
the same repo. For a deliberate fork-origin workspace, set
`WORM_SKIP_REMOTE_CHECK=1` to downgrade that to a warning.

## Access

The automated pipeline (multi-agent retry loop, LLM-based issue
triage/review) is the part of issue-worm that's actually hard to
reproduce, and is kept in a private package, issue-worm-pro. Contact
the maintainer for access, or watch this repo — a payment link is
planned. See the [open-core split](docs/monetization-split-plan.md) for a
complete list of what is available in this MIT-licensed package and what
requires the private package.

For a component-by-component breakdown of the public shell and private
pipeline, see the [open-core split](./docs/monetization-split-plan.md).

## License

MIT — see [LICENSE](./LICENSE).
