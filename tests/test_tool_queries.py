"""Tools with hardcoded SQL, run against the real schema.

fetch_debate spent a month returning nothing but a Postgres error: its query
said `SELECT debate ... WHERE debate = %s` while the column had become `id`.
Nothing caught it, because the failure surfaced as an error string handed to the
model, which simply routed around the tool. The eval even scored it 0 % bad —
it was never producing an answer to be wrong about.

These tests call the tools for real. They are skipped without a database, so the
rest of the suite still runs offline.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


@pytest.fixture(scope="module")
def db():
    try:
        from postgres_client import pg

        pg.execute("SELECT 1")
    except Exception as exc:
        pytest.skip(f"database unreachable: {exc}")
    return pg


@pytest.fixture(scope="module")
def a_debate_id(db) -> str:
    rows = db.execute(
        "SELECT id FROM debates WHERE num_talks > 1 AND talk_ids IS NOT NULL LIMIT 1"
    )
    if not rows:
        pytest.skip("no debates in this database")
    return rows[0]["id"]


def test_fetch_debate_returns_a_debate(db, a_debate_id):
    from backend.services.llm_tools import fetch_debate

    result = fetch_debate(a_debate_id)
    assert "error" not in result, f"fetch_debate failed: {result.get('error')}"
    assert result["debate_id"] == a_debate_id
    assert result.get("date")
    assert result.get("speeches"), "a debate with talk_ids returned no speeches"


def test_fetch_debate_reports_a_missing_debate_cleanly(db):
    """A bad id should be an ordinary 'not found', not a database error."""
    from backend.services.llm_tools import fetch_debate

    result = fetch_debate("1900-01-01:999")
    assert "error" in result
    assert "does not exist" not in result["error"], (
        "a missing debate is leaking a Postgres schema error to the model"
    )


def test_fetch_speeches_returns_rows(db):
    from backend.services.llm_tools import fetch_speeches

    row = db.execute("SELECT id FROM speeches LIMIT 1")[0]
    out = fetch_speeches([row["id"]])
    assert out and isinstance(out, list)


def test_fetch_document_returns_a_motion(db):
    from backend.services.llm_tools import fetch_document

    row = db.execute("SELECT doc_id FROM documents WHERE has_text LIMIT 1")[0]
    result = fetch_document(row["doc_id"])
    assert "error" not in result, f"fetch_document failed: {result.get('error')}"
