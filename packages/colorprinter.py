"""Coloured console output for the ingestion and chat pipelines.

Replaces the small private `colorprinter` package the predecessor depended on.
Colour is suppressed automatically when stdout is not a terminal, and when the
NO_COLOR convention (https://no-color.org) is in effect — so piping output to a
file or a systemd journal yields clean text.
"""
from __future__ import annotations

import os
import sys
from typing import Any

_CODES = {
    "red": "\033[31m",
    "green": "\033[32m",
    "yellow": "\033[33m",
    "blue": "\033[34m",
    "purple": "\033[35m",
}
_RESET = "\033[0m"


def _colour_enabled() -> bool:
    if os.environ.get("NO_COLOR") is not None:
        return False
    if os.environ.get("FORCE_COLOR"):
        return True
    return sys.stdout.isatty()


def _emit(colour: str, *args: Any, **kwargs: Any) -> None:
    text = " ".join(str(a) for a in args)
    if _colour_enabled():
        text = f"{_CODES[colour]}{text}{_RESET}"
    print(text, **kwargs)


def print_red(*args: Any, **kwargs: Any) -> None:
    """Errors and blocked operations."""
    _emit("red", *args, **kwargs)


def print_yellow(*args: Any, **kwargs: Any) -> None:
    """Warnings, timings, and retries."""
    _emit("yellow", *args, **kwargs)


def print_green(*args: Any, **kwargs: Any) -> None:
    """Successful completion of a step."""
    _emit("green", *args, **kwargs)


def print_blue(*args: Any, **kwargs: Any) -> None:
    """Informational trace, e.g. the SQL a tool is about to run."""
    _emit("blue", *args, **kwargs)


def print_purple(*args: Any, **kwargs: Any) -> None:
    """Secondary trace."""
    _emit("purple", *args, **kwargs)


__all__ = ["print_red", "print_yellow", "print_green", "print_blue", "print_purple"]
