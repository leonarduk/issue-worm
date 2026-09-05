# issue-worm

**Give it an issue. It sorts it out.**

issue-worm turns GitHub issues into pull requests using LLMs. This repo is
the free shell: issue filing, a standalone single-pass `build`, and run
history. The full automated review → implement → verify pipeline with a
verifier/retry loop and a scheduler (`triage`, `poll`) is
[issue-worm-pro](https://github.com/leonarduk/issue-worm-pro), a private
package — see [Access](#access) below.

Installing issue-worm-pro upgrades this shell in place rather than
replacing it: the `issue-worm` command stays the same, and `build` starts
running pro's full pipeline instead of the single pass described here.

## Demo

**This is the free shell** — no subscription, no cloud API key, honest
about what it does and doesn't do: AI writes the code, you do the rest.

<video src="docs/assets/demo-2026-08-31-free.mp4" controls width="720"></video>

For comparison, [issue-worm-pro](https://github.com/leonarduk/issue-worm-pro)
runs AI through the whole pipeline — triage, coding, judging its own
failures, and drafting the PR description:

<video src="docs/assets/demo-2026-08-31-pro.mp4" controls width="720"></video>

Watch both: this free tier is genuinely useful on its own for a
well-scoped task, but pro is what "AI does the whole loop" looks like.

For a deeper look at pro's retry loop and scheduler in action — real
transcripts, a bounded 3-attempt retry with genuine Analyser feedback,
and a real PR opened end to end — see
[docs/demo-2026-08-30-issue-388.md](docs/demo-2026-08-30-issue-388.md).

## What this package does today

- `issue-worm create` — file a new issue, guided interactively (via
  [cicaid](https://github.com/leonarduk/cicaid)).
- `issue-worm build <issue> --repo owner/name` — a deterministic
  (non-LLM) check that the issue is scoped enough to dispatch (an
  `## Implementation notes` section with `FILES:`/`DONE:`), then a single
  pass through a local Ollama coder that writes the proposed changes to
  the working tree. No verifier/retry loop, no scheduler. **With
  issue-worm-pro installed this command runs pro's pipeline instead**, so
  the behaviour described here is what you get on the free tier alone.
- `issue-worm history` — list or inspect past runs recorded by the
  pipeline.
- `issue-worm status` — show runs currently in progress (from the run
  registry state dir) plus the last N completed runs (from
  `issue-worm history`'s own store). `-n/--limit` caps how many completed
  runs are shown (default 10); `--json` emits the whole payload as one
  document. Prints `No active runs.` when nothing is running — on a
  free-tier build, the completed-runs list is often empty too, since only
  issue-worm-pro's scheduler writes to run history; that's normal, not a
  bug.
- `issue-worm triage` / `poll` — parse their flags (so `--help` stays
  accurate) but report themselves unavailable, since the scheduler and
  LLM-driven triage that implement them live in issue-worm-pro.

## Install

```bash
pip install issue-worm
```

## Working with private repositories

`issue-worm build` — the free-tier one; issue-worm-pro's resolves its own
workspace and does not accept `--workspace` — works in a checkout it calls
the workspace, chosen by `--workspace`, else `WORKSPACE_ROOT`, else a
default under `.issue-worm-workspace/`. When that path is missing or empty it is
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

**If those credentials lapse**, what you see depends on where you are
running.

When **stdin is not a terminal** — the Scheduler, CI, or any invocation
with stdin redirected — every prompt is disabled and git fails
immediately:

| Remote | Message |
|---|---|
| HTTPS | `fatal: could not read Username for 'https://github.com': terminal prompts disabled` |
| SSH | `Permission denied (publickey)`, or `Host key verification failed` |

That takes three settings, not one. `GIT_TERMINAL_PROMPT=0` covers only
git's *own* prompt; `GIT_ASKPASS`/`SSH_ASKPASS` are consulted **before**
it, so a process launched from a desktop session could otherwise block on
a GUI dialog; and for an SSH remote git execs `ssh`, which reads a
passphrase from `/dev/tty` directly and never sees any of git's
variables — so `BatchMode=yes` is appended to `GIT_SSH_COMMAND`. Since
the pre-clone above is normally an SSH checkout, that last one is the one
that matters here.

Without them a `git fetch` would wait for typing nobody can see until the
120-second fetch timeout, once per pass, and report a *timeout* — naming
the wrong cause.

Note that stdin is what decides this, not stdout: `issue-worm build |
tee run.log` still has a terminal on stdin and still prompts.

**Run by hand from a terminal**, you get git's normal prompts and can
answer them — but the same 120-second bound applies to the fetch, so
dawdling at the prompt turns into `git fetch origin timed out after
120.0s`. The clone is the exception: it disables prompts unconditionally,
because its bound is ten minutes.

A reused checkout is checked against the repo you asked for: if its
`origin` names a different project the run stops rather than committing
to the wrong codebase. SSH and HTTPS remotes of the same repo count as
the same repo, and a remote the check cannot read or parse — a bare
local path, a `file://` mirror — is allowed through rather than blocked.

| Variable | Effect |
|---|---|
| `WORM_SKIP_REMOTE_CHECK=1` | Downgrade a repository mismatch from an error to a warning. For a deliberate fork-origin workspace, where `origin` is your fork rather than the repo you pass to `--repo`. |

Note that with a fork origin the workspace is refreshed from the *fork's*
`main`, so it is only as current as your last sync — that staleness is
what the check exists to surface.

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
