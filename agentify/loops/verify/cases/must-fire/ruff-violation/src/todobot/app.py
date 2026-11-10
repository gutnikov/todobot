"""ruff fires: an unused import (F401)."""

from __future__ import annotations

import os


def greet(name: str) -> str:
    return f"hello, {name}"
