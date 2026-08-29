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
# The compact block goes out with every request; the full one only on demand.
MAX_ALWAYS_ON_TOKENS = 400
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


@pytest.mark.parametrize("notes", [True, False], ids=["full", "compact"])
def test_generated_prompt_is_current(notes):
    """Both committed files must match what the database would produce now."""
    from scripts.generate_schema_prompt import OUT, OUT_COMPACT, build

    path = OUT if notes else OUT_COMPACT
    try:
        expected = build(notes=notes)
    except Exception as exc:
        pytest.skip(f"database not reachable: {str(exc).splitlines()[0][:80]}")
    assert path.exists(), f"{path} is missing — run scripts/generate_schema_prompt.py"
    assert path.read_text(encoding="utf-8") == expected, (
        f"{path.relative_to(ROOT)} is out of date. Run:\n"
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


def test_the_always_on_block_stays_small():
    """The compact block rides in every request, so it is the one to watch.

    It carries column names but no notes: the names are what stop the model
    inventing one, and the explanation is what `database_schema` exists to serve.
    """
    from scripts.generate_schema_prompt import OUT_COMPACT

    if not OUT_COMPACT.exists():
        pytest.skip("compact schema block not generated yet")
    text = OUT_COMPACT.read_text(encoding="utf-8")
    tokens = len(text) / CHARS_PER_TOKEN
    assert tokens <= MAX_ALWAYS_ON_TOKENS, (
        f"The always-on schema block is ~{tokens:.0f} tokens, over "
        f"{MAX_ALWAYS_ON_TOKENS}. It is sent with every request — move detail into "
        "prompts/en/_shared/schema.md, which database_schema serves on demand."
    )
    assert "\u00b7 " not in text, "the compact block must not carry per-column notes"


def test_the_migration_matches_the_database():
    """The migration is what a rebuilt database gets, so it must be the truth.

    Comments are easy to tune with ad-hoc SQL and forget to write down. When that
    happens the drift is invisible until someone restores from scratch, runs the
    migration, and finds the generated prompt no longer matches — which is the
    same failure this whole mechanism exists to prevent, one level up.
    """
    import re

    sql = (ROOT / "_postgres" / "migrations" / "add_column_comments.sql").read_text(
        encoding="utf-8"
    )
    in_migration = {
        m.group(1): m.group(2).replace("''", "'")
        for m in re.finditer(
            r"COMMENT ON COLUMN\s+(\S+)\s+IS\s+'((?:[^']|'')*)'", sql
        )
    }
    in_database = {
        f"{r['table_name']}.{r['column_name']}": r["comment"] for r in _rows()
    }
    drifted = sorted(
        k for k in set(in_migration) | set(in_database)
        if in_migration.get(k) != in_database.get(k)
    )
    assert not drifted, (
        "These column comments differ between the migration and the database. "
        "The migration is what a fresh install applies, so it has to match:\n  "
        + "\n  ".join(
            f"{k}\n      migration: {in_migration.get(k)!r}\n"
            f"      database:  {in_database.get(k)!r}"
            for k in drifted[:12]
        )
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
