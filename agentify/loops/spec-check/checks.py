"""The project's own checks of three spec layers, where agentify's scripts list a problem but do not fail on it.

    checks.py glossary   GLOSSARY MODEL   a name in the event model uses a word the glossary lists under `not`;
                                          a term without a meaning, twice, or also listed as a synonym
    checks.py tables     MODEL            a decision table in the model's `rules:` with a combination of inputs no
                                          row decides and no `undecided` entry asks about, an overlap, a value
                                          outside its domain
    checks.py lifecycles LIFECYCLES MODEL a status no transition reaches, a status other than a final one with no
                                          way out, a (status, command) left undecided, a transition or an error the
                                          event model does not have

MODEL is the event model's folder (or one file). One line per finding, `<file>: <what> — <how to fix>`, then a
summary. Exit 0 when the layer is whole, 1 when the check fires, 2 when it cannot run. Python 3.9+ and PyYAML.
"""

from __future__ import annotations

import itertools
import re
import sys
from pathlib import Path

try:
    import yaml
except ImportError:  # pragma: no cover
    sys.exit("checks.py needs PyYAML")

WORD = re.compile(r"[A-Z]?[a-z]+|[A-Z]+(?![a-z])|\d+")  # the word split of specs.py's glossary check


def say(line: str) -> None:
    sys.stdout.write(line + "\n")


def read(path: Path) -> dict:
    with path.open(encoding="utf-8") as fh:
        doc = yaml.safe_load(fh)
    return doc if isinstance(doc, dict) else {}


def model_files(target: Path) -> list[Path]:
    if target.is_dir():
        return sorted(p for p in target.rglob("*") if p.suffix in (".yaml", ".yml") and p.is_file())
    return [target] if target.is_file() else []


def words(name: str) -> list[str]:
    return [w.lower() for w in WORD.findall(str(name))]


# ----------------------------------------------------------------------------------------------- glossary
def model_names(doc: dict) -> list[tuple[str, str]]:
    """(name, what it is) for every name in an event model file that a test or a fixture reuses."""
    names = [(str(sc), "screen") for sc in doc.get("screens") or {}]
    names += [(str(st), "state") for st in doc.get("states") or {}]
    for s in doc.get("slices") or []:
        if s.get("type") == "view":
            rm = s.get("read_model") or {}
            names.append((str(rm.get("name", "")), "read model"))
            names += [(str(f), f"field of {rm.get('name')}") for f in rm.get("fields") or {}]
            names += [(str(sc), "screen") for sc in s.get("screens") or []]
            continue
        cmd = s.get("command") or {}
        names.append((str(cmd.get("name", "")), "command"))
        names += [(str(f), f"field of {cmd.get('name')}") for f in cmd.get("fields") or {}]
        names += [(str(ev), "event") for ev in s.get("events") or {}]
        names += [(str(e), "error") for e in s.get("errors") or []]
    return names


def uses(name_words: list[str], bad: list[str]) -> bool:
    """Whether the words of a name contain the words of a synonym, in a row."""
    n = len(bad)
    return n > 0 and any(name_words[i : i + n] == bad for i in range(len(name_words) - n + 1))


def check_glossary(glossary: Path, model: Path) -> list[str]:
    rel = glossary.as_posix()
    if not glossary.is_file():
        return [f"{rel}: missing — create it with `terms:`, each with term, meaning and not"]
    found, terms, synonyms = [], {}, {}
    for i, entry in enumerate(read(glossary).get("terms") or []):
        term = str((entry or {}).get("term") or "").strip()
        if not term:
            found.append(f"{rel}: terms[{i}] has no term — name it, or remove the entry")
            continue
        if not str(entry.get("meaning") or "").strip():
            found.append(f"{rel}: '{term}' has no meaning — say what it is, in the product's words")
        if term.lower() in terms:
            found.append(f"{rel}: '{term}' is listed twice — keep one entry")
        terms[term.lower()] = term
        for bad in entry.get("not") or []:
            key = str(bad).lower()
            if key in synonyms and synonyms[key] != term:
                found.append(f"{rel}: '{bad}' is a synonym of both '{synonyms[key]}' and '{term}' — keep it under one")
            synonyms[key] = term
    for bad, term in synonyms.items():
        if bad in terms:
            found.append(f"{rel}: '{terms[bad]}' is a term and also a word to avoid for '{term}' — drop one")
    for path in model_files(model):
        for name, what in model_names(read(path)):
            name_words = words(name)
            for bad, term in synonyms.items():
                if uses(name_words, words(bad.replace(" ", "_"))) or bad == name.lower():
                    found.append(f"{path.as_posix()}: {what} '{name}' uses '{bad}' — the glossary says '{term}'")
    return found


# ----------------------------------------------------------------------------------------------- tables
def merged(model: Path, key: str, found: list[str]) -> dict:
    out: dict = {}
    for path in model_files(model):
        for k, v in (read(path).get(key) or {}).items():
            if k in out:
                found.append(f"{path.as_posix()}: {key}.{k} is defined twice — keep one")
            out[k] = v
    return out


def check_tables(model: Path) -> list[str]:
    found: list[str] = []
    if not model_files(model):
        return [f"{model.as_posix()}: no event model files"]
    domains = merged(model, "domains", found)
    rules = merged(model, "rules", found)
    where = model.as_posix()
    for rid, rule in rules.items():
        rule = rule or {}
        if rule.get("kind") != "table":
            continue
        inputs = rule.get("inputs") or []
        missing_domains = [d for d in inputs if d not in domains]
        for d in missing_domains:
            found.append(f"{where}: rule {rid}: input '{d}' has no domain — list its values under `domains:`")
        decided: dict[tuple, int] = {}
        for row in rule.get("rows") or []:
            if not isinstance(row, list) or len(row) != len(inputs) + 1:
                found.append(f"{where}: rule {rid}: row {row} needs {len(inputs)} input(s) and one output")
                continue
            key = tuple(str(v) for v in row[: len(inputs)])
            decided[key] = decided.get(key, 0) + 1
        for key, count in decided.items():
            if count > 1:
                found.append(f"{where}: rule {rid}: {'/'.join(key)} is decided {count} times — keep one row")
        undecided = set()
        for item in rule.get("undecided") or []:
            key = tuple(str(v) for v in (item or {}).get("inputs") or [])
            if not str((item or {}).get("question") or "").strip():
                found.append(f"{where}: rule {rid}: undecided {'/'.join(key)} has no question — ask it, or decide it")
            if key in decided:
                found.append(f"{where}: rule {rid}: {'/'.join(key)} is both a row and undecided — drop one")
            undecided.add(key)
        if missing_domains:
            continue
        values = [[str(v) for v in domains[d] or []] for d in inputs]
        for key in [*decided, *undecided]:
            for i, v in enumerate(key[: len(inputs)]):
                if v not in values[i]:
                    found.append(f"{where}: rule {rid}: '{v}' is not a value of {inputs[i]} {values[i]}")
        if rule.get("unknown"):
            continue  # the whole table is a question (unknown: true)
        for combo in itertools.product(*values):
            if combo not in decided and combo not in undecided:
                found.append(
                    f"{where}: rule {rid}: no row decides {'/'.join(combo)} — add the row, or ask: "
                    f"`undecided: [{{inputs: [{', '.join(combo)}], question: ...}}]`"
                )
    return found


# ----------------------------------------------------------------------------------------------- lifecycles
def check_lifecycles(lifecycles: Path, model: Path) -> list[str]:
    rel = lifecycles.as_posix()
    if not lifecycles.is_file():
        return [f"{rel}: missing — create it with `lifecycles: {{}}`"]
    found: list[str] = []
    read_models: dict[str, dict] = {}
    slices: dict[str, dict] = {}
    by_command: dict[str, list[dict]] = {}
    for path in model_files(model):
        for s in read(path).get("slices") or []:
            slices[str(s.get("id"))] = s
            if s.get("type") == "view":
                rm = s.get("read_model") or {}
                read_models[str(rm.get("name"))] = rm.get("fields") or {}
            else:
                by_command.setdefault(str((s.get("command") or {}).get("name")), []).append(s)
    for entity, lc in (read(lifecycles).get("lifecycles") or {}).items():
        lc = lc or {}
        at = f"{rel}: {entity}"
        rm, _, field = str(lc.get("field") or "").partition(".")
        sources = (read_models.get(rm) or {}).get(field)
        built_from = {str(x).split(".")[0] for x in sources or []}  # the events the status field is built from
        if sources is None:
            found.append(
                f"{at}: field '{lc.get('field')}' is not a read-model field of the event model — name the "
                f"<ReadModel>.<field> that holds the status"
            )
        statuses = [str(s) for s in lc.get("statuses") or []]
        if not statuses or len(set(statuses)) != len(statuses):
            found.append(f"{at}: statuses must be a list of distinct values")
        initial, finals = str(lc.get("initial") or ""), [str(f) for f in lc.get("final") or []]
        if initial not in statuses:
            found.append(f"{at}: initial '{initial}' is not one of the statuses {statuses}")
        found += [f"{at}: final '{f}' is not one of the statuses" for f in finals if f not in statuses]
        moves: dict[str, set[str]] = {s: set() for s in statuses}
        for command, decisions in (lc.get("commands") or {}).items():
            decisions = decisions or {}
            if command not in by_command:
                found.append(f"{at}: command {command} is not in the event model — add its slice, or drop it here")
            for status in decisions:
                if str(status) not in statuses:
                    found.append(f"{at}: {command} decides '{status}', which is not a status")
            for status in statuses:
                d = decisions.get(status)
                where = f"{at}: ({status}, {command})"
                if d is None:
                    found.append(f"{where} is undecided — a transition {{to, slice}}, {{error}}, or impossible")
                elif d == "impossible":
                    continue
                elif isinstance(d, dict) and "to" in d:
                    to, sid = str(d["to"]), str(d.get("slice"))
                    if to not in statuses:
                        found.append(f"{where}: goes to '{to}', which is not a status")
                    s = slices.get(sid)
                    if s is None or str((s.get("command") or {}).get("name")) != command:
                        found.append(f"{where}: slice {sid} is not a slice of the event model with command {command}")
                    elif sources is not None and not built_from & set(s.get("events") or {}):
                        found.append(f"{where}: slice {sid} records no event that {lc.get('field')} is built from")
                    moves.setdefault(status, set()).add(to)
                elif isinstance(d, dict) and "error" in d:
                    errors = {str(e) for s in by_command.get(command, []) for e in s.get("errors") or []}
                    if str(d["error"]) not in errors:
                        found.append(f"{where}: error {d['error']} is not in the errors of {command}'s slice")
                else:
                    found.append(f"{where}: {d!r} is not a transition {{to, slice}}, an {{error}} or impossible")
        reached, todo = {initial}, [initial]
        while todo:
            for nxt in moves.get(todo.pop(), set()):
                if nxt not in reached:
                    reached.add(nxt)
                    todo.append(nxt)
        for status in statuses:
            out = moves.get(status, set()) - {status}
            if status not in reached:
                found.append(f"{at}: status '{status}' is unreachable from '{initial}' — add the transition into it")
            if status in finals and out:
                found.append(f"{at}: final status '{status}' has a way out to {sorted(out)} — it is not final")
            if status not in finals and not out:
                found.append(f"{at}: status '{status}' is a dead end — add a way out, or list it under final")
    return found


def main(argv: list[str]) -> int:
    usage = __doc__.split("\n\n")[1]
    if len(argv) < 2:
        say(usage)
        return 2
    layer, args = argv[1], [Path(a) for a in argv[2:]]
    try:
        if layer == "glossary" and len(args) == 2:
            found = check_glossary(*args)
        elif layer == "tables" and len(args) == 1:
            found = check_tables(*args)
        elif layer == "lifecycles" and len(args) == 2:
            found = check_lifecycles(*args)
        else:
            say(usage)
            return 2
    except (OSError, yaml.YAMLError) as exc:
        say(f"checks.py {layer}: cannot run: {exc}")
        return 2
    for line in found:
        say(line)
    say(f"checks.py {layer}: {len(found)} problem(s); {'fires' if found else 'passes'}")
    return 1 if found else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
