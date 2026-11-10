"""acceptance-gate: every approved criterion has an acceptance test, its tests are the approved ones, they were red
before the work and are green now.

    run DIR [--base REF]

For each requirement in DIR/agentify/specs/requirements/*.md whose Status says approved, one line per criterion
(`### <ID>-<n> ...`, except "(dropped)"): `<ID>-<n>: <status>`, where the status is

    green                     its tests (`test_<id><n>_...` in DIR/tests/acceptance) all pass
    red                       one of them fails, errors, is skipped or did not run
    no test                   no test is named for it
    changed without approval  a file the requirement's "Approved tests" block lists is missing or has another
                              sha256; with --base, the requirement or an acceptance test changed since REF and no
                              H5 approval naming the requirement was added to agentify/hitl-log.jsonl
    no red record             the requirement does not record its tests' run "red before the work"

and `<path>: not approved` for a file in tests/acceptance/ that no approved requirement lists. The acceptance tests
run once, with pytest's junit report. Exit 0 only when every approved criterion is green and every acceptance file
is approved; 2 when the gate cannot run.

A case folder of this loop that has a `base/` folder is a history of two commits, base/ and then the rest: `run`
commits them into a scratch git repository and checks that with `--base` the first commit.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
CONFIG = ROOT / "pyproject.toml"
REQUIREMENTS = "agentify/specs/requirements"
ACCEPTANCE = "tests/acceptance"
HITL_LOG = "agentify/hitl-log.jsonl"

STATUS_LINE = re.compile(r"^\s*-?\s*\*\*Status:\*\*\s*(.*)$", re.M)
CRITERION = re.compile(r"^###\s+([A-Z][A-Z0-9]*)-(\d+)\b(.*)$", re.M)
HASH_LINE = re.compile(r"^([0-9a-f]{64})\s+(\S+)\s*$", re.M)
RED_RECORD = re.compile(r"red before the work", re.I)
TEST_DEF = re.compile(r"^\s*(?:async\s+)?def\s+(test_[a-z][a-z0-9]*?\d+_\w*)", re.M)


class GateError(Exception):
    """The gate cannot run: exit 2."""


@dataclass
class Requirement:
    path: str
    criteria: list[str]
    approved_tests: dict[str, str]
    red_record: bool
    changes: list[str] = field(default_factory=list)  # why its oracle is not the approved one

    @property
    def names(self) -> set[str]:
        """What an H5 approval names it by: its criteria's prefix (SK) or its file's stem (skeleton)."""
        return {criterion.split("-")[0] for criterion in self.criteria} | {Path(self.path).stem}


@dataclass
class Outcome:
    passed: bool
    detail: str


def _git(target: Path, *args: str, check: bool = True) -> str:
    done = subprocess.run(["git", *args], cwd=target, capture_output=True, text=True, check=False)  # noqa: S603, S607
    if check and done.returncode != 0:
        raise GateError(f"git {' '.join(args)}: {done.stderr.strip() or done.stdout.strip()}")
    return done.stdout


def _is_approved(text: str) -> bool:
    status = STATUS_LINE.search(text)
    return status is not None and status.group(1).strip().lower().startswith("approved")


def parse_requirement(rel: str, text: str) -> Requirement | None:
    """The requirement in `text`, or None when its Status is not approved."""
    if not _is_approved(text):
        return None
    criteria = [f"{m.group(1)}-{m.group(2)}" for m in CRITERION.finditer(text) if "(dropped)" not in m.group(3).lower()]
    block = re.search(r"^##\s+Approved tests\b(.*?)(?=^##\s|\Z)", text, re.M | re.S)
    approved = {path: digest for digest, path in HASH_LINE.findall(block.group(1))} if block else {}
    return Requirement(rel, criteria, approved, RED_RECORD.search(text) is not None)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def acceptance_files(target: Path) -> list[str]:
    folder = target / ACCEPTANCE
    if not folder.is_dir():
        return []
    return sorted(
        path.relative_to(target).as_posix()
        for path in folder.rglob("*")
        if path.is_file()
        and path.suffix != ".pyc"
        and not any(part.startswith(".") or part == "__pycache__" for part in path.relative_to(folder).parts)
    )


def _named_prefixes(file_name: str) -> set[str]:
    """The criteria prefix a test file's name points to: test_sk1_start.py -> {"SK"}."""
    match = re.match(r"test_([a-z][a-z0-9]*?)\d+_", file_name)
    return {match.group(1).upper()} if match else set()


def _test_prefix(criterion: str) -> str:
    prefix, number = criterion.split("-")
    return f"test_{prefix.lower()}{number}_"


def run_acceptance_tests(target: Path) -> tuple[dict[str, list[Outcome]], list[str]]:
    """{test name without parameters: outcomes} from one pytest run, and modules that could not be collected."""
    results: dict[str, list[Outcome]] = {}
    broken: list[str] = []
    if not (target / ACCEPTANCE).is_dir():
        return results, broken
    with tempfile.TemporaryDirectory() as tmp:
        report = Path(tmp) / "junit.xml"
        argv = [sys.executable, "-m", "pytest", ACCEPTANCE, "-c", str(CONFIG), "--rootdir", str(target)]
        # conftest.py files above tests/acceptance are not the approved ones: pytest must not load them.
        argv += ["--confcutdir", ACCEPTANCE, "-p", "no:cacheprovider", "-q", "--continue-on-collection-errors"]
        argv += ["--junitxml", str(report), "-o", "junit_family=xunit1"]
        done = subprocess.run(argv, cwd=target, capture_output=True, text=True, check=False)  # noqa: S603
        if not report.exists():
            tail = (done.stdout + done.stderr).strip().splitlines()
            broken.append(f"{ACCEPTANCE}: pytest did not run (exit {done.returncode}): {tail[-1] if tail else ''}")
            return results, broken
        # pytest wrote the report into our own temporary folder.
        for case in ET.parse(report).getroot().iter("testcase"):  # noqa: S314
            problem = next((child for child in case if child.tag in ("failure", "error", "skipped")), None)
            name = case.get("name", "")
            if not case.get("classname"):  # a module that does not import
                text = (problem.text or "") if problem is not None else ""
                last = next((line.strip() for line in reversed(text.splitlines()) if line.strip()), "")
                broken.append(f"{name.replace('.', '/')}.py:1: the module does not import: {last}")
                continue
            where = f"{case.get('file', '')}:{int(case.get('line') or 0) + 1}"
            if problem is None:
                outcome = Outcome(True, "")
            else:
                verb = {"failure": "failed", "error": "errored", "skipped": "was skipped"}[problem.tag]
                message = (problem.get("message") or problem.text or "").strip().splitlines()
                outcome = Outcome(False, f"{where}: {name} {verb}: {message[0][:300] if message else ''}")
            results.setdefault(name.split("[")[0], []).append(outcome)
    return results, broken


def declared_tests(target: Path) -> dict[str, str]:
    """{test name: file:line} for every `def test_<id><n>_...` in tests/acceptance."""
    found: dict[str, str] = {}
    for rel in acceptance_files(target):
        if not rel.endswith(".py"):
            continue
        text = (target / rel).read_text(encoding="utf-8", errors="replace")
        for match in TEST_DEF.finditer(text):
            found.setdefault(match.group(1), f"{rel}:{text.count(chr(10), 0, match.start()) + 1}")
    return found


def unapproved_changes(target: Path, ref: str, requirements: list[Requirement]) -> list[str]:
    """Changes since `ref` to tests/acceptance or an approved requirement without an H5 approval naming it.

    Marks the requirements concerned and returns one line per such path."""
    base = _git(target, "merge-base", ref, "HEAD").strip()
    changed = set(_git(target, "diff", "--name-only", "--relative", base).split())
    changed |= set(_git(target, "ls-files", "--others", "--exclude-standard").split())
    shown = _git(target, "show", f"{base}:./{HITL_LOG}", check=False)
    before = set(shown.splitlines())
    log = target / HITL_LOG
    now = log.read_text(encoding="utf-8").splitlines() if log.exists() else []
    added = [line for line in now if line not in before]
    approvals = []
    for line in added:
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if isinstance(entry, dict) and entry.get("policy") == "H5":
            approvals.append(" ".join(str(entry.get(key, "")) for key in ("loop", "question", "checked")))

    def approved(names: set[str]) -> bool:
        return any(
            re.search(rf"(?<![A-Za-z0-9]){re.escape(name)}(?![A-Za-z0-9])", text, 0 if name.isupper() else re.I)
            for name in names
            for text in approvals
        )

    by_path = {requirement.path: requirement for requirement in requirements}
    lines = []
    for path in sorted(changed):
        if path.startswith(f"{REQUIREMENTS}/") and path.endswith(".md"):
            owners = [by_path[path]] if path in by_path else []
            if not owners and not _is_approved(_git(target, "show", f"{base}:./{path}", check=False)):
                continue  # a requirement that is not approved, before or now
            names = {owner_name for owner in owners for owner_name in owner.names} or {Path(path).stem}
        elif path.startswith(f"{ACCEPTANCE}/"):
            # Its requirements: those that list it, and those its name points to (test_sk1_start.py: SK).
            owners = [
                requirement
                for requirement in requirements
                if path in requirement.approved_tests or requirement.names & _named_prefixes(Path(path).name)
            ]
            names = {owner_name for owner in owners for owner_name in owner.names}
        else:
            continue
        if approved(names) if names else approvals:
            continue
        who = " or ".join(sorted(names)) if names else "any requirement"
        why = f"changed since {ref}, and no H5 approval naming {who} was added to {HITL_LOG}"
        lines.append(f"{path}: changed without approval ({why})")
        for owner in owners:
            owner.changes.append(path)
    return lines


def gate(target: Path, ref: str | None) -> int:
    requirements = []
    for path in sorted((target / REQUIREMENTS).glob("*.md")):
        requirement = parse_requirement(path.relative_to(target).as_posix(), path.read_text(encoding="utf-8"))
        if requirement is not None:
            requirements.append(requirement)

    path_lines = []
    listed = set()
    for requirement in requirements:
        for rel, digest in sorted(requirement.approved_tests.items()):
            listed.add(rel)
            file = target / rel
            if not file.is_file():
                path_lines.append(f"{rel}: changed without approval ({requirement.path} lists it; the file is missing)")
                requirement.changes.append(rel)
            elif (actual := _sha256(file)) != digest:
                why = f"{requirement.path} approved sha256 {digest[:12]}…, the file has {actual[:12]}…"
                path_lines.append(f"{rel}: changed without approval ({why})")
                requirement.changes.append(rel)
    for rel in acceptance_files(target):
        if rel not in listed:
            how = "an acceptance test comes from the test writer, and its sha256 goes into an approved requirement"
            path_lines.append(f"{rel}: not approved (no approved requirement lists it; {how})")
    if ref is not None:
        path_lines += unapproved_changes(target, ref, requirements)

    declared = declared_tests(target)
    results, broken = run_acceptance_tests(target)
    out = sorted(set(path_lines)) + broken
    green = total = 0
    for requirement in requirements:
        for criterion in requirement.criteria:
            total += 1
            prefix = _test_prefix(criterion)
            tests = sorted({name for name in [*declared, *results] if name.startswith(prefix)})
            details = []
            if requirement.changes:
                status = "changed without approval"
            elif not requirement.red_record:
                status = "no red record"
                details.append(f"{requirement.path}: record the acceptance run that failed before the work")
            elif not tests:
                status = "no test"
                details.append(f"{requirement.path}: no test in {ACCEPTANCE} is named {prefix}…")
            else:
                for name in tests:
                    outcomes = results.get(name)
                    if not outcomes:
                        details.append(f"{declared.get(name, ACCEPTANCE)}: {name} did not run")
                    details += [outcome.detail for outcome in outcomes or [] if not outcome.passed]
                status = "red" if details else "green"
            green += status == "green"
            out.append(f"{criterion}: {status}")
            out += [f"  {detail}" for detail in details]
    passed = green == total and not path_lines and not broken
    verdict = "passes" if passed else "fires"
    out.append(f"acceptance-gate: {green} of {total} approved criteria green; {verdict}")
    sys.stdout.write("".join(f"{line}\n" for line in out))
    return 0 if passed else 1


def gate_case_history(case: Path) -> int:
    """A case with base/: commit base/, then the rest, into a scratch repository; gate it with --base base."""
    env = dict(os.environ, GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1")
    env |= {"GIT_AUTHOR_NAME": "case", "GIT_AUTHOR_EMAIL": "case@example.invalid", "GIT_COMMITTER_NAME": "case"}
    env |= {"GIT_COMMITTER_EMAIL": "case@example.invalid", "GIT_AUTHOR_DATE": "2026-01-01T00:00:00Z"}
    env["GIT_COMMITTER_DATE"] = env["GIT_AUTHOR_DATE"]
    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp) / "repo"
        shutil.copytree(case / "base", repo, ignore=shutil.ignore_patterns("__pycache__"))

        def commit(message: str) -> None:
            for argv in (["add", "-A"], ["-c", "commit.gpgsign=false", "commit", "-q", "-m", message]):
                subprocess.run(["git", *argv], cwd=repo, env=env, check=True, capture_output=True)  # noqa: S603, S607

        subprocess.run(["git", "init", "-q"], cwd=repo, env=env, check=True, capture_output=True)  # noqa: S607
        commit("base")
        base = _git(repo, "rev-parse", "HEAD").strip()
        for child in repo.iterdir():
            if child.name != ".git":
                shutil.rmtree(child) if child.is_dir() else child.unlink()
        shutil.copytree(case, repo, ignore=shutil.ignore_patterns("base", "__pycache__"), dirs_exist_ok=True)
        commit("head")
        return gate(repo, base)


def main() -> int:
    parser = argparse.ArgumentParser(prog="run", description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    parser.add_argument("dir", type=Path, help="the project to check")
    parser.add_argument("--base", metavar="REF", help="also check that what changed since REF was approved (H5)")
    args = parser.parse_args()
    target = args.dir.resolve()
    try:
        if args.base is None and target.is_relative_to(HERE / "cases") and (target / "base").is_dir():
            return gate_case_history(target)
        return gate(target, args.base)
    except GateError as error:
        sys.stdout.write(f"acceptance-gate: cannot run: {error}\n")
        if args.base:
            sys.stdout.write("  the history must reach the base: `actions/checkout` with `fetch-depth: 0`\n")
        return 2


if __name__ == "__main__":
    sys.exit(main())
