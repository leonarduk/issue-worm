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
  pass through a coder that writes the proposed changes to the working
  tree — a local Ollama instance by default, or a cloud/remote LLM if
  configured (see [Coder configuration](#coder-configuration) below). No
  verifier/retry loop, no scheduler. **With issue-worm-pro installed this
  command runs pro's pipeline instead**, so the behaviour described here
  is what you get on the free tier alone. The GitHub Action wraps this
  command with a single `cicaid run-ci-checks --all` verifier before
  publishing, but the `build` command itself does not run those checks.
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

issue-worm isn't published on PyPI. Install the latest release wheel
directly from GitHub Releases:

```bash
pip install https://github.com/leonarduk/issue-worm/releases/download/v0.2.5/issue_worm-0.2.5-py3-none-any.whl
```

`scripts/bump_readme_version.py`, run by
[the release workflow](.github/workflows/release.yml), keeps this URL in
sync with the latest tag on every release.

## Coder configuration

`build`'s coder is picked at run time by `CODER_MODEL_SOURCE`, read via
`config.py` and dispatched by `coder.build_coder` (`coder.py`):

| `CODER_MODEL_SOURCE` | Talks to | Required env vars |
|---|---|---|
| `local` (default) | A local/self-hosted Ollama instance's `/api/generate`. | `CODER_TARGETS` (see below); optionally `CODER_OLLAMA_ENDPOINT` / `CODER_OLLAMA_MODEL` to override per role. |
| `remote` | Any OpenAI-compatible `/v1/chat/completions` endpoint — OpenAI itself, a self-hosted vLLM/SGLang box, or an Ollama instance serving the OpenAI API. | `REMOTE_LLM_ENDPOINT` (no trailing `/v1` — that's appended automatically), `REMOTE_LLM_MODEL`, `REMOTE_LLM_API_KEY`. |
| `cloud` | DeepSeek's API (`https://api.deepseek.com`), which is itself OpenAI-compatible, so it reuses the same `remote` client with DeepSeek's endpoint/model as the default. | `DEEPSEEK_API_KEY`; optionally `DEEPSEEK_MODEL` (default `deepseek-v4-flash`) and `CODER_MAX_TOKENS` (output-token cap sent as `max_tokens`; default `32768` for `cloud`, not sent for `remote` unless set). |
| `claude` | Not implemented by this free engine's `build` coder yet. Setting it fails fast with an explanatory error rather than silently falling back to `local`. | — |

An unset `CODER_MODEL_SOURCE` defaults to `local` — today's original
behaviour, unchanged. Setting `remote` or `cloud` without its required env
var(s) fails the build immediately with a message naming the missing
variable, rather than constructing a coder that talks to an endpoint that
isn't there.

This is what makes `remote`/`cloud` usable on a GitHub-hosted runner,
which has no local Ollama reachable — see the Action's `runs-on` options
below.

## GitHub Action

This repo also ships itself as a **composite action** ([`action.yml`](action.yml))
that runs the free engine against one issue and opens a PR from the
result. It deliberately does *not* set `runs-on` — the calling job
chooses the runner, so the same action works unmodified on GitHub-hosted
and self-hosted runners:

```yaml
on:
  issues:
    types: [labeled]

concurrency:
  group: issue-worm-${{ github.event.issue.number }}
  cancel-in-progress: false

permissions:
  contents: read

jobs:
  build:
    #
