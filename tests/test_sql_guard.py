"""Model-authored SQL may read the corpus and nothing else.

This is a regression test for a real hole: `database_query` refused non-SELECT
and ran inside a READ ONLY transaction, but nothing restricted *which tables* it
could read — and READ ONLY blocks writes, not reads. Asking the live site to
"see what users there is in the users table" returned the account list.

The database holds users, auth_tokens and chat sessions alongside the corpus, so
the interesting cases here are the ones that try to reach them by a route other
than a bare `FROM users`.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend.services.llm_tools import _reject_unsafe_sql  # noqa: E402

MUST_REFUSE = [
    pytest.param("SELECT username, auth_hash FROM users", id="the-reported-query"),
    pytest.param('SELECT * FROM "users"', id="quoted"),
    pytest.param("SELECT * FROM public.users", id="schema-qualified"),
    pytest.param("select u.username from USERS u", id="case-and-alias"),
    pytest.param("SELECT * FROM auth_tokens", id="auth-tokens"),
    pytest.param("SELECT * FROM chat_sessions", id="chat-sessions"),
    pytest.param("SELECT * FROM research_boards", id="research-boards"),
    pytest.param("SELECT * FROM information_schema.columns", id="information-schema"),
    pytest.param("SELECT * FROM pg_catalog.pg_tables", id="pg-catalog"),
    pytest.param("SELECT s.id FROM speeches s, users u", id="comma-join"),
    pytest.param("SELECT s.id FROM speeches s JOIN users u ON u.id::text = s.id", id="join"),
    pytest.param("SELECT (SELECT count(*) FROM users) c FROM speeches", id="scalar-subquery"),
    pytest.param("SELECT * FROM speeches WHERE id IN (SELECT username FROM users)", id="in-subquery"),
    pytest.param("WITH x AS (SELECT * FROM users) SELECT * FROM x", id="hidden-in-cte"),
    pytest.param("SELECT * FROM users -- FROM speeches", id="trailing-comment"),
    pytest.param("SELECT * FROM /* c */ users", id="inline-comment"),
    pytest.param("SELECT id FROM speeches UNION SELECT username FROM users", id="union"),
    pytest.param("DELETE FROM speeches", id="not-a-select"),
    pytest.param("SELECT 1; DROP TABLE speeches", id="second-statement"),
]

MUST_ALLOW = [
    pytest.param("SELECT party, COUNT(*) FROM speeches GROUP BY party", id="group-by"),
    pytest.param(
        "SELECT * FROM speeches s JOIN people p ON p.person_id = s.person_id LIMIT 5",
        id="join-corpus",
    ),
    pytest.param(
        "SELECT * FROM documents d, document_authors a WHERE d.doc_id = a.doc_id",
        id="comma-join-corpus",
    ),
    pytest.param(
        "WITH a AS (SELECT id, party FROM speeches"
        " WHERE search_vector @@ websearch_to_tsquery('swedish', 'q1')),"
        " b AS (SELECT id FROM speeches"
        " WHERE search_vector @@ websearch_to_tsquery('swedish', 'q2'))"
        " SELECT a.party, COUNT(*) total, COUNT(b.id) matches"
        " FROM a LEFT JOIN b USING (id) GROUP BY 1",
        id="two-cte-pattern-from-the-prompt",
    ),
    pytest.param(
        "SELECT upper(p) party FROM documents, unnest(parties) AS p GROUP BY 1",
        id="unnest",
    ),
    pytest.param("SELECT * FROM (SELECT id FROM speeches LIMIT 5) x", id="subquery"),
    pytest.param(
        "SELECT count(*) FROM debates d LEFT JOIN speeches s ON s.debate_id = d.id",
        id="debates",
    ),
]


@pytest.mark.parametrize("sql", MUST_REFUSE)
def test_refuses_everything_outside_the_corpus(sql):
    assert _reject_unsafe_sql(sql), f"not refused: {sql}"


@pytest.mark.parametrize("sql", MUST_ALLOW)
def test_allows_ordinary_corpus_queries(sql):
    assert _reject_unsafe_sql(sql) is None, _reject_unsafe_sql(sql)


def test_a_table_name_inside_a_search_term_is_not_a_table_reference():
    """The literal is stripped before the scan, so this must still run."""
    sql = ("SELECT id FROM speeches"
           " WHERE search_vector @@ websearch_to_tsquery('swedish', 'users')")
    assert _reject_unsafe_sql(sql) is None


def test_the_refusal_names_what_the_model_may_read_instead():
    message = _reject_unsafe_sql("SELECT * FROM users")
    assert "users" in message and "speeches" in message
