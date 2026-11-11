---
id: acceptance-gate
title: "Approved criteria green, on the approved tests, red before the work"
type: acceptance-loop
status: active
trigger: [ci]
places: [delivery/gate, delivery/oracle]
run: ./run
budget_seconds: 180
validation: cases
agents: []
parts: [verify, spec-check]
hitl: []
wiring: [.github/workflows/acceptance-gate.yml jobs.acceptance-gate]
---

# Approved criteria green, on the approved tests, red before the work

## Requirement

In the skeleton task the delivery gate was done by hand in a clean clone: the approved acceptance tests unchanged
(their sha256), the acceptance tests green, the protected paths untouched. Nothing enforced it, and a pull request
could merge with a weakened test or a criterion without one. The user asked for this loop (H4, task:first-loops).
Its place is delivery/gate, and delivery/oracle: it holds the approved spec and tests fixed while the work is done.

Since 2026-11-11 the spec is set up (`agentify/specs/`, task:specs-setup) and requirements come in the spec part's
format, `agentify/specs/requirements/<KEY>.yaml`, traced to the examples of the event model. The gate reads that
format beside the markdown one, so the oracle stays protected through the conversion of skeleton.md into SK.yaml.

## Loop

```
 pull request ──► CI job acceptance-gate ──► ./run . --base origin/<base branch>
                     ├─ every approved requirement: one line per criterion, green | red | no test | no approver |
                     │  changed without approval | no red record
                     ├─ every file in tests/acceptance/ that no approved requirement lists: not approved
                     └─ every change since the base, without an H5 naming its requirement added to
                        agentify/hitl-log.jsonl: changed without approval. Watched: the acceptance tests, an approved
                        requirement file or a new one that redefines it, a spec element that traces to it
                  ──► exit 0 only when every approved criterion is green and nothing is unapproved
```

1. `run` reads the approved requirement files under `agentify/specs/requirements/`, in two formats.
   - `<KEY>.yaml`, the spec part's, whose `status` is `approved`. Its criteria are its requirements, `SK#1`. The
     approved tests' sha256 and the red record are under `acceptance_tests` (`sha256`, `red_before_work`).
     `approved_by` must name a person. `supersedes: <path>` retires the file it replaces once this one is approved,
     so the two are never read together.
   - `*.md` whose `**Status:**` starts with "approved". Its criteria are `### <ID>-<n> ...`, without "(dropped)". Its
     approved tests are the sha256 lines of its "Approved tests" block, and its red record is the words "red before
     the work".
2. It hashes every listed file, lists the files in `tests/acceptance/` that no approved requirement lists, and
   runs the acceptance tests once (pytest with a junit report, `--confcutdir tests/acceptance` so that no
   conftest.py outside the approved files is loaded).
3. Each criterion gets its tests, then a status, the first that applies: `changed without approval` (a listed file
   is missing or differs, or with `--base` an unapproved change), `no approver`, `no red record`, `no test`, `red`
   (a test failed, errored, was skipped or did not run), `green`.
   - The tests of a criterion are named for it: `test_<key><n>_...` (`SK-1` and `SK#1`: `test_sk1_`).
   - A YAML criterion also has examples: the event model's examples (the event-model layer in
     `agentify/specs/layers.yaml`) whose `reqs:`, or their slice's, name it. A test pins an example down when it is
     tagged with it and titled with it: `test_<tag>_<example name>`. `<tag>` is the slice id (`s02`) or one of the
     example's requirements (`sk2`). The name is the example's in lower case, every run of other characters one
     `_`: "inbox_saved log line has kind and version" is `test_sk2_inbox_saved_log_line_has_kind_and_version`. An
     example no test pins down makes its criterion `no test`, with the names it can take.
4. With `--base REF` it diffs the working tree against `git merge-base REF HEAD`. These changes are legitimate only
   when the lines added to `agentify/hitl-log.jsonl` since then hold an H5 whose `loop`, `question` or `checked`
   names the requirement, by its key or criteria prefix as a word (`SK`, `SK-1`) or its file's stem (`skeleton`):
   - a change to `tests/acceptance/**`. A test file goes to the requirements that list it or whose tag its name
     carries (`test_sk1_*.py`: SK; `test_s02_*.py`: the requirements with examples on S02), else to the one its
     name's prefix names;
   - a change to a requirement file approved before or after;
   - a new or changed requirement file that is not approved but defines a key an approved requirement has: SK.yaml
     beside the approved skeleton.md. It redefines an approved requirement, so it needs that approval too;
   - a change to a spec element of the event model (a slice, an example, a state, a rule) that traces to a
     requirement of an approved YAML file. One line per model file names the elements.
   The developer never writes under `agentify/`, so it cannot add the H5 or the new hashes itself.
5. The agent reads the lines: `red` goes back to the code (or the test writer), `no test` and `no red record` to
   the test writer, `changed without approval`, `no approver` and `not approved` to the spec approver.

It is a composite with `parts: [verify, spec-check]`. `verify` (its own job) runs every test, the acceptance tests
included, and says whether they pass; `spec-check` keeps the spec whole. This gate adds what only the whole can
show: that the tests are the approved ones, pin down every approved criterion and its examples, and were red before
the work. It runs the acceptance tests itself because the jobs run apart; see CHANGELOG.md.

Limits: the gate cannot tell whether an H5 line is genuine or what its answer was; it trusts that only the
harness and the spec approver write `agentify/`, which nothing but review enforces yet. One H5 naming a requirement
approves every change to it in the pull request. The "red before the work" record is a line of text the test
writer writes; the gate does not rerun the tests on the base. A key with a `-` or digits in it makes tags that can
collide (`GH-12#3` and `GH-1#23` are both `gh123`). The glossary and the lifecycles are not traced to requirements,
so the gate does not watch them; spec-check keeps them whole.

## Tools

`run` starts `gate.py` with `uv run --quiet --locked --with pyyaml==6.0.3 --project <repo> python`: the repository's
environment, plus PyYAML beside it (the project's dependencies and `uv.lock` stay as they are). pytest reads the
repository's `pyproject.toml`. Validated with uv 0.10.11, Python 3.12.13, pytest 9.2.0 (pytest-timeout 2.4.0) from
`uv.lock`, PyYAML 6.0.3, and git 2.50.1 for `--base`. In CI: the same uv version via `astral-sh/setup-uv`, and the
full history (`fetch-depth: 0`) so that the merge base is there.

## Signal

One line per approved criterion, `<ID>-<n>: <status>` or `<KEY>#<n>: <status>`, with what to fix indented under
it; one line per path that is not approved; a summary; exit 1 when it fires, 2 when it cannot run (a base the
clone does not have). Real runs on 2026-11-11, on the branch `specs/setup` with SK.yaml written and not approved:

`run .`, exit 0, 60 s (SK.yaml is not approved, so skeleton.md is still the oracle):

```
SK-1: green
SK-2: green
SK-4: green
SK-5: green
acceptance-gate: 4 of 4 approved criteria green; passes
```

`run . --base origin/main`, exit 1: SK.yaml redefines the approved SK, and no H5 approves it yet.

```
agentify/specs/requirements/SK.yaml: changed without approval (changed since origin/main, and no H5 approval naming SK or skeleton was added to agentify/hitl-log.jsonl)
SK-1: changed without approval
SK-2: changed without approval
SK-4: changed without approval
SK-5: changed without approval
acceptance-gate: 0 of 4 approved criteria green; fires
```

The must-fire case `yaml-example-no-test`:

```
CD#1: green
CD#2: no test
  agentify/specs/model/cd.em.yaml: example S02/two minus two is zero has no test: name one test_s02_two_minus_two_is_zero or test_cd2_two_minus_two_is_zero
acceptance-gate: 1 of 2 approved criteria green; fires
```

The must-fire case `red-test`:

```
AB-1: green
AB-3: red
  tests/acceptance/test_ab3_subtracts.py:8: test_ab3_four_minus_two_is_two failed: assert 6 == 2
acceptance-gate: 1 of 2 approved criteria green; fires
```

## Validation

`loops.py validate acceptance-gate`: 15 cases, 2 runs each, under a second each. Each case is a small project with
tests that do not start the app. The markdown ones have a fake requirement `AB` (AB-1, AB-2 dropped, AB-3) and a
draft `DR` the gate must ignore. The YAML ones have `CD.yaml` (CD#1, CD#2) with an event model whose examples trace
to it: CD#1's test is tagged with the requirement (`test_cd1_...`), CD#2's with the slice (`test_s02_...`). They
also have a draft `EF.yaml` the gate must ignore. A case with a `base/` folder is a history: `run`, for a folder
under its own `cases/` only, commits `base/` and then the rest into a scratch git repository and gates it with
`--base` the first commit, so the git path CI takes is the one validated.

| Case | Stands for |
|---|---|
| must-fire/no-test | AB-3 approved, no `test_ab3_*` |
| must-fire/hash-changed | a listed test file edited, its approved sha256 not |
| must-fire/red-test | AB-3's test fails |
| must-fire/no-red-record | the requirement does not record its red run |
| must-fire/unlisted-test-file | an acceptance test file no requirement lists |
| must-fire/changed-without-h5 | `--base`: a test and its hash changed, only an H1 added to the log |
| must-fire/yaml-example-no-test | CD.yaml approved, the example S02/"two minus two is zero" has no test |
| must-fire/yaml-no-approver | CD.yaml `approved` with an empty `approved_by` |
| must-fire/yaml-test-changed-without-h5 | `--base`: CD#1's test and its hash in CD.yaml changed, only an H1 added |
| must-fire/yaml-example-changed-without-h5 | `--base`: an example of CD#2 changed after approval, only an H1 added |
| must-fire/yaml-conversion-without-h5 | `--base`: AB.yaml (not approved) added beside the approved ab.md, no H5 |
| must-pass/all-green | approved, hashes match, red recorded, every test green |
| must-pass/approved-change-with-h5 | `--base`: the same change with an H5 naming AB-1 added |
| must-pass/yaml-all-green | CD.yaml approved, hashes match, red recorded, every example tested and green |
| must-pass/yaml-conversion-with-h5 | `--base`: AB.yaml approved with `supersedes: ab.md` and an H5 naming AB; AB#1, AB#3 green |

The eight markdown cases print the same lines as before the change, byte for byte.

Also checked on the real tests, in a scratch clone of the branch (base: 9d74f7c, origin/main), on 2026-11-11:

- SK.yaml approved (status, approved_by, every question answered), no H5: fires on SK.yaml and on the model
  (`example S01//start replies with greeting within 5 seconds, … and 24 more, tracing to SK`), SK#1–SK#5
  `changed without approval`.
- The same with an H5 "Approve SK.yaml, the converted skeleton (SK)?": `SK#1`, `SK#2`, `SK#4`, `SK#5` green, from
  SK.yaml, its 18 examples each pinned down by one of the 19 approved tests; skeleton.md is not read.
- From there, an example of SK#2 changed with no H5: fires (`example S02/other command is saved as text, tracing to
  SK`). An example renamed so that no test is titled with it: `SK#2: no test`, with the two names it can take.
- Before the change (2026-11-10): with `test_sk1_start.py` changed and its hash updated in `skeleton.md`, `run .
  --base origin/main` fires; with an H5 "Approve the changed SK-1 test?" added to the log, it passes.

## Wiring

- `.github/workflows/acceptance-gate.yml`, job `acceptance-gate`, on every pull request: checkout with
  `fetch-depth: 0`, `astral-sh/setup-uv` (uv 0.10.11, Python 3.12), `uv sync --locked`,
  `agentify/loops/acceptance-gate/run . --base "origin/$BASE_REF"` with `BASE_REF` set from `github.base_ref`
  through `env`. Actions are pinned to commit SHAs. `run` fetches PyYAML 6.0.3 through uv on the way. It is a
  required status check of main.
