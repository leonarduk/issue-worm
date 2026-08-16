# issue-worm

**Give it an issue. It sorts it out.**

issue-worm turns GitHub issues into pull requests using LLMs. This repo is
the free shell: issue filing and run history, with no LLM/agent code of
its own. The automated review → implement → verify pipeline (`triage`,
`build`, `poll`) is [issue-worm-core](https://github.com/leonarduk/issue-worm-core),
a private package — see [Access](#access) below.

## What this package does today

- `issue-worm create` — file a new issue, guided interactively (via
  [cicaid](https://github.com/leonarduk/cicaid)).
- `issue-worm history` — list or inspect past runs recorded by the
  pipeline.
- `issue-worm triage` / `build` / `poll` — parse their flags (so
  `--help` stays accurate) but report themselves unavailable, since the
  pipeline that implements them lives in issue-worm-core.

## Install

```bash
pip install issue-worm
```

## Access

The automated pipeline (multi-agent retry loop, LLM-based issue
triage/review) is the part of issue-worm that's actually hard to
reproduce, and is kept in a private package, issue-worm-core. Contact
the maintainer for access, or watch this repo — a payment link is
planned. See the [open-core split](docs/monetization-split-plan.md) for a
complete list of what is available in this MIT-licensed package and what
requires the private package.

For a component-by-component breakdown of the public shell and private
pipeline, see the [open-core split](./docs/monetization-split-plan.md).

## License

MIT — see [LICENSE](./LICENSE).
