"""Prompts must survive being moved out of Python unchanged.

The snapshots under tests/golden/prompts/ were captured from the module-level
constants before they became files. Any drift here means the model is being given
different instructions than the ones that were evaluated, which is the kind of
regression that shows up as subtly worse answers rather than as a failure.

Regenerate deliberately, never casually:

    python tests/test_prompts_golden.py --update
"""
from __future__ import annotations

import sys
from pathlib import Path

try:
    import pytest
except ModuleNotFoundError:  # capture mode runs without the dev extras installed
    pytest = None

ROOT = Path(__file__).resolve().parents[1]
GOLDEN = Path(__file__).parent / "golden" / "prompts"

sys.path.insert(0, str(ROOT))

# name -> (module, attribute). Kept explicit rather than discovered, so adding a
# prompt is a deliberate act that shows up in review.
PROMPTS: dict[str, tuple[str, str]] = {
    "chat/orchestrator": ("backend.services.chat", "ORCHESTRATOR_SYSTEM"),
    "chat/worker": ("backend.services.chat", "WORKER_SYSTEM"),
    "chat/fact_checker": ("backend.services.chat", "FACT_CHECKER_SYSTEM"),
    "chat/language_checker": ("backend.services.chat", "LANGUAGE_CHECKER_SYSTEM"),
    "chat/paragraph_rewriter": ("backend.services.chat", "PARAGRAPH_REWRITER_SYSTEM"),
    "chat/attribution_fixer": ("backend.services.chat", "ATTRIBUTION_FIXER_SYSTEM"),
    "chat/shadow_communicator": ("backend.services.chat", "_SHADOW_INSTRUCTION"),
    "chat/planner": ("backend.services.chat", "PLANNER_SYSTEM"),
    "chat/researcher": ("backend.services.chat", "RESEARCHER_SYSTEM"),
    "research/discover": ("backend.services.research.board", "_DISCOVER_SYSTEM"),
    "research/scout_query": ("backend.services.research.board", "_SCOUT_QUERY_SYSTEM"),
    "research/followup": ("backend.services.research.board", "_FOLLOWUP_SYSTEM"),
    "research/answer": ("backend.services.research.synthesis", "_ANSWER_SYSTEM"),
    "research/report": ("backend.services.research.synthesis", "_REPORT_SYSTEM"),
    "research/trip": ("backend.services.research.trip", "_TRIP_SYSTEM"),
    "research/trip_final": ("backend.services.research.trip", "_FINAL_INSTRUCTION"),
    "tools/reader": ("backend.services.llm_tools", "_READER_SYSTEM"),
}

# Prompts with no module-level constant to read back. The tool descriptions are
# the largest prompt surface in the system — the model reads them on every turn —
# so they belong under review even though they reach it through a decorator.
# The persona is built per member at request time; snapshot the unfilled template.
TOOL_DESCRIPTIONS: tuple[str, ...] = (
    "database_query", "search_speeches", "vector_search", "vector_search_debates",
    "fetch_debate", "fetch_speeches", "read_documents_for", "lookup_source",
    "search_documents", "vector_search_documents", "fetch_document", "share_insight",
)

TEMPLATE_PROMPTS: tuple[str, ...] = ("chat/mp_persona",)


def _resolve(module_name: str, attr: str) -> str:
    import importlib

    return getattr(importlib.import_module(module_name), attr)


def _stabilise(text: str) -> str:
    """Blank out values that change on their own, so the diff shows real edits.

    $date_today renders as the current date, which made every snapshot containing
    it fail the day after it was captured — a daily false alarm that teaches you
    to re-run --update without reading the diff, which is the one habit these
    snapshots exist to prevent.
    """
    from prompts_loader import base_context

    today = str(base_context().get("date_today") or "")
    return text.replace(today, "<DATE_TODAY>") if today else text


def _capture() -> dict[str, str]:
    from prompts_loader import load_prompt, tool_doc

    captured = {name: _resolve(mod, attr) for name, (mod, attr) in PROMPTS.items()}
    captured.update({f"tools/{n}": tool_doc(n) for n in TOOL_DESCRIPTIONS})
    captured.update({n: load_prompt(n) for n in TEMPLATE_PROMPTS})
    return {name: _stabilise(text) for name, text in captured.items()}


ALL_NAMES = sorted(
    list(PROMPTS)
    + [f"tools/{n}" for n in TOOL_DESCRIPTIONS]
    + list(TEMPLATE_PROMPTS)
)


@(pytest.mark.parametrize("name", ALL_NAMES) if pytest else (lambda f: f))
def test_prompt_matches_golden(name: str) -> None:
    path = GOLDEN / f"{name}.txt"
    assert path.exists(), f"No golden snapshot for {name}; run with --update to create one."
    assert _capture()[name] == path.read_text(encoding="utf-8"), (
        f"Prompt {name} differs from its golden snapshot. If the change is "
        f"intended, re-run with --update and review the diff."
    )


def main() -> int:
    if "--update" not in sys.argv:
        print(__doc__)
        return 1
    from dotenv import load_dotenv

    load_dotenv(ROOT / ".env")
    GOLDEN.mkdir(parents=True, exist_ok=True)
    for name, text in _capture().items():
        path = GOLDEN / f"{name}.txt"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        print(f"  wrote {path.relative_to(ROOT)} ({len(text)} chars)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
