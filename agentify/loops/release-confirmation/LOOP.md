---
id: release-confirmation
title: "Release confirmation: the change is there, it happened, nothing broke"
type: release-confirmation
status: active
trigger: [production]
places: [delivery/confirmation, remediation/confirmation, maintenance/confirmation]
run: ./run
budget_seconds: 5
validation: cases
agents: []
parts: []
hitl: []
wiring: [.github/workflows/release-confirmation.yml jobs.release-confirmation]
---

# Release confirmation: the change is there, it happened, nothing broke

## Requirement

The skeleton's release (caba438, PR #1) failed twice before it worked, and the third attempt was confirmed by hand
(`agentify/releases.jsonl`):

- b5eacc16: the deploy failed, a config error (two regions on the Hobby plan);
- d4e7bd51: the deploy ran without its migration (railway.json's preDeployCommand was ignored), and Railway's
  healthcheck still passed it;
- fc7d52ae: confirmed by the agent reading `railway logs` and `/healthz` by hand.

These are the map's pain "release config" and the gap at the confirmation phase. The owner chose this loop with
verify and acceptance-gate (hitl-log: H4, task:first-loops). It must catch a deploy that failed, a deploy without
its migration, production not running the commit, the change's signal never appearing, and new errors in the new
version. Its places are `delivery/confirmation`, `remediation/confirmation` and `maintenance/confirmation`. The pain
sits at `delivery/release`, but nothing there reads a deploy's result: Railway's healthcheck passed d4e7bd51. The
confirmation is the earliest phase that does.

## Loop

```
 push to main touching Railway's watch paths ──► Railway builds and deploys the commit
   └─► GitHub Actions: release-confirmation (the same paths)
         collect --sha <sha> --out snapshot   wait for the deployment and the window, save the snapshot
         run snapshot                         one line per check; exit 0 confirmed, 1 not confirmed, 3 inconclusive
         job summary + artifact release-confirmation-<sha>
   └─► the agent reads the verdict ──► confirmed: a `confirm` line in agentify/releases.jsonl, done
                                   ├─► not confirmed: roll back or fix forward → remediation, the snapshot as evidence
                                   └─► inconclusive: gh workflow run … -f sha=<sha>, a fresh window from the
                                       run's start; the owner sends /start and one message into it
```

**Wait in the wiring, judge in `run`.** `collect` does everything that waits or needs the network. `run` only reads
the snapshot, so it is fast and deterministic, and recorded windows are its cases.

`collect` (the wiring's part):

1. Waits until `railway deployment list` has a deployment of the service for the commit (its newest one) and that
   deployment reaches a final status: SUCCESS, FAILED, CRASHED, REMOVED or SKIPPED. The limit is
   `--deploy-timeout-minutes` (10; a build takes 1–2 min). Railway creates no GitHub deployments, so collect polls Railway.
2. Finds the previous version: the newest earlier deployment of another commit that has log lines. For caba438 there
   is none, since all four deployments are of caba438.
3. Works out the signals. It reads the `prod:` lines (`` `event` = `start` ``) of the files under
   `agentify/specs/requirements/` changed between the previous version's commit and this one. On the first release
   that is every requirement file. If no requirement changed, there is no signal.
4. Waits out the window. On a push, it starts at the new version's `server_started` line, so the build does not
   count. On a `workflow_dispatch`, it starts when the run started (`--window-start`, the job's first step), or at
   `server_started` if that is later. A re-run therefore opens a fresh window, and a synthetic run sent after it
   counts. The window lasts at least `--min-minutes` (10) and until each signal has `--min-events` (1) lines with
   the version, up to `--cap-minutes` (60). /healthz is asked at the start and at the end. If the deployment stops
   being SUCCESS, the window ends there.
5. Saves `meta.json` (the deployment, its status at the end, the previous version, the signals, the /healthz
   answers, the window with `start_from`, the baseline, the parameters), `new.jsonl` and `previous.jsonl`. `new.jsonl`
   holds the deployment's lines up to its `server_started`, with the pre-deploy migration, then the window's lines.
   `previous.jsonl` holds the previous version's lines over the same length of time just before the window.

**Privacy.** The repository is public, and so are the job's log, summary and artifact. `collect` reduces every line
before it is written or printed: `time` (Railway's stamp, ms), `level`, `event`, `version`, `logger` and `error`.
`error` is an exception's type name, from the line's `error` field or the last line of its traceback, and only when
it looks like one (`ProgrammingError`, `asyncpg.exceptions.InvalidPasswordError`). A value that is not a name becomes
`other`. Everything else is dropped: `user_id`, `kind`, messages, request lines, addresses, headers, tracebacks.

`run` (the judge) prints one line per check or item, `<item>: <status> — <what was seen and what to do>`. Lines
starting with `#` only describe the window. These checks decide the verdict:

| # | Item | Status when it fires | Judged on |
|---|---|---|---|
| 1 | `deploy` | `deploy failed` | the deployment's final status is not SUCCESS, it never appeared or never finished, or it CRASHED or FAILED by the end of the window; then no other check is printed |
| 1 | `migration` | `migration missing` | no `migrate_finished` line with the version in the deployment's lines |
| 1 | `version` | `not the running version` | a /healthz answer (start or end) without the version |
| 2 | `signal <event>` | `signal missing (<n> of <min>)` | fewer than `min_events` lines of the event with the version in the window, while the bot took in at least `min_events × signals` updates |
| 2 | `signal <event>` | `inconclusive (too few events)` | the same, with fewer updates than that: too little traffic to tell |
| 3 | `errors` | `error <logger>/<event>/<error> (<n>)` | the strict rule: any line of level ERROR or CRITICAL with the new version inside the window, one line per fingerprint, whether or not the previous version had the same error |

Two more items compare the new version with the previous one. They are informational: their details say so, and
they never change the exit code. With the strict rule, any error in the window has already failed the release.

| Item | Status when it shows something | Judged on |
|---|---|---|
| `new errors` | `new error <fingerprint> (<n>)` | an error fingerprint among the new deployment's collected lines that the previous version's baseline lines do not have |
| `rate` | `error rate <x>% vs <y>%` | error lines per line of the new deployment against the previous version's baseline. With a weak baseline (no previous version, or fewer than half as many lines), the comparison is against the absolute limit of 1% |

A *fingerprint* is `<logger>/<event>/<error type>`, with `-` for a missing part. An error line with no event and no
logger (raw stderr) is `unstructured`. For the strict rule, such a line counts as the new version's, since it can
only come from the new deployment's container. An *update* is one Telegram update the bot took in: aiogram's
per-update line (logger `aiogram.event`, level info) or an `update_invalid` line. An error line is level ERROR or
worse; Railway reports levels in lower case. The exit code is 0 when every check is `confirmed`. It is 3 when the
only other status is `inconclusive (too few events)`, 1 when anything else fired, and 2 when the snapshot cannot be
read.

**What the agent does.** After a push that deploys, it waits for the job (`gh run watch`, `gh run view`). For a
commit deployed another way (a redeploy in Railway, a variable change), it starts the workflow by hand
(`gh workflow run release-confirmation.yml -f sha=<sha>`) or runs collect locally (see Wiring). With the verdict it:

- `confirmed`: appends a `confirm` record to `agentify/releases.jsonl`, with the run's link as evidence;
- not confirmed: goes to remediation with the snapshot. It rolls back or fixes forward, which `autonomy: deploy`
  allows. A rollback is the deploy pipeline's job, never `run`'s;
- `inconclusive`: re-runs the workflow with the same commit, `gh workflow run release-confirmation.yml -f sha=<sha>`.
  Then it asks the owner for the synthetic run the requirement names (/start and one message) and waits for the
  verdict. The dispatched run's window opens at the run's start, so the order matters. The run starts first, and
  the owner's messages come within its 60 minutes. Messages sent before the run started fall outside the window and
  do not count.

**What v1 does not check yet.**

- **A release with no signal named** gets checks 1 and 3 only. That covers remediation, maintenance, and any
  release that changes no requirement file. Its window is the 10 minutes, whatever the traffic, so check 3 covers
  whatever ran in them, perhaps only the startup.
- **The second check of remediation and maintenance is not built.** Remediation's is "the problem's signal does not
  appear while its path runs"; maintenance's is "the product's events stay at their level and the measure
  improved".
- **No latency.** The app logs no durations (aiogram's per-update duration is in its message, which is dropped).
- **A signal is its event name only.** The `kind` of `inbox_saved` (SK-2) is not checked, and the version must
  match.
- **Check 3 sees the window only.** The strict rule reads the window's lines. An ERROR between the deployment's
  creation and its `server_started` (the pre-deploy and the startup) appears only in the informational `new errors`
  line, unless it fails the deploy or the migration. The informational rate is error lines per log line, not per
  update, with no statistical test.
- **A deployment replaced before it served** reads `deploy failed` (REMOVED or SKIPPED); confirm the newer one. If
  the replaced deployment served for a while, the newer run's signals do not include the replaced commit's
  requirement changes.
- **Assumptions about the app and the CLI.** Updates are counted from aiogram's per-update line, and the window
  starts at the `server_started` line. If either changes, the count drops to zero and missing signals read as
  inconclusive, or the window starts at the deployment's creation (`window.start_from` says which). One
  `railway logs` call returns the newest 5000 lines at most (`lines_cut` in meta.json).
- **A window collected after the fact** (`--sha` of an old release, as for the cases) asks /healthz at collection
  time, not during the window.

## Tools

| Tool | Version | How |
|---|---|---|
| railway CLI | 5.71.0 | Locally, the owner's login (`railway login`). In CI, the release tarball `railway-v5.71.0-x86_64-unknown-linux-musl.tar.gz`, its sha256 pinned in the workflow, with `RAILWAY_TOKEN`. Used: `railway deployment list -p -e -s --json --limit 50` and `railway logs <deployment> -p -e -s --deployment --json --since --until --lines 5000`. The project, environment and service IDs are constants in `collect`, so no `railway link` is needed. |
| Python | 3.9.6 (macOS `/usr/bin/python3`), the runner's `python3` in CI | `collect` and `run` use the standard library only |
| git | 2.50.1 | `rev-parse`, `diff`, `show` for the signals; CI checks out with `fetch-depth: 0` |
| HTTP | /healthz | `https://todobot-production-dbb2ecd7.up.railway.app/healthz`, urllib, 15 s timeout |
| GitHub Actions | `actions/checkout@v7`, `actions/upload-artifact@v7`, `ubuntu-latest` | the wiring |

## Signal

The real first release, caba438, collected from production with
`collect --sha caba43826fa7fa8aba0ea44a7186bf96f55d4ca4` (the must-pass case):

```
# caba438, deployment fc7d52ae: window 2026-11-10T17:53:06.649Z to 2026-11-10T18:03:39.542Z, 10.5 min, ended: signals; 2 updates; previous version none
deploy: confirmed — deployment fc7d52ae, SUCCESS
migration: confirmed — migrate_finished with version caba438 at 2026-11-10T17:52:57.754Z
version: confirmed — /healthz answered caba438 2 of 2 times
signal start: confirmed — 1 of 1 in the window (agentify/specs/requirements/skeleton.md)
signal inbox_saved: confirmed — 1 of 1 in the window (agentify/specs/requirements/skeleton.md)
errors: confirmed — no ERROR or CRITICAL line of caba438 in the window's 8 lines
new errors: confirmed — informational: 0 error lines, none new against the previous version
rate: confirmed — informational: 0.0%; weak baseline: no previous version, so the limit is 1.0%
```

The real failed deploy b5eacc16 (`collect --sha caba438… --deployment b5eacc16…`), exit 1:

```
deploy: deploy failed — deployment b5eacc16 of caba438 ended FAILED: read `railway logs --build b5eacc16` and the deployment's config errors in Railway, fix, push again
```

The same release without its migration, as d4e7bd51 ran (case `migration-missing`), exit 1:

```
migration: migration missing — no migrate_finished line with version caba438 in deployment fc7d52ae: the pre-deploy migration did not run or did not finish; check the service's preDeployCommand (`python -m todobot migrate`) in Railway, then redeploy
```

The strict rule (case `known-error`), exit 1. The previous version had the same error, so the comparison finds
nothing new and a lower rate, and the release still fails:

```
errors: error todobot.bot/handler_error/ProgrammingError (1) — version caba438 logged it in the window; an error of the new version fails the release whether or not the previous one had it: read `railway logs fc7d52ae --filter @level:error`
new errors: confirmed — informational: 1 error lines, none new against the previous version
rate: confirmed — informational: 4.0% vs 8.3% of the previous version
```

## Validation

`loops.py validate release-confirmation`: every case below gives its expected result, the same on every run, in
about 0.05 s against a budget of 5 s.

| Case | Made from | Fires with |
|---|---|---|
| `must-pass/caba438-first-release` | the real window of deployment fc7d52ae, collected from production | nothing: all confirmed |
| `must-fire/deploy-failed` | the real deployment b5eacc16 (`collect --deployment b5eacc16…`): FAILED, no lines | `deploy failed` |
| `must-fire/too-few-events-at-cap` | the real window of fc7d52ae collected with `--min-events 2`: 1 of each signal and 2 updates at the 60-min cap | `inconclusive (too few events)` ×2, exit 3 |
| `must-fire/migration-missing` | the must-pass window without the pre-deploy container's lines, as d4e7bd51 ran | `migration missing` |
| `must-fire/old-version` | the must-pass window with /healthz answering an older commit, 3d27709 | `not the running version` |
| `must-fire/signal-missing` | the cap window without the `inbox_saved` line: the photo update was taken in, the signal never came | `signal missing (0 of 1)` |
| `must-fire/new-error` | the must-pass window plus one update that failed with `handler_error` `ProgrammingError`, what d4e7bd51 would have logged | `error todobot.bot/handler_error/ProgrammingError (1)`; informational: `new error …`, `error rate 4.0% vs 1.0%` |
| `must-fire/known-error` | the same, plus a previous version (3d27709) whose 24 baseline lines had the same error twice | `error todobot.bot/handler_error/ProgrammingError (1)` only: none new, the rate lower (4.0% vs 8.3%); under the card's comparison it would have passed |

Each derived case changes one thing in a real window and says what in `meta.json`'s `note`. It fires on its own
check only.

Also checked on 2026-11-10, outside the cases:

- **The waiting path, live against production.** `collect --sha caba438 --min-minutes 108 --cap-minutes 120` was
  started 105 minutes after the start, and the window's minimum ended during the run. It polled the logs and the
  deployment's status once a minute, then saved the window `ended: signals` and `run` confirmed it.
- **run's other paths** on scratch snapshots: a strong baseline, worse and better; a weak baseline; no deployment; a
  deployment still BUILDING at the timeout; CRASHED during the window; REMOVED; no signals; /healthz unreachable; an
  unstructured error line.
- **A dispatched run's window, live against production.** `collect --sha caba438 --window-start
  2026-11-10T18:00:00Z` opened the window at 18:00:00Z. It kept the pre-deploy lines up to `server_started` and
  dropped the lines between that and 18:00. The real /start (18:02Z) and photo (18:03Z) came after the start, so they
  counted, and the release was confirmed. `--window-start now --min-minutes 1 --cap-minutes 2` at 19:48:51Z opened a
  fresh window. It polled to the cap with no synthetic run and ended `inconclusive (too few events)`, exit 3.
- **A push run still gives the same snapshot.** After `--window-start` was added, `collect --sha caba438`, with or
  without a `--window-start` earlier than `server_started`, wrote a `new.jsonl` byte-identical to the must-pass case.
- **The sanitizer** on lines with `user_id`, a request line with headers and an address, a DB error whose traceback
  quotes a row, a config error's text, and a non-name event and logger. Only the allowed fields came out.

The final run happens in GitHub Actions on `main`, where the agent doing product work has no write access to the
job; `run` never touches production.

## Wiring

**`.github/workflows/release-confirmation.yml`, job `release-confirmation`.**

- **Triggers:** a push to `main` whose changes touch Railway's watch paths for the service (`src/**`,
  `pyproject.toml`, `uv.lock`, `Dockerfile`, `.dockerignore`, `railway.json`; checked against the service's
  `watchPatterns`), and `workflow_dispatch` with a `sha` input.
- **Steps:** note the run's start (`RUN_START`, the first step), checkout with full history, install the pinned
  railway CLI, run `collect` then `run` with `RAILWAY_TOKEN` from secrets, write the result lines to the job summary,
  and upload the snapshot as the artifact `release-confirmation-<sha>` (90 days).
- **A dispatched run** passes `--window-start "$RUN_START"` to `collect`, so its window opens at the run's start, not
  at the deploy. That is how an inconclusive release is judged again after a synthetic run. A push run passes
  nothing, and its window opens at the deployment's `server_started`.
- **Timeout:** `timeout-minutes: 100`, over 30 for the deployment plus the 60-minute cap. `inputs.sha` reaches the
  shell only through `env`, and `collect` accepts 7–40 hex digits only.

**Locally, for the agent**, with the owner's CLI login, from the repository root:

```
agentify/loops/release-confirmation/collect --sha <sha> --out /tmp/rc-<sha7>
agentify/loops/release-confirmation/run /tmp/rc-<sha7>
```

`--deployment <id>` re-collects a particular deployment of the commit, as for the cases. `--window-start now` opens
a fresh window, as a dispatched run does. `--min-minutes`, `--min-events`, `--cap-minutes` and
`--deploy-timeout-minutes` change the window.

**The project token is not verified yet.** The RAILWAY_TOKEN secret exists (hitl-log: H3, task:first-loops), but
its value never reached this machine, and the workflow has not run: it is not on `main`. Railway's docs say a project token
is scoped to one environment and serves project-level CLI actions. They do not say whether `railway deployment list`
and `railway logs` accept one. The first run on `main`, or this command on a machine holding the token, settles it:
`RAILWAY_TOKEN=<token> agentify/loops/release-confirmation/collect --sha caba43826fa7fa8aba0ea44a7186bf96f55d4ca4 --out /tmp/rc`.
If either command is refused, `collect` exits 2 with the CLI's error, and the summary says there is no verdict.
