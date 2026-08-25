"""Load prompts from files instead of Python string constants.

Two reasons this is worth the indirection:

* Prompts are the main thing a fork for another parliament has to rewrite, and
  editing Markdown is a different job from editing Python.
* With PROMPTS_RELOAD=1 the files are re-read on every call, so prompt iteration
  no longer needs a server restart.

Placeholders use ``$name`` / ``${name}`` (:class:`string.Template`), **not**
``{name}``. Several prompts embed literal JSON braces, which ``str.format`` would
raise on; and ``safe_substitute`` leaves an unknown placeholder alone rather than
killing a live chat turn over a typo.

    from prompts_loader import load_prompt
    ORCHESTRATOR_SYSTEM = load_prompt("chat/orchestrator")
"""
from __future__ import annotations

import os
import re
from datetime import date
from functools import cache
from pathlib import Path
from string import Template
from typing import Any

from parliament import PARLIAMENT

_ROOT = Path(__file__).resolve().parent
def _resolve_dir(value: str | None, default: Path) -> Path:
    """A relative PROMPTS_DIR is repo-relative, not cwd-relative."""
    if not value:
        return default
    path = Path(value)
    return path if path.is_absolute() else (_ROOT / path)


PROMPTS_DIR = _resolve_dir(os.environ.get("PROMPTS_DIR"), _ROOT / "prompts")

# {{include:path/to/partial}} — expanded before substitution so a shared block
# (the schema reference, say) has exactly one source.
_INCLUDE_RE = re.compile(r"^[ \t]*\{\{include:([\w/\-.]+)\}\}[ \t]*$", re.MULTILINE)

_MAX_INCLUDE_DEPTH = 5


class PromptNotFound(FileNotFoundError):
    pass


def _reload_enabled() -> bool:
    return os.environ.get("PROMPTS_RELOAD", "").lower() in {"1", "true", "yes"}


def _candidates(name: str) -> list[Path]:
    """Language directory first, then the language-neutral and English fallbacks."""
    lang = PARLIAMENT.language.prompt_language
    return [
        PROMPTS_DIR / lang / f"{name}.md",
        PROMPTS_DIR / f"{name}.md",
        PROMPTS_DIR / "en" / f"{name}.md",
    ]


def _resolve(name: str) -> Path:
    for path in _candidates(name):
        if path.exists():
            return path
    tried = "\n  ".join(str(p) for p in _candidates(name))
    raise PromptNotFound(f"No prompt named {name!r}. Looked in:\n  {tried}")


def _expand_includes(text: str, depth: int = 0) -> str:
    if depth >= _MAX_INCLUDE_DEPTH:
        raise RecursionError(f"{{{{include:}}}} nested more than {_MAX_INCLUDE_DEPTH} deep")

    def replace(match: re.Match) -> str:
        return _expand_includes(_resolve(match.group(1)).read_text(encoding="utf-8"), depth + 1)

    return _INCLUDE_RE.sub(replace, text)


def base_context() -> dict[str, Any]:
    """Values available to every prompt without being passed explicitly.

    Domain words come from `vocabulary:` so a prompt can say "$speech_plural"
    and read naturally in any parliament's own language.
    """
    ids = PARLIAMENT.ids
    return {
        "parliament_name": PARLIAMENT.meta.get("name", ""),
        "parliament_name_en": PARLIAMENT.meta.get("name_en", ""),
        "country": PARLIAMENT.meta.get("country", ""),
        "data_start_year": PARLIAMENT.meta.get("data_start_year", ""),
        "fts_config": PARLIAMENT.language.fts_config,
        "answer_language": PARLIAMENT.language.name_en or PARLIAMENT.language.prompt_language,
        "answer_language_native": PARLIAMENT.language.name or PARLIAMENT.language.prompt_language,
        "preserve_characters": PARLIAMENT.language.preserve_characters,
        "party_codes": ", ".join(PARLIAMENT.party_codes),
        "date_today": date.today().isoformat(),
        "person_id_example": ids.get("person_id", {}).get("example", ""),
        "speech_id_example": ids.get("speech_id", {}).get("example", ""),
        "doc_id_example": ids.get("doc_id", {}).get("example", ""),
        "debate_id_example": ids.get("debate_id", {}).get("example", ""),
        **PARLIAMENT.vocabulary,
    }


@cache
def _load_cached(name: str) -> str:
    return _expand_includes(_resolve(name).read_text(encoding="utf-8"))


def load_prompt(name: str, **extra: Any) -> str:
    """Return a prompt with placeholders filled in.

    Args:
        name: Path under prompts/ without the .md suffix, e.g. "chat/orchestrator".
        **extra: Additional placeholder values, overriding the base context.
    """
    raw = _expand_includes(_resolve(name).read_text(encoding="utf-8")) if _reload_enabled() \
        else _load_cached(name)
    return Template(raw).safe_substitute({**base_context(), **extra})


def tool_doc(name: str, **extra: Any) -> str:
    """Load a tool description from prompts/tools/<name>.md.

    Passed as ``@register_tool(description=...)``, which overrides the docstring
    description while leaving ``Args:`` parsing to the docstring — so the country-
    specific prose lives in a file without disturbing schema generation.
    """
    return load_prompt(f"tools/{name}", **extra)


def clear_cache() -> None:
    _load_cached.cache_clear()
