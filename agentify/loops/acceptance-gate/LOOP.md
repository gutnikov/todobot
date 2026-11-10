---
id: acceptance-gate
title: "Approved criteria green, on the approved tests, red before the work"
type: acceptance-loop
status: validated
trigger: [ci]
places: [delivery/gate, delivery/oracle]
run: ./run
budget_seconds: 180
validation: cases
agents: []
parts: [verify]
hitl: []
wiring: [.github/workflows/acceptance-gate.yml jobs.acceptance-gate]
---

# Approved criteria green, on the approved tests, red before the work

## Requirement

In the skeleton task the delivery gate was done by hand in a clean clone: the approved acceptance tests unchanged
(their sha256), the acceptance tests green, the protected paths untouched. Nothing enforced it, and a pull request
could merge with a weakened test or a criterion without one. The user asked for this loop (H4, task:first-loops).
Its place is delivery/gate, and delivery/oracle: it holds the approved spec and tests fixed while the work is done.

## Loop

```
 pull request ──► CI job acceptance-gate ──► ./run . --base origin/<base branch>
                     ├─ every approved requirement: one line per criterion, green | red | no test |
                     │  changed without approval | no red record
                     ├─ every file in tests/acceptance/ that no approved requirement lists: not approved
                     └─ every change since the base to tests/acceptance/ or an approved requirement
                        without an H5 added to agentify/hitl-log.jsonl: changed without approval
                  ──► exit 0 only when every approved criterion is green and nothing is unapproved
```

1. `run` reads `agentify/specs/requirements/*.md` and keeps the ones whose `**Status:**` starts with "approved".
   For each: its criteria (`### <ID>-<n> ...`, without "(dropped)"), the sha256 lines of its "Approved tests"
   block, and whether it records the run "red before the work".
2. It hashes every listed file, lists the files in `tests/acceptance/` that no approved requirement lists, and
   runs the acceptance tests once (pytest with a junit report, `--confcutdir tests/acceptance` so that no
   conftest.py outside the approved files is loaded).
3. Each criterion `<ID>-<n>` gets the tests named `test_<id><n>_...` and a status, the first that applies:
   `changed without approval` (a listed file is missing or differs, or with `--base` an unapproved change),
   `no red record`, `no test`, `red` (a test failed, errored, was skipped or did not run), `green`.
4. With `--base REF` it diffs the working tree against `git merge-base REF HEAD`. A change to `tests/acceptance/**`
   or to a requirement approved before or after is legitimate only when the lines added to
   `agentify/hitl-log.jsonl` since then hold an H5 whose `loop`, `question` or `checked` names the requirement: its
   criteria prefix as a word (`SK`, `SK-1`) or its file's stem (`skeleton`). A new test file goes to the
   requirement its name points to (`test_sk1_*.py`: SK). The developer never writes under `agentify/`, so it cannot
   add the H5 or the new hashes itself.
5. The agent reads the lines: `red` goes back to the code (or the test writer), `no test` and `no red record` to
   the test writer, `changed without approval` and `not approved` to the spec approver.

It is a composite with `parts: [verify]`. `verify` (its own job) runs every test, the acceptance tests included,
and says whether they pass; this gate adds what only the whole can show: that the tests are the approved ones,
cover every approved criterion and were red before the work. It runs the acceptance tests itself because the two
jobs run apart; see CHANGELOG.md.

Limits: the gate cannot tell whether an H5 line is genuine or what its answer was; it trusts that only the
harness and the spec approver write `agentify/`, which nothing but review enforces yet. One H5 naming a requirement
approves every change to it in the pull request. The "red before the work" record is a line of text the test
writer writes; the gate does not rerun the tests on the base.

## Tools

`run` starts `gate.py` with `uv run --quiet --locked --project <repo> python`; pytest reads the repository's
`pyproject.toml`. Validated with uv 0.10.11, Python 3.12.13, pytest 9.2.0 (pytest-timeout 2.4.0) from `uv.lock`, and
git 2.50.1 for `--base`. In CI: the same uv version via `astral-sh/setup-uv`, and the full history
(`fetch-depth: 0`) so that the merge base is there.

## Signal

One line per approved criterion, `<ID>-<n>: <status>`, with what to fix indented under it; one line per path that
is not approved; a summary; exit 1 when it fires, 2 when it cannot run (a base the clone does not have). Real runs:

On today's repository (`run .` and `run . --base origin/main`, both exit 0, 60 s):

```
SK-1: green
SK-2: green
SK-4: green
SK-5: green
acceptance-gate: 4 of 4 approved criteria green; passes
```

The must-fire case `red-test`:

```
AB-1: green
AB-3: red
  tests/acceptance/test_ab3_subtracts.py:8: test_ab3_four_minus_two_is_two failed: assert 6 == 2
acceptance-gate: 1 of 2 approved criteria green; fires
```

The must-fire case `hash-changed`:

```
tests/acceptance/test_ab1_adds.py: changed without approval (agentify/specs/requirements/ab.md approved sha256 bca7a8f5e3d7…, the file has e1543383cae3…)
AB-1: changed without approval
AB-3: changed without approval
acceptance-gate: 0 of 2 approved criteria green; fires
```

## Validation

`loops.py validate acceptance-gate`: 8 cases, 2 runs each, under a second each. Each case is a small project with
a fake requirement `AB` (AB-1, AB-2 dropped, AB-3), a draft requirement the gate must ignore, and tests that do
not start the app. A case with a `base/` folder is a history: `run`, for a folder under its own `cases/` only,
commits `base/` and then the rest into a scratch git repository and gates it with `--base` the first commit, so
the git path CI takes is the one validated.

| Case | Stands for |
|---|---|
| must-fire/no-test | AB-3 approved, no `test_ab3_*` |
| must-fire/hash-changed | a listed test file edited, its approved sha256 not |
| must-fire/red-test | AB-3's test fails |
| must-fire/no-red-record | the requirement does not record its red run |
| must-fire/unlisted-test-file | an acceptance test file no requirement lists |
| must-fire/changed-without-h5 | `--base`: a test and its hash changed, only an H1 added to the log |
| must-pass/all-green | approved, hashes match, red recorded, every test green |
| must-pass/approved-change-with-h5 | `--base`: the same change with an H5 naming AB-1 added |

Also checked on the real history, in a scratch copy: with `test_sk1_start.py` changed and its hash updated in
`skeleton.md`, `run . --base origin/main` fires (`changed without approval` on both files and on SK-1, 2, 4 and
5); with an H5 "Approve the changed SK-1 test?" added to the log, it passes.

## Wiring

- `.github/workflows/acceptance-gate.yml`, job `acceptance-gate`, on every pull request: checkout with
  `fetch-depth: 0`, `astral-sh/setup-uv` (uv 0.10.11, Python 3.12), `uv sync --locked`,
  `agentify/loops/acceptance-gate/run . --base "origin/$BASE_REF"` with `BASE_REF` set from `github.base_ref`
  through `env`. Actions are pinned to commit SHAs. Making `acceptance-gate` a required status check of main is
  the owner's step.
