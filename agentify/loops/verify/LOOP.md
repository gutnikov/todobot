---
id: verify
title: "Lint, format, types, unit tests and log privacy before the turn ends"
type: stop-hook
status: active
trigger: [local, ci]
places: [delivery/work, remediation/work, maintenance/work]
run: ./run
budget_seconds: 30
validation: cases
agents: []
parts: []
hitl: []
wiring: [.claude/settings.json hooks.Stop, .github/workflows/verify.yml jobs.verify]
---

# Lint, format, types, unit tests and log privacy before the turn ends

## Requirement

The agent ends its work with broken lint, formatting, types or unit tests, or with a code path that writes what
users send into the logs (the map's pain "user content in logs"). In the skeleton task, message text reached the
logs twice: through `exc_info=` on a database error, whose DETAIL quotes the row, and through a library's error.
A reviewer caught both times, not a loop. The user asked for this loop (H4, task:first-loops). Its places are
delivery/work, remediation/work and maintenance/work: the agent's own loop while it writes the change, the earliest
phase that can catch these failures.

## Loop

```
 agent ends its turn ──► Stop hook ──► changes in src/ tests/ pyproject.toml uv.lock? ──no──► exit 0
                                         └─yes─► ./run <project> ──► exit 0: the turn ends
                                                                └──► exit 2: findings on stderr, the turn goes on
 pull request, push to main ──► CI job verify ──► ./run . --full ──► red check: the merge waits
```

1. The agent ends its turn. The Stop hook (`stop-hook`) reads the hook input and asks git whether anything under
   `src/`, `tests/`, `pyproject.toml` or `uv.lock` changed in the working tree. If nothing did, the turn ends.
2. Otherwise it runs `run <project>`: ruff check, ruff format --check, mypy (strict) on `src/`, pytest
   `tests/unit` and the log-privacy rule, in parallel.
3. When any of them fails, the hook exits 2 and Claude Code shows the agent the findings, one per line with what
   to do. The agent fixes them and tries to end the turn again. The hook does not give up on `stop_hook_active`;
   Claude Code itself ends the turn after 8 blocks in a row without a tool call.
4. In CI the same `run` with `--full` also runs the acceptance tests (`make ci`'s scope). The job `verify` is the
   check that main requires; there the agent cannot edit or switch the loop off.

The log-privacy rule (`log_privacy.py`) is a static check over the AST of `src/`:

- R1: `exc_info=` or `.exception(...)` with arguments, outside `src/todobot/logs.py` (the process-level crash
  hooks) and `src/todobot/__main__.py` (main()'s last-resort catch; see CHANGELOG.md, an open question).
- R2: SQLAlchemy's `create_engine(...)` or `create_async_engine(...)` (resolved through the imports, so the
  project's own `todobot.db.create_engine` is not one) without `hide_parameters=True`.
- R3: a logging call (`log_event(...)`, `logger.<level>(...)`, `logging.<level>(...)`) whose arguments read
  `.text` or `.caption` (except in the secrets handed to `redact()`), or log a `message` or `update` object
  whole (as an argument, in an f-string, a container, `%`, `str()` or `repr()`).

Its limits: it knows these names, not types. An object called something else than `message`/`update`, content
that went through a variable first, `warnings.warn(...)` and engines made with `engine_from_config` pass it.

## Tools

The tools come from the repository's uv environment: `run` starts `verify.py` with
`uv run --quiet --locked --project <repo> python`, and each tool reads the repository's `pyproject.toml`
(`--config`, `--config-file`, `-c`) whatever folder it checks. Validated with:

- uv 0.10.11 (`uv sync --locked` installs the rest; CI pins the same version)
- Python 3.12.13 (the stop hook itself runs on any `python3` from 3.9; it only calls git and `run`)
- ruff 0.17.4, mypy 2.4.1, pytest 9.2.0 with pytest-timeout 2.4.0 (dev group of `uv.lock`)
- git 2.50.1 (the hook's change check)

## Signal

`file:line: <rule>: what is wrong. Fix: what to do`, one line per problem, then a summary; exit 1. A real run, the
stop hook in a scratch copy of the repository with one expectation of `test_message_kind.py` changed:

```
tests/unit/test_message_kind.py:37: pytest: test_classify[sticker] failed: AssertionError: assert InboxItem(kin... file_id=None) == InboxItem(kin... file_id=None). Fix the code, or the test if the test is wrong; `uv run pytest 'tests/unit/test_message_kind.py::test_classify[sticker]'` shows the traceback
verify: 1 problem(s) (ruff check, ruff format, mypy, pytest tests/unit, log-privacy); fix each, then run verify again

The turn cannot end while verify fails (agentify/loops/verify). Fix each problem above in the code, then run `agentify/loops/verify/run .` until it prints `verify: ok`.
```

And the rule on today's `src/` before `src/todobot/bot.py:133` was fixed:

```
src/todobot/bot.py:133: log-privacy/R1: `exc_info=` logs the traceback: a traceback quotes what users wrote (a database error's DETAIL shows the rejected row). Log `exc=redact(format_exception(exc), _content(message))` as `on_error` in src/todobot/bot.py does, or only `error=type(exc).__name__` when no message is in scope
```

The rules are `ruff/<code>`, `ruff-format`, `mypy/<code>`, `pytest`, `log-privacy/R1..R3`. Passing prints
`verify: ok (...)` and exits 0. Exit 2: `<dir>` has no `src/`.

## Validation

`loops.py validate verify`: 8 cases, 2 runs each, all within budget. Each case is a minimal project
(`src/todobot/`, and `tests/unit/` where needed) with one failure; the repository's tools and config check it, and
the repository's `ruff` skips `agentify/loops/*/cases` (`pyproject.toml`).

| Case | Stands for |
|---|---|
| must-fire/ruff-violation | an unused import (F401) |
| must-fire/format-violation | a file ruff format would change |
| must-fire/mypy-error | a return value against its annotation |
| must-fire/failing-unit-test | a unit test that fails; its `todobot` is the case's, not the repository's |
| must-fire/log-r1-exc-info | the skeleton's leak rebuilt: the errors handler logs `exc_info=event.exception` |
| must-fire/log-r2-engine | `create_async_engine(...)` without `hide_parameters=True` |
| must-fire/log-r3-content | `text=message.text` and `logger.info("got %s", message)` |
| must-pass/clean | every form the rule must leave alone: the redacting form with `.text` in `redact()`'s secrets, `_content(message)`, `_update_id(update)`, ids, a length taken outside the call, `task.exception()`, the project's own `create_engine`, an engine with `hide_parameters=True`, a passing unit test |

Measured: fast `run .` on the repository 11 s (unit tests 10 s, mypy 0.6 s warm, 3.5 s cold), the stop hook in a
fresh copy 23 s including uv creating the environment, `run . --full` 68 s; `budget_seconds` is 30 for the fast mode.
The stop hook was tested by hand in a scratch copy: exit 2 with the finding above on a broken unit test, exit 0
after restoring it (verify ran and passed), exit 0 in 0.2 s with no changes in the watched paths.

## Wiring

- `.claude/settings.json` `hooks.Stop`: `"$CLAUDE_PROJECT_DIR"/agentify/loops/verify/stop-hook`, timeout 300 s.
  The developer agent does not write `agentify/`, so it cannot change the check, only the hook entry.
- `.github/workflows/verify.yml`, job `verify`, on every pull request and every push to main: checkout,
  `astral-sh/setup-uv` (uv 0.10.11, Python 3.12), `uv sync --locked`, `agentify/loops/verify/run . --full`. Actions
  are pinned to commit SHAs. Making `verify` a required status check of main is the owner's step.
