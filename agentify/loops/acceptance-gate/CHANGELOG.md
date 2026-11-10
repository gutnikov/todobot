# acceptance-gate — changes

- 2026-11-10: proposed.
- 2026-11-10: built and validated. `run`: one line per approved criterion (green, red, no test, changed without
  approval, no red record), `not approved` for unlisted acceptance files, and with `--base REF` an H5 required for
  every change to the acceptance tests or an approved requirement. Wired as the CI job `acceptance-gate`. It runs
  the acceptance tests a second time beside `verify --full`; sharing one junit report would save about a minute
  per pull request.
