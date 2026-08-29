"""The schema the model reads is generated from the database, and stays that way.

The column list was maintained by hand until it accumulated `debates.debate`,
`document_proposals.nummer` and `behandlas_i` — none of which existed, so every
query touching them failed — plus a warning that speeches have NULL `debate_id`
(none do) and a claim that `people.birth_year` is an integer (it is text).

Generation removes that whole class of bug. These tests keep it removed:

* every column must be decided, so a new one cannot be silently forgotten;
* the committed prompt must match what the database would generate right now;
* notes stay short, so the prompt cannot creep;
* the tables the model is told about are the tables the SQL guard permits.

Skipped when no database is reachable, so the suite still runs offline.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

MAX_NOTE_CHARS = 80
# The generated block reaches every agent that can write SQL, on every turn. It is
# ~729 tokens today; this cap leaves room to describe a new column without letting
# the block quietly double.
MAX_BLOCK_TOKENS = 800
CHARS_PER_TOKEN = 3.54

HIDE = "[hide]"
PLAIN = "-"


def _rows():
    """Column comments for the corpus tables, or skip if there is no database."""
    try:
        from postgres_client import pg
        from scripts.generate_schema_prompt import TABLES, _QUERY

        return pg.execute(_QUERY, (TABLES, TABLES))
    except Exception as exc:  # no database configured, or unreachable
        pytest.skip(f"database not reachable: {str(exc).splitlines()[0][:80]}")


def test_every_column_is_decided():
    """A column with no comment is an unmade decision, not a default.

    This is the routine: add a column, and the suite fails until someone says
    whether the model should see it.
    """
    undecided = [f"{r['table_name']}.{r['column_name']}"
                 for r in _rows() if r["comment"] is None]
    assert not undecided, (
        "These columns have no COMMENT, so nobody has decided whether the model may "
        "use them. Add one to _postgres/migrations/add_column_comments.sql — '-' if "
        "the name says it, '[hide] reason' to keep it out, or a one-line note:\n  "
        + "\n  ".join(undecided)
    )


def test_notes_stay_short():
    too_long = [
        f"{r['table_name']}.{r['column_name']} ({len(r['comment'])} chars)"
        for r in _rows()
        if r["comment"] and not r["comment"].startswith(HIDE)
        and r["comment"] != PLAIN and len(r["comment"]) > MAX_NOTE_CHARS
    ]
    assert not too_long, (
        f"Column notes must fit on one line of {MAX_NOTE_CHARS} characters. Anything "
        "longer belongs in the prose in prompts/en/_shared/schema.md:\n  "
        + "\n  ".join(too_long)
    )


def test_generated_prompt_is_current():
    """The committed file must match what the database would produce now."""
    from scripts.generate_schema_prompt import OUT, build

    try:
        expected = build()
    except Exception as exc:
        pytest.skip(f"database not reachable: {str(exc).splitlines()[0][:80]}")
    assert OUT.exists(), f"{OUT} is missing — run scripts/generate_schema_prompt.py"
    assert OUT.read_text(encoding="utf-8") == expected, (
        f"{OUT.relative_to(ROOT)} is out of date. Run:\n"
        "    python scripts/generate_schema_prompt.py"
    )


def test_generated_block_stays_within_budget():
    from scripts.generate_schema_prompt import OUT

    if not OUT.exists():
        pytest.skip("schema block not generated yet")
    tokens = len(OUT.read_text(encoding="utf-8")) / CHARS_PER_TOKEN
    assert tokens <= MAX_BLOCK_TOKENS, (
        f"The schema block is ~{tokens:.0f} tokens, over the {MAX_BLOCK_TOKENS} budget. "
        "Shorten the column notes, or hide a column that is not earning its place."
    )


def test_the_model_is_only_told_about_tables_it_may_read():
    """The prompt and the SQL guard must describe the same six tables.

    Deliberately an equality check rather than deriving one from the other: a
    security control should not change because someone edited a comment.
    """
    from backend.services.llm_tools import _LLM_READABLE_TABLES
    from scripts.generate_schema_prompt import TABLES

    assert set(TABLES) == set(_LLM_READABLE_TABLES), (
        "scripts/generate_schema_prompt.TABLES and llm_tools._LLM_READABLE_TABLES "
        "have drifted. The model must not be shown a table it cannot query, nor be "
        "able to query one it was never told about."
    )
