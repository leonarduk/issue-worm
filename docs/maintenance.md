# Maintenance

## Dependency pins

issue-worm pins three git-only packages by tag, across five locations
(cicaid-devtools appears in three files). The table lists every location a pin
lives in, and is the complete list: nothing else in the repo names a tag for
these packages.

| Dependency | File | What to change |
|---|---|---|
| `cicaid-devtools` | [`requirements.txt`](../requirements.txt) | the `cicaid-devtools[dotenv] @ git+https://github.com/leonarduk/cicaid.git@vX.Y.Z` line |
| `cicaid-devtools` | [`pyproject.toml`](../pyproject.toml) | the same URL in `[project] dependencies` |
| `cicaid-devtools` | [`.github/cicaid-pins.env`](../.github/cicaid-pins.env) | `CICAID_REF=vX.Y.Z` |
| `cicaid-devtools-pro` | [`.github/cicaid-pins.env`](../.github/cicaid-pins.env) | `CICAID_PRO_REF=vX.Y.Z` |
| `ollama-tools` | [`pyproject.toml`](../pyproject.toml) | the `laptop-egpu-llm.git@vX.Y.Z` URL in the `vram` extra |

**Invariant: every `cicaid-devtools` location names the same tag.** If one is
missed, CI tests one version while the review workflow installs another.

[`_ai-pr-review.yml`](../.github/workflows/_ai-pr-review.yml) is **not** a pin
location. It reads `CICAID_REF` and `CICAID_PRO_REF` from the pins file at
install time, and accepts only `main` or `vX.Y.Z` (#614). The pins moved out
of the workflow on purpose (#142, #156). GitHub never lets the default
`GITHUB_TOKEN` push a change under `.github/workflows/`, so an inline pin
could not be bumped by the scheduled update. Do not put a tag back inline.

Two tests enforce the invariant on every CI run, in
[`tests/test_update_dependency_pins.py`](../tests/test_update_dependency_pins.py):

- `test_real_repo_pins_are_readable_and_agree` runs the updater's
  `current_pin` against the real files. It fails if any location is missing
  or names a different tag, and the message lists every file with the tag it
  holds.
- `test_no_workflow_hardcodes_a_dependency_pin` fails if any workflow
  hardcodes a tag for one of these packages.

### Normal path: the scheduled update

[`update-dependencies.yml`](../.github/workflows/update-dependencies.yml) runs
daily. For each dependency that is behind its latest release, it rewrites
every location in the table above with
[`scripts/update_dependency_pins.py`](../scripts/update_dependency_pins.py),
validates the result, and opens a PR. Validation means the test suite for
`cicaid-devtools`, and a smoke install plus a module import for
`cicaid-devtools-pro` and `ollama-tools`. Usually nothing needs to be done by
hand.

### Upgrading cicaid-devtools by hand

1. Rewrite all three locations at once. Never edit them one at a time:

   ```bash
   python scripts/update_dependency_pins.py cicaid-devtools --dry-run
   python scripts/update_dependency_pins.py cicaid-devtools
   ```

   The script always moves to the **latest** release. To pin some other
   version, edit all three rows of the table by hand, then run
   `python -m pytest tests/test_update_dependency_pins.py`.
2. Keep the floor described in the `pyproject.toml` comment, currently at least
   `v0.4.0`. issue-worm-pro depends on both packages, and pip resolves a single
   version across the graph, so going lower makes that install unresolvable.
3. Reinstall and run the suite: `python -m pip install -e ".[dev]"`, then
   `python -m pytest`.
4. For a minor bump or larger, read the
   [cicaid release notes](https://github.com/leonarduk/cicaid/releases) for
   breaking changes.
5. Open a PR. CI fails if the three locations disagree.

### Upgrading cicaid-devtools-pro by hand

The release lookup reads the private `leonarduk/cicaid-pro`, so the script
needs a token with read access to it:

```bash
GITHUB_TOKEN=<PAT with read access to leonarduk/cicaid-pro> python scripts/update_dependency_pins.py cicaid-devtools-pro
```

This rewrites `CICAID_PRO_REF` only. Before merging, check the new version
against the compatibility contract below.

## cicaid-devtools-pro compatibility contract

issue-worm does not depend on `cicaid-devtools-pro`. Only the AI PR-review
workflows install it, together with `cicaid-devtools` at `CICAID_REF`.
`cicaid-devtools-pro` depends on `cicaid-devtools`, and the two share the
`cicaid_devtools` namespace. Use this section to review a `CICAID_PRO_REF`
bump.

### Supported versions

The only supported pair is the one
[`.github/cicaid-pins.env`](../.github/cicaid-pins.env) names on `main`, as
`CICAID_PRO_REF` with `CICAID_REF`.

No wider range is claimed. Other versions have not been validated, and pro's
release notes are in a private repo, so they can't be checked from a public
PR. A version becomes supported only when a PR moves the pin to it and that PR
passes validation (see the rules below). The pins file on `main` is therefore
the record of what has been validated. This document deliberately does not
copy the version: a copy would go stale with the first unattended bump.

### Contract surface

The contract is the set of modules the review workflows run with
`python3 -m`, together with the arguments, outputs and exit codes they rely on.
All of these are in
[`_ai-pr-review.yml`](../.github/workflows/_ai-pr-review.yml), except
`review_module`, which each calling workflow passes in.

| Module | Invoked with | Relied on |
|---|---|---|
| `cicaid_devtools.lib.linked_issue` | PR title + body on stdin | prints the linked issue number, or nothing |
| `cicaid_devtools.lib.review_diff` | `--base-ref`, `--pr-title`, `--issue-body` | prints the diff text sent to the provider |
| `cicaid_devtools.lib.review_discussion` | `--repo`, `--pr-number`, `--provider-name`, optional `--max-chars` | prints prior discussion text |
| `cicaid_devtools.lib.verified_symbols` | `--diff` | prints verified-facts text |
| `cicaid_devtools.lib.deepseek_review` / `gpt_review` | env: `DIFF`, `ISSUE_BODY`, `DISCUSSION`, `VERIFIED_FACTS`, `PR_TITLE`, provider key/model vars | prints the review body; empty output means failure |
| `cicaid_devtools.lib.verdict` | `<review_body.md> <provider_name>` | exit `0` approve, `2` provider outage (soft fail), anything else request changes |
| `cicaid_devtools.lib.review_comment` | `<review_body.md> <provider_name> <workflow_file> <run_url>` | prints the PR comment body |
| `cicaid_devtools.lib.followups` | `<review_body.md>` | prints follow-up titles as JSON |
| `cicaid_devtools.lib.followup_issues` | `<followups.json> <pr_number> <review_body.md>` | creates the follow-up issues |

A `cicaid-devtools-pro` version **breaks the contract** if it does any of the
following:

- removes or renames one of these modules
- removes or renames an argument listed above, or makes a new one required
- changes the meaning of `verdict`'s exit codes
- changes an output format that a later step consumes
- requires a newer `cicaid-devtools` than `CICAID_REF`, so the pair no longer
  resolves together

A version that breaks the contract can only be pinned in the same PR that
updates the workflow to match.

### Rules for moving `CICAID_PRO_REF`

- The new value must pass validation before it merges:
  - **Bot update PR:** the "Smoke-install cicaid-devtools-pro" step in
    `update-dependencies.yml` must pass. That step installs the pair and
    imports every module in the table above, reading the module list from the
    workflows themselves (#572). No other workflow runs on the bot's PRs.
  - **Human PR:** the `ai-review / DeepSeek AI code review` job installs the
    pair and runs the whole surface end to end on the PR. It must not fail at
    install or with empty output.
- If the workflow starts using another module, argument or output, add it to
  the contract surface table in the same PR.

## The floating `nightly` tag

Every night, [`nightly-tag.yml`](../.github/workflows/nightly-tag.yml) moves a
lightweight tag named `nightly` to the tip of `main`. It leaves the tag alone
when `main` hasn't changed. The same workflow runs in the sibling repos
(cicaid, and the pro repos).

- **It is a tag, not a release.** Every version checker and dependency-pin
  updater across these repos reads `releases/latest`, and `nightly` must never
  show up there.
- **The name deliberately contains no digit.**
  - setuptools-scm's default `git describe --match '*[0-9]*'` can't derive a
    package version from it.
  - The release workflow's `v*` tag filter never fires on it.
  - A dated name such as `nightly-2026-01-31` would break the first of these.
- **It is force-moved.** A clone that already has the tag needs
  `git fetch --force --tags` (or
  `git fetch origin +refs/tags/nightly:refs/tags/nightly`) to see its new
  position.

### Nightly integration

An hour later,
[`nightly-integration.yml`](../.github/workflows/nightly-integration.yml)
installs each sibling in `NIGHTLY_DEPS` at the exact commit its `nightly` tag
points to, then runs this repo's tests against them. That answers "does `main`
still work with what upstream merged today?" a day before anyone cuts a
release. The pins themselves are left untouched.

- **Before swapping,** the run asserts that each sibling was installed from
  this repo's own pin. A removed pin therefore fails the run instead of passing
  silently.
- **After swapping,** it asserts that each sibling is at exactly the commit
  recorded in the job summary.
- **On failure,** it opens a tracking issue, or comments on the existing one.
  The next green run closes it.
- **A missing `nightly` tag makes the run test `main`.** This is intentional
  bootstrap behaviour: an upstream repo whose first nightly-tag run hasn't
  happened yet has no tag. The run emits a `::warning::` saying so, and the job
  summary shows `main` in the Ref column. A run that keeps testing `main` long
  after the sibling's nightly-tag workflow should have run means that workflow
  is broken in the sibling, not here.

## CI secrets and release guards

Each of these is optional in the sense that a workflow without it still runs:
the step it feeds warns and skips instead of failing. A fork or Dependabot PR
never gets secrets, which is why none of them is a hard requirement.

| Name | Kind | Used by | What it needs |
|---|---|---|---|
| `CICAID_PRO_TOKEN` | secret | the AI review install, `ci.yml`'s `cicaid-pins` and `pro-dependency-graph` jobs | fine-grained PAT, Contents: read on `leonarduk/cicaid-pro` |
| `ISSUE_WORM_PRO_TOKEN` | secret | `ci.yml`'s `pro-dependency-graph` job | fine-grained PAT, Contents: read on `leonarduk/issue-worm-pro` |
| `DISPATCH_APP_ID` | variable | `release.yml`'s dispatch to issue-worm-pro | the ID of a GitHub App installed on `leonarduk/issue-worm-pro` with Actions: write |
| `DISPATCH_APP_PRIVATE_KEY` | secret | the same | that App's private key |
| `DISPATCH_TOKEN` | secret | the same, only when the App isn't set up | fine-grained PAT, Actions: write on `leonarduk/issue-worm-pro` |

- **`cicaid-pins`** ([`scripts/check_cicaid_pins.py`](../scripts/check_cicaid_pins.py))
  resolves every `.github/cicaid-pins.env` pin with `git ls-remote`. It tells
  a missing tag apart from a token that can't read the private repo, at PR
  time instead of in the review's install step (#391, #444).
- **`pro-dependency-graph`** ([`scripts/pro_dependency_requirements.py`](../scripts/pro_dependency_requirements.py))
  resolves issue-worm-pro's runtime requirements, with issue-worm swapped for
  the PR's checkout, as a `pip install --dry-run`. A pin moved here that pro
  hasn't moved yet fails with `ResolutionImpossible` in the PR that moves it
  (#103, the #100/#101 conflict). The fix is to move both pins together, as
  in the table above.
- **`wheel-contents`** and release.yml's matching step
  ([`scripts/check_wheel_contents.py`](../scripts/check_wheel_contents.py))
  check that the built wheel has every `py-modules` module, the `issue-worm`
  console script and its metadata, and no repo tooling (#197).
- **Dispatch to issue-worm-pro.** Prefer the GitHub App (#513): its token is
  minted per release run and scoped to issue-worm-pro's Actions alone, where
  `DISPATCH_TOKEN` is a long-lived PAT. Once `DISPATCH_APP_ID` and
  `DISPATCH_APP_PRIVATE_KEY` are set, `DISPATCH_TOKEN` can be deleted. The
  dispatch retries once before warning (#515).
