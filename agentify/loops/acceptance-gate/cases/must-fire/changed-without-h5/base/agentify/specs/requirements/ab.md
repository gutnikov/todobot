# AB: the gate's own example

- **Status:** approved by the user on 2026-11-10 (hitl-log: H5, task:ab)

## Acceptance criteria

### AB-1 Adds

Two and two make four.

### AB-2 (dropped)

Not needed.

### AB-3 Subtracts

Four minus two is two.

## Approved tests

Written by a test writer with a fresh context; red before the work: `uv run pytest tests/acceptance` → `2 failed`, 2026-11-10.

```
bca7a8f5e3d70f7889d34adffb3de440fc8e313559696e589470f03f8adf8d30  tests/acceptance/test_ab1_adds.py
0cd37f313163004d050a29a5a5b7159ae11fcebb0c0a447a5711400bb1fb3379  tests/acceptance/test_ab3_subtracts.py
```
