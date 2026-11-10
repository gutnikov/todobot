"""verify: the fast checks on the project at DIR, each problem as `file:line: rule: what to do`.

    run DIR [--full]

ruff check, ruff format --check, mypy (strict) on DIR/src, pytest DIR/tests/unit (DIR/tests with --full: the
acceptance tests too, what CI runs) and the log-privacy rule (log_privacy.py). The tools and their settings are
this repository's: `run` starts this script in the repository's uv environment, and the tools read the repository's
pyproject.toml whatever DIR is, so a case folder needs no config of its own. Exit 0 when every check passes.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import log_privacy

ROOT = Path(__file__).resolve().parents[3]
CONFIG = ROOT / "pyproject.toml"
PY = sys.executable


def _run(argv: list[str], cwd: Path, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    # The project's own tools, from the environment uv set up for it.
    return subprocess.run(argv, cwd=cwd, env=env, capture_output=True, text=True, check=False)  # noqa: S603


def _first_line(text: str, limit: int = 300) -> str:
    line = next((part.strip() for part in text.splitlines() if part.strip()), "")
    return line if len(line) <= limit else line[: limit - 1] + "…"


def _tool_failed(where: str, rule: str, done: subprocess.CompletedProcess[str]) -> list[str]:
    output = (done.stderr.strip() or done.stdout.strip()).splitlines()
    detail = output[-1] if output else "no output"
    return [f"{where}:1: {rule}: the tool itself failed (exit {done.returncode}): {detail}"]


def _ruff(target: Path, command: list[str]) -> tuple[list[tuple[str, int, dict[str, Any]]], list[str]]:
    """ruff's diagnostics as (path, line, item), or the tool's own failure."""
    done = _run([PY, "-m", "ruff", *command, "--config", str(CONFIG), "--no-cache", "--output-format", "json"], target)
    if done.returncode not in (0, 1):
        return [], _tool_failed("pyproject.toml", f"ruff {command[0]}", done)
    items = [
        (Path(item["filename"]).relative_to(target).as_posix(), item["location"]["row"], item)
        for item in json.loads(done.stdout or "[]")
    ]
    return sorted(items, key=lambda entry: entry[:2]), []


def ruff_check(target: Path) -> list[str]:
    items, failed = _ruff(target, ["check"])
    lines = []
    for path, line, item in items:
        fix = item.get("fix") or {}
        if fix.get("applicability") == "safe":
            how = f"{fix.get('message') or 'autofixable'}: `uv run ruff check --fix {path}` does it"
        elif fix:
            how = f"{fix.get('message') or 'a fix exists'} (not safe to apply blindly: apply it by hand)"
        else:
            how = f"fix it by hand ({item['url']})" if item.get("url") else "fix it by hand"
        lines.append(f"{path}:{line}: ruff/{item.get('code') or 'syntax'}: {item['message']}. Fix: {how}")
    return failed + lines


def ruff_format(target: Path) -> list[str]:
    items, failed = _ruff(target, ["format", "--check"])
    return failed + [
        f"{path}:{line}: ruff-format: the file is not formatted from here on. Fix: `uv run ruff format {path}`"
        for path, line, _ in items
    ]


def mypy(target: Path) -> list[str]:
    argv = [PY, "-m", "mypy", "--config-file", str(CONFIG), "--no-error-summary", "--no-pretty"]
    done = _run([*argv, "--no-color-output", "--show-error-codes", "src"], target)
    if done.returncode not in (0, 1):
        return _tool_failed("src", "mypy", done)
    lines = []
    for raw in done.stdout.splitlines():
        path, sep, rest = raw.partition(": ")
        if not sep or ":" not in path:
            continue
        kind, _, message = rest.partition(": ")
        if kind == "note":
            lines.append(f"{path}: mypy/note: {message}")
            continue
        code = message.rsplit("[", 1)[1].rstrip("]") if message.endswith("]") and "  [" in message else "error"
        text = message.rsplit("  [", 1)[0]
        lines.append(
            f"{path}: mypy/{code}: {text}. Fix: make the code and its annotations agree (mypy --strict); "
            f"a `# type: ignore[{code}]` needs a comment saying why"
        )
    return lines


def pytest(target: Path, tests: str) -> list[str]:
    if not (target / tests).is_dir():
        return []
    env = dict(os.environ)
    # The case's or the project's own src/ first: the repository's editable install would import its todobot.
    env["PYTHONPATH"] = os.pathsep.join(filter(None, [str(target / "src"), env.get("PYTHONPATH")]))
    with tempfile.TemporaryDirectory() as tmp:
        report = Path(tmp) / "junit.xml"
        argv = [PY, "-m", "pytest", tests, "-c", str(CONFIG), "--rootdir", str(target), "-p", "no:cacheprovider"]
        argv += ["-q", "--junitxml", str(report), "-o", "junit_family=xunit1"]
        done = _run(argv, target, env)
        # pytest wrote the report into our own temporary folder.
        cases = ET.parse(report).getroot().iter("testcase") if report.exists() else iter(())  # noqa: S314
        lines = []
        for case in cases:
            problem = next((child for child in case if child.tag in ("failure", "error")), None)
            if problem is None:
                continue
            name = case.get("name", "")
            path = case.get("file") or name.replace(".", "/") + ".py"
            line = int(case.get("line") or 0) + 1
            if not case.get("classname"):  # the module could not be collected: its import or its syntax failed
                detail = _first_line("\n".join(reversed((problem.text or "").splitlines())))
                lines.append(f"{path}:1: pytest: the test module does not import: {detail}. Fix the import or the code")
                continue
            what = "failed" if problem.tag == "failure" else "errored (a fixture failed)"
            lines.append(
                f"{path}:{line}: pytest: {name} {what}: {_first_line(problem.get('message') or problem.text or '')}. "
                f"Fix the code, or the test if the test is wrong; `uv run pytest '{path}::{name}'` shows the traceback"
            )
    if done.returncode not in (0, 1, 5) and not lines:
        return _tool_failed(tests, "pytest", done)
    return lines


def main() -> int:
    parser = argparse.ArgumentParser(prog="run", description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    parser.add_argument("dir", type=Path, help="the project to check")
    parser.add_argument("--full", action="store_true", help="all tests (tests/), the acceptance tests too")
    args = parser.parse_args()
    target = args.dir.resolve()
    if not (target / "src").is_dir():
        sys.stdout.write(f"{target}: no src/: verify checks a project with its code in src/\n")
        return 2
    tests = "tests" if args.full else "tests/unit"
    checks: list[tuple[str, Callable[[], list[str]]]] = [
        ("ruff check", lambda: ruff_check(target)),
        ("ruff format", lambda: ruff_format(target)),
        ("mypy", lambda: mypy(target)),
        (f"pytest {tests}", lambda: pytest(target, tests)),
        ("log-privacy", lambda: [str(finding) for finding in log_privacy.check(target)]),
    ]
    with ThreadPoolExecutor(max_workers=len(checks)) as pool:
        results = [pool.submit(check) for _, check in checks]
        findings = [line for result in results for line in result.result()]
    names = ", ".join(name for name, _ in checks)
    if findings:
        sys.stdout.write("".join(f"{line}\n" for line in findings))
        sys.stdout.write(f"verify: {len(findings)} problem(s) ({names}); fix each, then run verify again\n")
        return 1
    sys.stdout.write(f"verify: ok ({names})\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
