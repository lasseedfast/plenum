"""The schema block in the prompts must name only columns that exist.

The block is the "never invent columns" contract handed to every agent that can
write SQL. It has been wrong before — `debates.debate`, `document_proposals.nummer`
and `behandlas_i` were all in it, and every query touching them failed — and it was
compacted afterwards, which is exactly the kind of edit that quietly drops a column.

Skipped when no database is reachable, so the suite still runs offline.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# Words inside the schema block that are types or markers, not column names.
_NOT_COLUMNS = {"int", "date", "bool", "text", "pk", "tsvector"}


def _claimed_columns() -> dict[str, set[str]]:
    """Parse the rendered schema partial into {table: {column, ...}}."""
    from prompts_loader import load_prompt

    schema = load_prompt("_shared/schema")
    block = schema[schema.index("    speeches "): schema.index("\nJoins:")]
    tables: dict[str, set[str]] = {}
    current: str | None = None
    for line in block.split("\n"):
        match = re.match(r"^ {4}(\w+) +(.*)$", line)
        if match:
            current, rest = match.group(1), match.group(2)
        else:
            rest = line.strip()
            if not rest or current is None:
                continue
        tables.setdefault(current, set()).update(
            token for token in re.findall(r"[a-z_]{2,}", rest)
            if token not in _NOT_COLUMNS
        )
    return tables


def _real_columns(table: str) -> set[str]:
    from postgres_client import pg

    return {
        row["column_name"]
        for row in pg.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_name = %s",
            (table,),
        )
    }


@pytest.fixture(scope="module")
def claimed() -> dict[str, set[str]]:
    tables = _claimed_columns()
    assert tables, "schema partial parsed to nothing — has its layout changed?"
    return tables


def test_schema_block_lists_every_table(claimed):
    assert set(claimed) == {
        "speeches", "people", "debates",
        "documents", "document_authors", "document_proposals",
    }


def test_no_invented_columns(claimed):
    try:
        from postgres_client import pg

        pg.execute("SELECT 1")
    except Exception as exc:  # no database in this environment
        pytest.skip(f"database unreachable: {exc}")

    invented: dict[str, list[str]] = {}
    for table, columns in claimed.items():
        missing = sorted(columns - _real_columns(table))
        if missing:
            invented[table] = missing
    assert not invented, f"prompt names columns that do not exist: {invented}"


def test_party_columns_are_case_normalised():
    """Every party column holds one casing, so a plain `=` filter is safe.

    This replaces a pair of tests that asserted the opposite. `document_authors.party`
    and `documents.parties` used to hold the same party as both `C` and `c`, so
    `party = 'C'` returned 33,776 rows where a case-insensitive compare returned
    46,652 — a 28 % undercount with no error to notice. The prompt carried a warning
    telling the model to fold the case, and a tripwire test here failed the day the
    data was cleaned, so the warning could not outlive the problem. Both are gone;
    `_postgres/migrations/20260828_01_normalise_party_case.sql` did the backfill.

    What is left is the guard in the other direction. Ingest uppercases on the way in,
    but that is one `.upper()` in one adapter — if it is ever dropped, this fails
    before anyone notices a quarter of the rows going missing from a count.
    """
    try:
        from postgres_client import pg

        pg.execute("SELECT 1")
    except Exception as exc:  # no database in this environment
        pytest.skip(f"database unreachable: {exc}")

    for table, column in (
        ("document_authors", "party"),
        ("speeches", "party"),
        ("people", "party"),
    ):
        mixed = pg.execute(
            f"SELECT COUNT(*) AS c FROM {table} WHERE {column} <> upper({column})"
        )[0]["c"]
        assert mixed == 0, (
            f"{table}.{column} has {mixed} mixed-case rows — ingest has stopped "
            "normalising party codes, and equality filters are now undercounting."
        )

    unfolded = pg.execute(
        "SELECT COUNT(*) AS c FROM documents"
        " WHERE EXISTS (SELECT 1 FROM unnest(parties) p WHERE p <> upper(p))"
    )[0]["c"]
    assert unfolded == 0, (
        f"documents.parties has {unfolded} rows with a lowercase entry — grouping on"
        " unnest(parties) will split one party into two rows."
    )
