# Demo transcripts

Text transcripts for the two demo videos linked from the README. These
exist so the narrated content is accessible to deaf and hard-of-hearing
users, discoverable by search engines and in-repo text search, and
durable if the videos are ever re-recorded.

- [Free version demo](#free-version-demo) — `demo-2026-08-31-free.mp4`
- [Pro version demo](#pro-version-demo) — `demo-2026-08-31-pro.mp4`

## Free version demo

**Video:** `docs/assets/demo-2026-08-31-free.mp4`

**Summary:** The free shell takes a single, well-scoped issue and runs
one pass through the coder. AI writes the code; the human does the rest
— no verifier/retry loop, no scheduler.

**Transcript:**

> This is issue-worm, the free shell.
>
> I've got a GitHub issue here that's already scoped — it has an
> `## Implementation notes` section with `FILES:` and `DONE:` lines, so
> the deterministic pre-check will let it through.
>
> I run `issue-worm build` against the issue and the repo. First it
> checks the issue is scoped enough to dispatch. Then it hands the issue
> to the coder — a local Ollama model by default — which writes the
> proposed changes straight into the working tree.
>
> That's the whole run: one pass, no verifier, no retry loop. The coder
> has produced a diff, and it's up to me to review it, run the tests,
> and open the PR myself.
>
> That's the honest trade-off of the free tier: it's genuinely useful
> for a well-scoped task, but you're still in the loop for everything
> after the code is written. If you want AI to run the whole pipeline —
> triage, coding, judging its own failures, and drafting the PR — that's
> issue-worm-pro.

## Pro version demo

**Video:** `docs/assets/demo-2026-08-31-pro.mp4`

**Summary:** issue-worm-pro runs AI through the whole pipeline — triage,
coding, a verifier that judges the coder's own failures, and a drafted
PR description — with a bounded retry loop and a scheduler.

**Transcript:**

> This is issue-worm-pro, the full pipeline.
>
> Same starting point: a GitHub issue. But here the scheduler picks it
> up, and the triage step decides whether it's ready to work — is it
> scoped, is it actionable, does it need more information first?
>
> Once triage passes, the coder writes the changes. Then the verifier
> runs — it checks the coder's work against the issue's `DONE:` criteria
> and the repo's own CI checks. If the verifier fails, its feedback goes
> back to the coder as an Analyser note, and the coder gets another
> attempt. That loop is bounded — three attempts here — so a genuinely
> stuck issue fails cleanly instead of spinning forever.
>
> When the verifier passes, pro drafts the PR description from the issue
> and the diff, opens the PR, and updates the issue with a live progress
> comment as each stage finishes.
>
> So the difference from the free shell is the loop: the free tier
> writes the code once and hands it back to you; pro judges its own
> failures, retries with real feedback, and only opens the PR once the
> verifier is satisfied.

## Notes on accuracy

These transcripts are paraphrased from the narration in each video, not
word-for-word. If the videos are re-recorded, update the transcript to
match.

There are no `.vtt` caption files: cue timings have to come from the
actual audio (the free demo runs about 2m04s and the pro demo about 3m),
so add them only once they can be generated from, or checked against, the
recordings.
