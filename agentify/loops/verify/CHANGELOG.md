# verify — changes

- 2026-11-10: proposed.
- 2026-11-10: built and validated. `run`: ruff check, ruff format --check, mypy on src/, pytest tests/unit (with
  `--full`: tests/) and the log-privacy rule R1–R3 (`log_privacy.py`), output `file:line: rule: what to do`.
  Wired as the Stop hook and the CI job `verify`. `src/todobot/bot.py:133` (`apology_failed` with `exc_info=exc`)
  now logs `exc=redact(format_exception(exc), _content(message))`. `pyproject.toml`: ruff skips
  `agentify/loops/*/cases`. Open, for the owner: `src/todobot/__main__.py:53` logs `<command>_failed` with
  `exc_info=exc` and has no message to redact; R1 allows that file, like `logs.py`, until the owner decides.
