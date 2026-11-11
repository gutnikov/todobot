# release-confirmation — changes

- 2026-11-10: proposed.
- 2026-11-10: built and validated: `collect` (Railway deployment, /healthz, the requirements' `prod:` signals, the
  window, a sanitized snapshot) and `run` (deploy, migration, version, signals, new errors, error rate); the real
  first release caba438 as the must-pass case, the real failed deploy b5eacc16 and five more must-fire windows; wired
  as `.github/workflows/release-confirmation.yml`. The project token in CI is not verified yet.
- 2026-11-10: the owner's answers. Errors are strict: any ERROR or CRITICAL line of the new version in the window
  fails the release (`error <fingerprint> (<n>)`), whether or not the previous version had it. The comparison with
  the previous version (`new errors`, `rate`) stays, as informational lines that do not change the exit. New
  must-fire case `known-error`. An inconclusive window is judged again by a `workflow_dispatch` run of the same
  commit: the workflow notes the run's start and `collect --window-start` opens the window there, so the owner's
  synthetic run counts. Validated again.
- 2026-11-10: the first real run (merge of PR #2, ba8938c) waited 30 min for a deployment that never came: the service had no deploy trigger. Deploy wait 30 → 10 min; the no-deployment hint names the trigger; new must-fire case `no-deployment` from that run. Validated again.
- 2026-11-10: the workflow's paths drop `railway.json`: the service's settings moved to `.railway/railway.ts` and Railway's watch paths no longer list it. No change to collect or run.
- 2026-11-11: `collect` takes the signals of the spec part's requirement files from `specs.py signals --base
  <previous sha>` (agentify 0.4.0), run in a temporary worktree of the released commit, when the release changes
  `agentify/specs/`; skeleton.md and other markdown files keep their `prod:` lines. A signal's `min` above
  `--min-events` raises it for all. `run` is unchanged. The workflow checks agentify out (public since 2026-11-11)
  and runs `collect` through uv with PyYAML. New must-pass case `caba438-signals-from-spec`.
  Validated again: 10 cases.
- 2026-11-11: active: wired on main with the merge of task:specs-setup.
