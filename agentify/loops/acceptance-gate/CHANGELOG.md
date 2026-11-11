# acceptance-gate — changes

- 2026-11-10: proposed.
- 2026-11-10: built and validated. `run`: one line per approved criterion (green, red, no test, changed without
  approval, no red record), `not approved` for unlisted acceptance files, and with `--base REF` an H5 required for
  every change to the acceptance tests or an approved requirement. Wired as the CI job `acceptance-gate`. It runs
  the acceptance tests a second time beside `verify --full`; sharing one junit report would save about a minute
  per pull request.
- 2026-11-11: the YAML requirement format of the spec part (task:specs-setup). `run` reads
  `agentify/specs/requirements/<KEY>.yaml` beside the markdown files: an approved file with an empty `approved_by`
  is `no approver`; its approved tests and red record are under `acceptance_tests`; each of its requirements needs
  a test for each of its examples in the event model (`test_<slice or requirement>_<example name>`); `supersedes`
  retires the file it replaces. With `--base`, an H5 is also needed for a new requirement file that redefines an
  approved one (SK.yaml beside skeleton.md) and for a change to a model element that traces to an approved
  requirement. PyYAML 6.0.3 through `uv run --with`. `parts` adds `spec-check`. Seven new cases; the eight markdown
  cases print what they printed before. Status built, then validated again: 15 cases.
- 2026-11-11: active: wired on main with the merge of task:specs-setup.
