"""acceptance-gate: every approved criterion has an acceptance test, its tests are the approved ones, they were red
before the work and are green now.

    run DIR [--base REF]

It reads the approved requirement files in DIR/agentify/specs/requirements/, in two formats:

- `<KEY>.yaml`, the spec part's format, whose `status` is approved: one line per requirement, `<KEY>#<n>`. Its tests
  are named for it (`test_<key><n>_...`: SK#1, test_sk1_...) or for one of its examples. Its examples are those of
  the event model (the event-model layer in agentify/specs/layers.yaml) that trace to it, through `reqs:` on the
  example or its slice. A test pins an example down when it is named `test_<tag>_<example name>`: <tag> is the
  slice id (s02) or one of the example's requirements (sk2), and the name is the example's in lower case, every run
  of other characters one `_`. `acceptance_tests` holds the approved tests' sha256 and the red record
  (`red_before_work`). `supersedes: <path>` retires the requirement file it replaces, once this one is approved.
- `*.md` whose Status says approved: one line per criterion `<ID>-<n>` (`### <ID>-<n> ...`, except "(dropped)"), its
  tests named `test_<id><n>_...`, its approved tests in an "Approved tests" block of sha256 lines, and the record of
  its tests' run "red before the work".

The status of each criterion, the first that applies:

    changed without approval  a file of its approved tests is missing or has another sha256; with --base: it, its
                              requirement file, an example or another spec element that traces to it, or an
                              acceptance test changed since REF with no H5 naming its requirement added to
                              agentify/hitl-log.jsonl; or a new requirement file redefines it so
    no approver               (YAML) approved, but approved_by is empty: only a person approves
    no red record             its tests' run before the work is not recorded
    no test                   no test is named for it, or one of its examples has no test
    red                       one of its tests fails, errors, is skipped or did not run
    green                     its tests all pass

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

import yaml

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
CONFIG = ROOT / "pyproject.toml"
REQUIREMENTS = "agentify/specs/requirements"
LAYERS = "agentify/specs/layers.yaml"
ACCEPTANCE = "tests/acceptance"
HITL_LOG = "agentify/hitl-log.jsonl"
YAML_SUFFIXES = (".yaml", ".yml")

STATUS_LINE = re.compile(r"^\s*-?\s*\*\*Status:\*\*\s*(.*)$", re.M)
CRITERION = re.compile(r"^###\s+([A-Z][A-Z0-9]*)-(\d+)\b(.*)$", re.M)
HASH_LINE = re.compile(r"^([0-9a-f]{64})\s+(\S+)\s*$", re.M)
RED_RECORD = re.compile(r"red before the work", re.I)
TEST_DEF = re.compile(r"^\s*(?:async\s+)?def\s+(test_[a-z][a-z0-9]*?\d+_\w*)", re.M)
TEST_TAG = re.compile(r"test_([a-z][a-z0-9]*?\d+)_")


class GateError(Exception):
    """The gate cannot run: exit 2."""


def tag_of(criterion: str) -> str:
    """The tag tests carry for a criterion: SK-1 and SK#1 -> sk1."""
    key, _, number = criterion.rpartition("#") if "#" in criterion else criterion.rpartition("-")
    return re.sub(r"[^a-z0-9]", "", key.lower()) + number


def slug(name: str) -> str:
    """An example's name as a test names it: lower case, every run of other characters one `_`."""
    return re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")


@dataclass
class Example:
    file: str
    slice: str
    name: str
    reqs: list[str]

    @property
    def label(self) -> str:
        return f"example {self.slice}/{self.name}"

    @property
    def test_names(self) -> list[str]:
        """The names of the tests that pin it down: tagged with its slice or one of its requirements."""
        tags = [self.slice.lower(), *(tag_of(req) for req in self.reqs)]
        return [f"test_{tag}_{slug(self.name)}" for tag in dict.fromkeys(tags)]


@dataclass
class Requirement:
    path: str
    criteria: list[str]
    approved_tests: dict[str, str]
    red_record: bool
    key: str = ""  # YAML: the file's key; markdown: empty, its criteria's prefix names it
    approver: bool = True
    supersedes: list[str] = field(default_factory=list)
    examples: dict[str, list[Example]] = field(default_factory=dict)  # criterion -> its examples (YAML)
    changes: list[str] = field(default_factory=list)  # why its oracle is not the approved one

    @property
    def names(self) -> set[str]:
        """What an H5 approval names it by: its key or criteria's prefix (SK), or its file's stem (skeleton)."""
        prefixes = {self.key} if self.key else {criterion.split("-")[0] for criterion in self.criteria}
        return prefixes | {Path(self.path).stem}

    @property
    def tags(self) -> set[str]:
        """The tags of the tests that belong to it: its criteria's (sk1) and its examples' slices' (s02)."""
        slices = {example.slice.lower() for examples in self.examples.values() for example in examples}
        return {tag_of(criterion) for criterion in self.criteria} | slices


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


def _load_yaml(text: str) -> dict:
    try:
        doc = yaml.safe_load(text)
    except yaml.YAMLError:
        return {}
    return doc if isinstance(doc, dict) else {}


def parse_requirement(rel: str, text: str) -> Requirement | None:
    """The requirement in `text`, or None when it is not approved."""
    if rel.endswith(YAML_SUFFIXES):
        return parse_yaml_requirement(rel, text)
    if not _is_approved(text):
        return None
    criteria = [f"{m.group(1)}-{m.group(2)}" for m in CRITERION.finditer(text) if "(dropped)" not in m.group(3).lower()]
    block = re.search(r"^##\s+Approved tests\b(.*?)(?=^##\s|\Z)", text, re.M | re.S)
    approved = {path: digest for digest, path in HASH_LINE.findall(block.group(1))} if block else {}
    return Requirement(rel, criteria, approved, RED_RECORD.search(text) is not None)


def parse_yaml_requirement(rel: str, text: str) -> Requirement | None:
    doc = _load_yaml(text)
    if doc.get("status") != "approved":
        return None
    tests = doc.get("acceptance_tests") if isinstance(doc.get("acceptance_tests"), dict) else {}
    supersedes = doc.get("supersedes") or []
    return Requirement(
        rel,
        [str(r.get("id")) for r in doc.get("requirements") or [] if isinstance(r, dict) and r.get("id")],
        {str(path): str(digest) for path, digest in (tests.get("sha256") or {}).items()},
        bool(str(tests.get("red_before_work") or "").strip()),
        key=str(doc.get("key") or Path(rel).stem),
        approver=bool(str(doc.get("approved_by") or "").strip()),
        supersedes=[supersedes] if isinstance(supersedes, str) else [str(s) for s in supersedes],
    )


def _key_of(rel: str, text: str) -> set[str]:
    """What a requirement file, approved or not, says it defines: its key, or its criteria's prefixes."""
    if rel.endswith(YAML_SUFFIXES):
        key = _load_yaml(text).get("key")
        return {str(key)} if key else set()
    return {m.group(1) for m in CRITERION.finditer(text)}


def _approved_text(rel: str, text: str) -> bool:
    return (_load_yaml(text).get("status") == "approved") if rel.endswith(YAML_SUFFIXES) else _is_approved(text)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


# ----------------------------------------------------------------------------------------------- the event model
def model_paths(target: Path) -> list[str]:
    """The event model's folders and files (the event-model layers of layers.yaml), relative to target."""
    layers = _load_yaml((target / LAYERS).read_text(encoding="utf-8")) if (target / LAYERS).is_file() else {}
    return [
        str(layer.get("path") or "").rstrip("/")
        for layer in layers.get("layers") or []
        if isinstance(layer, dict) and layer.get("type") == "event-model" and layer.get("status") != "retired"
    ]


def model_files(target: Path) -> list[str]:
    found = set()
    for rel in model_paths(target):
        base = target / rel
        candidates = base.rglob("*") if base.is_dir() else [base]
        found |= {p.relative_to(target).as_posix() for p in candidates if p.is_file() and p.suffix in YAML_SUFFIXES}
    return sorted(found)


def in_model(target: Path, rel: str) -> bool:
    return rel.endswith(YAML_SUFFIXES) and any(rel == p or rel.startswith(p + "/") for p in model_paths(target))


def elements(doc: dict) -> dict[str, tuple[str, list[str]]]:
    """{element id: (content, the requirements it traces to)} for the slices, examples, states and rules."""
    out: dict[str, tuple[str, list[str]]] = {}

    def put(eid: str, content: object, reqs: object) -> None:
        out[eid] = (json.dumps(content, sort_keys=True, default=str), [str(r) for r in reqs or []])

    for s in doc.get("slices") or []:
        if not isinstance(s, dict):
            continue
        put(f"slice {s.get('id')}", {k: v for k, v in s.items() if k != "specs"}, s.get("reqs"))
        for spec in s.get("specs") or []:
            name = spec.get("name") or json.dumps(spec, sort_keys=True, default=str)[:60]
            put(f"example {s.get('id')}/{name}", spec, [*(spec.get("reqs") or []), *(s.get("reqs") or [])])
    for name, state in (doc.get("states") or {}).items():
        put(f"state {name}", state, (state or {}).get("reqs"))
    for rid, rule in (doc.get("rules") or {}).items():
        put(f"rule {rid}", rule, (rule or {}).get("reqs"))
    return out


def model_examples(target: Path) -> list[Example]:
    found = []
    for rel in model_files(target):
        for s in _load_yaml((target / rel).read_text(encoding="utf-8")).get("slices") or []:
            if not isinstance(s, dict) or s.get("type") == "view":
                continue
            for spec in s.get("specs") or []:
                if isinstance(spec, dict) and spec.get("name"):
                    reqs = [str(r) for r in [*(spec.get("reqs") or []), *(s.get("reqs") or [])]]
                    found.append(Example(rel, str(s.get("id")), str(spec["name"]), list(dict.fromkeys(reqs))))
    return found


# ----------------------------------------------------------------------------------------------- the tests
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


def _owners_of_test(path: str, requirements: list[Requirement]) -> list[Requirement]:
    """The requirements an acceptance file belongs to: those that list it, those its name's tag is theirs (sk1, s02),
    and, when no tag matches, those its name's prefix names (test_sk9_x.py: SK)."""
    match = TEST_TAG.match(Path(path).name)
    tag = match.group(1) if match else ""
    owners = [r for r in requirements if path in r.approved_tests or tag in r.tags]
    if not any(tag in r.tags for r in owners):
        owners += [r for r in requirements if r not in owners and r.names & _named_prefixes(Path(path).name)]
    return owners


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
    """{test name: file:line} for every `def test_<tag>_...` in tests/acceptance."""
    found: dict[str, str] = {}
    for rel in acceptance_files(target):
        if not rel.endswith(".py"):
            continue
        text = (target / rel).read_text(encoding="utf-8", errors="replace")
        for match in TEST_DEF.finditer(text):
            found.setdefault(match.group(1), f"{rel}:{text.count(chr(10), 0, match.start()) + 1}")
    return found


# ----------------------------------------------------------------------------------------------- --base
def unapproved_changes(target: Path, ref: str, requirements: list[Requirement]) -> list[str]:
    """Changes since `ref` without an H5 approval naming the requirement they belong to: to an approved requirement
    file (or a new one that redefines an approved requirement), to a spec element that traces to an approved
    requirement, to tests/acceptance.

    Marks the requirements concerned and returns one line per such change."""
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

    def judge(what: str, owners: list[Requirement], names: set[str], about: str = "") -> None:
        if approved(names) if names else approvals:
            return
        who = " or ".join(sorted(names)) if names else "any requirement"
        why = f"{about}changed since {ref}, and no H5 approval naming {who} was added to {HITL_LOG}"
        lines.append(f"{what}: changed without approval ({why})")
        for owner in owners:
            owner.changes.append(what)

    for path in sorted(changed):
        if path.startswith(f"{REQUIREMENTS}/") and path.endswith((".md", *YAML_SUFFIXES)):
            owners = [by_path[path]] if path in by_path else []
            text = (target / path).read_text(encoding="utf-8") if (target / path).is_file() else ""
            if not owners and not _approved_text(path, _git(target, "show", f"{base}:./{path}", check=False)):
                # not approved, before or now: a draft, unless it redefines an approved requirement
                keys = _key_of(path, text)
                owners = [r for r in requirements if r.names & keys]
                if not owners:
                    continue
            judge(path, owners, {name for owner in owners for name in owner.names} or {Path(path).stem})
        elif path.startswith(f"{ACCEPTANCE}/"):
            owners = _owners_of_test(path, requirements)
            judge(path, owners, {name for owner in owners for name in owner.names})
        elif in_model(target, path):
            old = elements(_load_yaml(_git(target, "show", f"{base}:./{path}", check=False)))
            new = elements(_load_yaml((target / path).read_text(encoding="utf-8"))) if (target / path).is_file() else {}
            groups: dict[tuple[str, ...], list[str]] = {}  # the approved requirements' paths -> elements changed
            for eid in sorted(set(old) | set(new)):
                if old.get(eid, ("", []))[0] == new.get(eid, ("", []))[0]:
                    continue
                refs = set(old.get(eid, ("", []))[1]) | set(new.get(eid, ("", []))[1])
                owned = tuple(sorted(r.path for r in requirements if r.key and refs & set(r.criteria)))
                if owned:
                    groups.setdefault(owned, []).append(eid)
            for owned, eids in sorted(groups.items()):
                owners = [by_path[p] for p in owned]
                names = {name for owner in owners for name in owner.names}
                shown = ", ".join(eids[:3]) + (f" and {len(eids) - 3} more" if len(eids) > 3 else "")
                keys = " and ".join(sorted(owner.key for owner in owners))
                judge(path, owners, names, f"{shown}, tracing to {keys}: ")
    return lines


# ----------------------------------------------------------------------------------------------- the gate
def read_requirements(target: Path) -> tuple[list[Requirement], list[str]]:
    requirements, problems = [], []
    folder = target / REQUIREMENTS
    paths = sorted(p for p in folder.glob("*") if p.is_file() and p.suffix in (".md", *YAML_SUFFIXES))
    for path in paths:
        rel = path.relative_to(target).as_posix()
        text = path.read_text(encoding="utf-8")
        if rel.endswith(YAML_SUFFIXES) and not _load_yaml(text):
            problems.append(f"{rel}: cannot be read as a requirement file (YAML with key, status and requirements)")
            continue
        requirement = parse_requirement(rel, text)
        if requirement is not None:
            requirements.append(requirement)
    retired = {path for requirement in requirements for path in requirement.supersedes}
    requirements = [requirement for requirement in requirements if requirement.path not in retired]
    examples = model_examples(target)
    for requirement in requirements:
        if requirement.key:
            requirement.examples = {c: [e for e in examples if c in e.reqs] for c in requirement.criteria}
    return requirements, problems


def criterion_status(
    requirement: Requirement, criterion: str, declared: dict[str, str], results: dict[str, list[Outcome]]
) -> tuple[str, list[str]]:
    prefix = f"test_{tag_of(criterion)}_"
    known = {*declared, *results}
    tests = {name for name in known if name.startswith(prefix)}
    untested = []
    for example in requirement.examples.get(criterion, []):
        pinned = [name for name in example.test_names if name in known]
        tests.update(pinned)
        if not pinned:
            untested.append(example)
    if requirement.changes:
        return "changed without approval", []
    if not requirement.approver:
        return "no approver", [f"{requirement.path}: approved, but approved_by is empty: only a person approves"]
    if not requirement.red_record:
        return "no red record", [f"{requirement.path}: record the acceptance run that failed before the work"]
    if not tests or untested:
        if not untested:
            return "no test", [f"{requirement.path}: no test in {ACCEPTANCE} is named {prefix}…"]
        return "no test", [
            f"{example.file}: {example.label} has no test: name one {' or '.join(example.test_names)}"
            for example in untested
        ]
    details = []
    for name in sorted(tests):
        outcomes = results.get(name)
        if not outcomes:
            details.append(f"{declared.get(name, ACCEPTANCE)}: {name} did not run")
        details += [outcome.detail for outcome in outcomes or [] if not outcome.passed]
    return ("red" if details else "green"), details


def gate(target: Path, ref: str | None) -> int:
    requirements, path_lines = read_requirements(target)
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
            status, details = criterion_status(requirement, criterion, declared, results)
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
