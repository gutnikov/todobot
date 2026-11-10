"""The log-privacy rule: what users send must not reach the logs.

    python log_privacy.py DIR      check DIR/src/**/*.py; print `file:line: log-privacy/R<n>: message`, exit 1 if any

A static check over the AST, three rules:

- R1: `exc_info=` or `.exception(...)` outside the files in R1_ALLOWED. A traceback quotes what users wrote: a
  database error's DETAIL shows the rejected row, asyncpg and pydantic errors repr their input.
- R2: SQLAlchemy's `create_engine(...)` or `create_async_engine(...)` without `hide_parameters=True`. Without it,
  a database error carries the statement's parameters: the users' messages.
- R3: a logging call (`log_event(...)`, `logger.<level>(...)`, `logging.<level>(...)`) whose arguments read
  `.text` or `.caption`, or pass a `message` or `update` object, which carries them.

Paths are relative to DIR. The verify loop runs this as one of its checks; it needs only the standard library.
"""

from __future__ import annotations

import ast
import re
import sys
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

# Files where R1 does not apply, with why.
R1_ALLOWED = {
    "src/todobot/logs.py": "the process-level crash hooks",
    # Pending the owner's decision (agentify/loops/verify/CHANGELOG.md): main()'s last-resort catch logs
    # `<command>_failed` with the traceback, and no message is in scope to redact.
    "src/todobot/__main__.py": "main()'s last-resort catch before the process exits",
}

LEVELS = frozenset({"debug", "info", "warning", "warn", "error", "exception", "critical", "fatal", "log"})
CONTENT_ATTRS = frozenset({"text", "caption"})
CONTENT_OBJECTS = frozenset({"message", "update"})
# Calls that put their arguments into the logged value as they are: str(message) logs the message.
TRANSPARENT_CALLS = frozenset({"str", "repr", "format", "ascii", "dict", "list", "tuple", "set", "vars"})
LOGGER_NAME = re.compile(r"(^|_)log(ger)?$")

FIX_R1 = (
    "a traceback quotes what users wrote (a database error's DETAIL shows the rejected row). "
    "Log `exc=redact(format_exception(exc), _content(message))` as `on_error` in src/todobot/bot.py does, "
    "or only `error=type(exc).__name__` when no message is in scope"
)
FIX_R2 = (
    "without `hide_parameters=True` a database error carries the statement's parameters, which are users' "
    "messages. Pass `hide_parameters=True`, as `create_engine` in src/todobot/db.py does"
)
FIX_R3_CONTENT = (
    "is what a user wrote and must not be logged. Log what describes it instead: its kind, its length, "
    "the ids (`kind=item.kind`, `user_id=message.from_user.id`)"
)
FIX_R3_OBJECT = (
    "is logged whole, and with it the text and caption it carries. Log its ids instead "
    "(`update_id=update.update_id`, `user_id=message.from_user.id`)"
)


@dataclass(frozen=True)
class Finding:
    path: str
    line: int
    rule: str
    message: str

    def __str__(self) -> str:
        return f"{self.path}:{self.line}: log-privacy/{self.rule}: {self.message}"


def _name(node: ast.AST) -> str | None:
    """The last identifier of a name or an attribute chain: `logger`, `self.logger` -> "logger"."""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def _is_logger(node: ast.AST) -> bool:
    if isinstance(node, ast.Call):  # logging.getLogger(__name__).info(...)
        return _name(node.func) == "getLogger"
    name = _name(node)
    return name is not None and (name == "logging" or LOGGER_NAME.search(name.lower()) is not None)


def is_logging_call(call: ast.Call) -> bool:
    func = call.func
    if _name(func) == "log_event":
        return True
    return isinstance(func, ast.Attribute) and func.attr in LEVELS and _is_logger(func.value)


def _arguments(call: ast.Call) -> list[ast.expr]:
    return [*call.args, *(keyword.value for keyword in call.keywords)]


def content_reads(node: ast.AST) -> Iterator[ast.Attribute]:
    """Every `.text` and `.caption` in `node`, except in the secrets handed to `redact()`, which cuts them out."""
    if isinstance(node, ast.Call) and _name(node.func) == "redact":
        logged = node.args[:1] + [keyword.value for keyword in node.keywords if keyword.arg == "text"]
        for argument in logged:
            yield from content_reads(argument)
        return
    if isinstance(node, ast.Attribute) and node.attr in CONTENT_ATTRS:
        yield node
    for child in ast.iter_child_nodes(node):
        yield from content_reads(child)


def logged_objects(node: ast.AST) -> Iterator[ast.expr]:
    """A `message` or `update` that becomes part of the logged value: as the argument itself, inside an f-string,
    a container, a `%` or `+`, or `str()`/`repr()`. Reading one of its fields (`update.update_id`) or handing it to
    another function (`_content(message)`) does not log it."""
    if isinstance(node, (ast.Name, ast.Attribute)) and _name(node) in CONTENT_OBJECTS:
        yield node
    elif isinstance(node, ast.JoinedStr):
        for value in node.values:
            yield from logged_objects(value)
    elif isinstance(node, (ast.FormattedValue, ast.Starred)):
        yield from logged_objects(node.value)
    elif isinstance(node, ast.BinOp):
        yield from logged_objects(node.left)
        yield from logged_objects(node.right)
    elif isinstance(node, (ast.Tuple, ast.List, ast.Set)):
        for element in node.elts:
            yield from logged_objects(element)
    elif isinstance(node, ast.Dict):
        for value in node.values:
            yield from logged_objects(value)
    elif isinstance(node, ast.Call) and _name(node.func) in TRANSPARENT_CALLS:
        for argument in _arguments(node):
            yield from logged_objects(argument)


def _sqlalchemy_imports(tree: ast.Module) -> dict[str, str]:
    """{local name: its sqlalchemy name}: {"create_async_engine": "sqlalchemy.ext.asyncio.create_async_engine"}."""
    names: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.split(".")[0] == "sqlalchemy":
                    names[alias.asname or "sqlalchemy"] = alias.name if alias.asname else "sqlalchemy"
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and (node.module or "").split(".")[0] == "sqlalchemy":
            for alias in node.names:
                names[alias.asname or alias.name] = f"{node.module}.{alias.name}"
    return names


def _qualified(node: ast.AST, imports: dict[str, str]) -> str | None:
    if isinstance(node, ast.Name):
        return imports.get(node.id)
    if isinstance(node, ast.Attribute):
        base = _qualified(node.value, imports)
        return f"{base}.{node.attr}" if base else None
    return None


def _is_true(node: ast.expr) -> bool:
    return isinstance(node, ast.Constant) and node.value is True


def check_file(path: Path, rel: str) -> list[Finding]:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=rel)
    except (SyntaxError, UnicodeDecodeError):
        return []  # ruff and mypy report a file that does not parse
    imports = _sqlalchemy_imports(tree)
    findings: list[Finding] = []
    for call in (node for node in ast.walk(tree) if isinstance(node, ast.Call)):
        if rel not in R1_ALLOWED:
            for keyword in call.keywords:
                if keyword.arg == "exc_info" and not (
                    isinstance(keyword.value, ast.Constant) and keyword.value.value in (False, None)
                ):
                    message = f"`exc_info=` logs the traceback: {FIX_R1}"
                    findings.append(Finding(rel, keyword.value.lineno, "R1", message))
            if isinstance(call.func, ast.Attribute) and call.func.attr == "exception" and _arguments(call):
                findings.append(Finding(rel, call.lineno, "R1", f"`.exception()` logs the traceback: {FIX_R1}"))
        qualified = _qualified(call.func, imports) or ""
        if qualified.endswith((".create_engine", ".create_async_engine")) and not any(
            keyword.arg == "hide_parameters" and _is_true(keyword.value) for keyword in call.keywords
        ):
            findings.append(Finding(rel, call.lineno, "R2", f"`{qualified.rsplit('.', 1)[1]}(...)`: {FIX_R2}"))
        if is_logging_call(call):
            for argument in _arguments(call):
                for read in content_reads(argument):
                    findings.append(Finding(rel, read.lineno, "R3", f"`{ast.unparse(read)}` {FIX_R3_CONTENT}"))
                for obj in logged_objects(argument):
                    findings.append(Finding(rel, obj.lineno, "R3", f"`{ast.unparse(obj)}` {FIX_R3_OBJECT}"))
    return findings


def check(root: Path) -> list[Finding]:
    """The findings in root/src, sorted by file and line."""
    findings: list[Finding] = []
    for path in sorted((root / "src").rglob("*.py")):
        findings.extend(check_file(path, path.relative_to(root).as_posix()))
    return sorted(set(findings), key=lambda f: (f.path, f.line, f.rule, f.message))


def main(argv: list[str]) -> int:
    if len(argv) != 1:
        sys.stderr.write(__doc__ or "")
        return 2
    findings = check(Path(argv[0]))
    sys.stdout.write("".join(f"{finding}\n" for finding in findings))
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
