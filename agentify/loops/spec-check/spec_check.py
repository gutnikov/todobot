"""spec-check: `specs.py check` and the check of every spec layer in agentify/specs/layers.yaml, on a project.

    run DIR

1. `specs.py check --root DIR`: the preferences, the layers, the requirement files and the requirement <-> spec
   trace (a `reqs:` reference to a requirement nobody wrote, an approved requirement no spec element covers).
2. Every layer of DIR/agentify/specs/layers.yaml with a `check`, unless it is proposed or retired: the command, run
   in DIR, with `<agentify>` and `<spec-check>` filled in and `python3` this loop's Python, which has PyYAML.

One line per check, `[<layer>] <command>: passes | fires`, the output of a check that fires indented under it, and
a summary. Exit 0 when every check passes, 1 when one fires, 2 when the loop cannot run.

agentify's scripts come from AGENTIFY_ROOT, else from Claude Code's plugin cache,
~/.claude/plugins/cache/agentify/agentify/0.4.0. The loop is validated with agentify 0.4.0 (commit eaff327); it
refuses another version, so that a newer check does not change the verdict unnoticed.
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
AGENTIFY_VERSION = "0.4.0"
AGENTIFY_CACHE = Path.home() / ".claude" / "plugins" / "cache" / "agentify" / "agentify" / AGENTIFY_VERSION
LAYERS = Path("agentify") / "specs" / "layers.yaml"
TIMEOUT = 120


class CannotRunError(Exception):
    """The loop cannot run: exit 2."""


def say(line: str = "") -> None:
    sys.stdout.write(line + "\n")


def agentify_root() -> Path:
    root = Path(os.environ.get("AGENTIFY_ROOT") or AGENTIFY_CACHE).expanduser().resolve()
    if not (root / "parts" / "spec" / "scripts" / "specs.py").is_file():
        raise CannotRunError(
            f"agentify's scripts are not in {root}: install the agentify plugin {AGENTIFY_VERSION}, or set "
            f"AGENTIFY_ROOT to a checkout of github.com/gutnikov/agentify at {AGENTIFY_VERSION}"
        )
    manifest = root / ".claude-plugin" / "plugin.json"
    version = json.loads(manifest.read_text(encoding="utf-8")).get("version") if manifest.is_file() else None
    if version != AGENTIFY_VERSION:
        raise CannotRunError(
            f"{root} is agentify {version}, and this loop is validated with {AGENTIFY_VERSION}: point AGENTIFY_ROOT "
            f"at {AGENTIFY_VERSION}, or validate the loop again with {version} and change AGENTIFY_VERSION"
        )
    return root


def checks(target: Path, agentify: Path) -> list[tuple[str, str, list[str]]]:
    """(label, command as written, argv) for specs.py check and each layer's check."""
    out = [("specs", "specs.py check", [sys.executable, str(agentify / "parts/spec/scripts/specs.py"), "check"])]
    out[0][2].extend(["--root", str(target)])
    path = target / LAYERS
    if not path.is_file():
        return out  # specs.py check reports the missing layers.yaml
    layers = (yaml.safe_load(path.read_text(encoding="utf-8")) or {}).get("layers") or []
    for layer in layers:
        command = str(layer.get("check") or "").strip()
        if not command or layer.get("status") in ("proposed", "retired"):
            continue
        filled = command.replace("<agentify>", str(agentify)).replace("<spec-check>", str(HERE))
        argv = shlex.split(filled)
        if argv and argv[0] in ("python3", "python"):
            argv[0] = sys.executable
        out.append((str(layer.get("id")), command, argv))
    return out


def main() -> int:
    if len(sys.argv) != 2 or sys.argv[1].startswith("-"):
        say(__doc__.split("\n\n")[1])
        return 2
    target = Path(sys.argv[1]).resolve()
    try:
        agentify = agentify_root()
        todo = checks(target, agentify)
    except (CannotRunError, OSError, yaml.YAMLError) as exc:
        say(f"spec-check: cannot run: {exc}")
        return 2
    fired = 0
    for label, command, argv in todo:
        try:
            done = subprocess.run(argv, cwd=target, capture_output=True, text=True, timeout=TIMEOUT, check=False)  # noqa: S603
            code, text = done.returncode, (done.stdout + done.stderr).rstrip()
        except (OSError, subprocess.TimeoutExpired) as exc:
            code, text = None, f"did not finish: {exc}"
        verdict = "passes" if code == 0 else "fires"
        say(f"[{label}] {command}: {verdict}")
        if code != 0:
            fired += 1
            say("\n".join(f"    {line}" for line in text.splitlines()))
    say(f"spec-check: {len(todo) - fired} of {len(todo)} checks pass; {'fires' if fired else 'passes'}")
    return 1 if fired else 0


if __name__ == "__main__":
    sys.exit(main())
