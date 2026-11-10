"""mypy fires: the return value does not match the annotation."""

from __future__ import annotations


def count(name: str) -> int:
    return name
