# Per-check `paths` in `.cicaid-checks.toml`

When issue-worm verifies an attempt, it can run only the CI checks the change
actually touches. A target repo opts in per check with a `paths` key on a
`[[checks]]` entry in its `.cicaid-checks.toml`. `cicaid run-ci-checks` itself
ignores the key, so adding it changes nothing about how the PR's own CI runs.

```toml
[[checks]]
name = "frontend"
commands = ["npm --prefix frontend run lint", "npm --prefix frontend test -- --run"]
paths = ["frontend/**", "package.json"]

[[checks]]
name = "backend"
commands = ["pytest"]
paths = ["backend/**", "tests/**", "pyproject.toml"]

[[checks]]
name = "deps"          # no paths: always runs
commands = ["pip check"]
```

A check runs when any changed path matches one of its globs.

## Glob semantics

These are not quite `fnmatch` or `.gitignore` rules. They match
`workspace._path_matches`:

| Rule | Example |
|---|---|
| **Case-insensitive.** The glob and the path are both lowercased first, on every OS, including case-sensitive Linux CI. | `Frontend/**` matches `frontend/src/a.ts` |
| **`*` crosses `/`.** There is no separate "one directory level" wildcard. | `frontend/*` matches `frontend/src/deep/a.ts` |
| **A leading `**/` also matches at the repo root.** It does not require a directory component. | `**/*.sh` matches `run.sh` and `scripts/bash/run.sh` |
| Paths are repo-relative, with `/` separators. | `tests/**`, not `./tests/**` |

## Which checks run

- **A check without `paths`** always runs.
- **A check whose `paths` is not a list** also always runs, for example a bare string such as `paths = "frontend/**"`. A malformed value never causes a check to be skipped.
- **A check with `paths = []`** claims no change. It is skipped whenever any other check is selected.
- **Everything runs, exactly as without the key, when:**
  - no check declares `paths`;
  - the config can't be read or parsed;
  - the selection would be *no* check, because nothing claims the change; or
  - the selection would be *every* check.

  A change that nothing claims is therefore still fully checked.

When only some checks run, the verifier output starts with a note naming the
checks it ran and the ones it skipped.
