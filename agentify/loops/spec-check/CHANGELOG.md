# spec-check — changes

- 2026-11-11: proposed.
- 2026-11-11: built and validated (task:specs-setup). `run`: `specs.py check` and the check of every layer in
  `agentify/specs/layers.yaml` (`em.py check --strict` for the spine; `checks.py glossary`, `tables` and
  `lifecycles`, the project's own, for the other three), with agentify 0.4.0 and PyYAML 6.0.3 through uv. 14 cases:
  11 broken copies of the spec, one per kind of failure, the real spec, a table gap that is asked about, and a whole
  made-up lifecycle. Wired as the CI job `spec-check` on every pull request, a required check of main;
  github.com/gutnikov/agentify is public, so the job needs no secret (the owner's choice, H3).
- 2026-11-11: active: wired on main with the merge of task:specs-setup, a required status check of main.
