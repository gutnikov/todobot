---
id: spec-check
title: "The spec is whole: every layer's check passes"
type: spec-check
status: active
trigger: [ci]
places: [delivery/oracle]
run: ./run
budget_seconds: 10
validation: cases
agents: []
parts: []
hitl: []
wiring: [.github/workflows/spec-check.yml jobs.spec-check]
---

# The spec is whole: every layer's check passes

## Requirement

The owner chose four spec layers for todobot (hitl-log: H4, task:specs-setup, 2026-11-11): the event model as
the spine, the glossary, decision tables and state machines, for the risks integrations, timers,
rules-and-statuses and permissions. The spec part sets each layer up with the command that checks it, and wires
those checks into this loop so that they run on every change under `agentify/specs/` (its Setup, step 7).

The failure it prevents is a spec handed to the test writer while it is broken: a field that comes from nowhere,
an error no example pins down, a reference to a requirement nobody wrote, a name the glossary forbids, a decision
table with a combination nobody decided, a status nobody can reach or leave. A test writer cannot derive a test
from such a spec without guessing. No such failure has happened yet: the spec is new. Its place is
`delivery/oracle`, where the spec is written and before the tests are.

## Loop

```
 the agent changes agentify/specs/ ──► agentify/loops/spec-check/run .   (by hand, before it hands the spec on)
 pull request or push to main touching agentify/specs/** ──► CI job spec-check ──► run .
      run: specs.py check ──► the check of each layer in agentify/specs/layers.yaml
           ├─ exit 0: every check passes: the spec goes to the test writer, with specs.py delta
           ├─ exit 1: one line per check, and the output of each that fired: fix the spec, or ask
           └─ exit 2: cannot run (no agentify 0.4.0): set AGENTIFY_ROOT
```

1. `run DIR` runs `specs.py check --root DIR`: the preferences, the layers, the requirement files and the trace
   between requirements and spec elements (a `reqs:` reference to a requirement nobody wrote, an approved
   requirement no spec element covers).
2. Then every layer of `DIR/agentify/specs/layers.yaml` with a `check`, unless it is `proposed` or `retired`: the
   command, run in DIR. `<agentify>` becomes agentify's folder, `<spec-check>` this loop's folder, and `python3`
   the loop's own Python, which has PyYAML. Today's layers:

   | Layer | Check | Fires on |
   |---|---|---|
   | `model` (event-model) | `em.py check --strict` | an error of the model (a field from nowhere, a state or input that does not exist, a table overlap), or a test obligation no e2e-ready example pins down |
   | `glossary` | `checks.py glossary` | a name in the model that uses a word the glossary lists under `not`; a term without a meaning, listed twice, or also a synonym |
   | `rules` (decision-tables) | `checks.py tables` | a combination of a table's inputs that no row decides and no `undecided: [{inputs, question}]` asks about; an overlap; a value outside its domain |
   | `lifecycles` (state-machines) | `checks.py lifecycles` | a status no transition reaches; a status other than a final one with no way out; a (status, command) left undecided; a transition or error the model does not have |

3. It prints one line per check, `[<layer>] <command>: passes | fires`, the output of each check that fired
   indented under it, and a summary. Exit 0 when every check passes, 1 when one fires, 2 when it cannot run.
4. The agent fixes what fired. When only a person knows the answer (a table's missing combination, a test
   obligation nobody can pin down), it writes the question into the spec (`undecided`, a slice's `questions`, the
   requirement file's `questions`) and asks the spec approver in one batch.

`checks.py` holds the project's own checks: the three layers' formats have no check that fails. `specs.py check`
only warns on a glossary synonym, `em.py check` lists a table's missing combinations without failing, and nothing
checks a lifecycle. The check proves the spec is whole, not that it is right: a person approves each requirement
file (H5).

Limits: `checks.py lifecycles` reads `agentify/specs/lifecycles.yaml` and the model's read models, slices,
commands and errors. It does not check that a transition's example shows the status after it. SK has no
lifecycle, so the real spec passes it trivially; the made-up Door of the cases is what shows it works.

## Tools

| Tool | Version | How |
|---|---|---|
| agentify `specs.py`, `em.py` | 0.4.0, commit eaff327 | `AGENTIFY_ROOT`, else Claude Code's plugin cache `~/.claude/plugins/cache/agentify/agentify/0.4.0`. `run` refuses another version (exit 2): a newer check could change the verdict unnoticed. In CI a checkout of github.com/gutnikov/agentify at eaff327, whose `parts/` is identical to the plugin cache |
| `checks.py` | this folder | the project's own checks; Python 3.9+ and PyYAML |
| uv, Python, PyYAML | uv 0.10.11, Python 3.12.13, PyYAML 6.0.3 | `run` is `uv run --no-project --python 3.12 --with pyyaml==6.0.3 python spec_check.py`: beside the project, so the project's dependencies and `uv.lock` stay as they are. By hand, the macOS `python3` 3.9.6 has PyYAML 6.0.3 |

## Signal

On the real spec (2026-11-11), exit 0, about 0.6 s:

```
[specs] specs.py check: passes
[model] python3 <agentify>/parts/event-model/scripts/em.py check --strict agentify/specs/model/: passes
[glossary] python3 <spec-check>/checks.py glossary agentify/specs/glossary.yaml agentify/specs/model/: passes
[rules] python3 <spec-check>/checks.py tables agentify/specs/model/: passes
[lifecycles] python3 <spec-check>/checks.py lifecycles agentify/specs/lifecycles.yaml agentify/specs/model/: passes
spec-check: 5 of 5 checks pass; passes
```

The must-fire case `table-gap`, exit 1:

```
[specs] specs.py check: passes
[model] python3 <agentify>/parts/event-model/scripts/em.py check --strict agentify/specs/model/: passes
[glossary] python3 <spec-check>/checks.py glossary agentify/specs/glossary.yaml agentify/specs/model/: passes
[rules] python3 <spec-check>/checks.py tables agentify/specs/model/: fires
    agentify/specs/model: rule inbox-item: no row decides video_note — add the row, or ask: `undecided: [{inputs: [video_note], question: ...}]`
    checks.py tables: 1 problem(s); fires
[lifecycles] python3 <spec-check>/checks.py lifecycles agentify/specs/lifecycles.yaml agentify/specs/model/: passes
spec-check: 4 of 5 checks pass; fires
```

A check that fires prints all its output: for `em.py` that includes its warnings, its coverage and its open
questions, so the line to read is under ERRORS or "not pinned down yet".

## Validation

`loops.py validate spec-check`: 14 cases, 2 runs each, about 0.6 s each against a budget of 10 s. Each case is
`agentify/specs/` copied from the real spec on 2026-11-11 with one change, and `agentify/agentify.md` with the two
spec preferences. The Door of the lifecycle cases is made up: a door is open or closed, with two command slices and
a view, added as `model/door.em.yaml`; it is not todobot's spec.

| Case | The one change | Fires |
|---|---|---|
| must-fire/field-from-nowhere | the log shows `chat_title`, which no event records | model |
| must-fire/error-without-example | /start lists an error, TelegramRefused, that no example pins down | model |
| must-fire/unknown-state | an example starts from a state nobody defined | model |
| must-fire/unknown-requirement | an example traces to SK#9, which no requirement file defines | specs |
| must-fire/synonym-in-model | the read model InboxItem renamed InboxRecord ("record" is under inbox item's `not`) | glossary |
| must-fire/term-without-meaning | the glossary's caption has no meaning | glossary |
| must-fire/table-gap | MessageHas gets video_note, which no row decides and nobody asked about | rules |
| must-fire/table-overlap | the health table decides `answers` twice | model, rules |
| must-fire/unreachable-status | the Door gets a final status, locked, that nothing reaches | lifecycles |
| must-fire/dead-end-status | a closed Door cannot be opened: closed is a dead end and not final | lifecycles |
| must-fire/undecided-pair | nobody decided what OpenDoor does to an open Door | lifecycles |
| must-pass/clean | the real spec | — |
| must-pass/gap-asked | video_note as in table-gap, listed under `undecided` with its question | — |
| must-pass/valid-lifecycle | the Door with a whole lifecycle: both statuses reachable, both with a way out, every pair decided | — |

Each broken copy fires the check of its layer, and only that one, except table-overlap, which `em.py` also
catches. That is the validation of each layer's check (Setup, step 6 of the spec part): it fired on every broken
copy of its layer and passed the real spec, so each layer is `validated` in `layers.yaml`.

Also checked, on 2026-11-11: the CI path, in a scratch clone of the branch with agentify cloned at eaff327 into
`.agentify-plugin` and `AGENTIFY_ROOT=.agentify-plugin`: passes, as above. With that checkout's version changed to
0.5.0, and with `AGENTIFY_ROOT=/nowhere`, `run` exits 2 with the reason. The workflow's first run on GitHub is the pull
request of task:specs-setup.

The final run happens in CI, where the agent doing product work cannot change the job.

## Wiring

- `.github/workflows/spec-check.yml`, job `spec-check`, on every pull request and push to main: it is a required
  status check of main, and a path filter would leave it unreported on the pull requests it skips. Steps: checkout; checkout of gutnikov/agentify at
  eaff327 (0.4.0) into `.agentify-plugin`; `astral-sh/setup-uv` (uv 0.10.11, Python 3.12);
  `agentify/loops/spec-check/run .` with `AGENTIFY_ROOT=.agentify-plugin`. Actions are pinned to commit SHAs, as in
  the other workflows.
- github.com/gutnikov/agentify is public (the owner's choice, 2026-11-11, H3), so the job needs no secret and
  runs on a fork's pull request too.
- By hand, the agent runs `agentify/loops/spec-check/run .` after every change to the spec, before it hands the
  spec to the test writer.
