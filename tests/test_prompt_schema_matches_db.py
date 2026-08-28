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


def test_warns_about_columns_whose_case_is_not_normalised():
    """A column holding both `C` and `c` silently undercounts any equality filter.

    Found via a smoke run: `document_authors.party = 'C'` returns 33,776 rows where
    a case-insensitive compare returns 46,652 — a 28 % undercount, with no error to
    notice. The prompt has to say so; the other party columns are clean.
    """
    from prompts_loader import load_prompt

    schema = load_prompt("_shared/schema")
    assert "document_authors.party" in schema
    assert "upper(" in schema, "schema block must show a case-insensitive comparison"


def test_case_sensitivity_claim_still_holds():
    """If ingest ever normalises the data, this warning becomes misleading."""
    try:
        from postgres_client import pg

        pg.execute("SELECT 1")
    except Exception as exc:
        pytest.skip(f"database unreachable: {exc}")

    mixed = pg.execute(
        "SELECT COUNT(*) AS c FROM document_authors WHERE party <> upper(party)"
    )[0]["c"]
    assert mixed > 0, (
        "document_authors.party is now normalised — drop the case warning from "
        "prompts/en/_shared/schema.md, it no longer applies."
    )
    for table, column in (("speeches", "party"), ("people", "party")):
        clean = pg.execute(
            f"SELECT COUNT(*) AS c FROM {table} WHERE {column} <> upper({column})"
        )[0]["c"]
        assert clean == 0, f"{table}.{column} is now mixed-case too — the prompt says it is clean"
