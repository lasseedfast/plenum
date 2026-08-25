"""Name lookup for members of parliament.

Serves both the `@mention` autocomplete and the people cards shown above search
results, which want the same rows — a name alone is not enough to tell two
members apart, so every match carries party, constituency, photo and how much
the person has spoken.
"""
import re
from typing import Any, Dict, List

from fastapi import APIRouter
from psycopg2 import errors as pg_errors

from postgres_client import pg

router = APIRouter(prefix="/api")

# Two characters is enough to be worth a round trip ("Bo", "Ek"), and the
# trigram index keeps even a broad match cheap.
MIN_QUERY_CHARS = 2
MAX_LIMIT = 25

# Imported records with no source id land under the literal string "None".
# Nothing can link to them, so keep them out of results.
_MISSING_ID = "None"

_SELECT = """
    SELECT p.person_id,
           p.name,
           p.party,
           p.constituency,
           p.status,
           p.active,
           COALESCE(p.image_url_medium, p.image_url_small) AS image_url,
           {stats_select}
    FROM people p
    {stats_join}
    WHERE p.person_id <> %(missing)s
      AND p.name IS NOT NULL
      AND lower(p.name) LIKE %(contains)s
    ORDER BY
        CASE
            WHEN lower(p.name) LIKE %(prefix)s THEN 0   -- "Stefan L" → Stefan Löfven
            WHEN lower(p.name) ~ %(word)s      THEN 1   -- "Löfven"   → Stefan Löfven
            ELSE 2                                      -- anywhere else in the name
        END,
        {stats_order}
        p.name
    LIMIT %(limit)s
"""

_WITH_STATS = {
    "stats_select": "s.speech_count, s.last_speech::text AS last_speech",
    "stats_join": "LEFT JOIN person_speech_stats s ON s.person_id = p.person_id",
    # Recency by year, then volume. Coarse on purpose: to the day, whoever
    # happened to speak most recently wins, so a backbencher outranks a former
    # prime minister with five times the speeches over a gap of a few days.
    # Grouping by year separates those who are still active from those who are
    # not, and lets prominence decide within each year.
    #
    # `active` is not usable in place of this: a party leader serving as a
    # minister rather than as a sitting member is flagged inactive yet speaks
    # constantly.
    "stats_order": (
        "EXTRACT(YEAR FROM s.last_speech) DESC NULLS LAST, "
        "s.speech_count DESC NULLS LAST,"
    ),
}

_WITHOUT_STATS = {
    "stats_select": "NULL::int AS speech_count, NULL::text AS last_speech",
    "stats_join": "",
    "stats_order": "p.active DESC NULLS LAST,",
}


def _rows(query: str, params: Dict[str, Any]) -> List[Dict[str, Any]]:
    return [dict(row) for row in pg.execute(query, params)]


def search_people(q: str, limit: int = 8) -> List[Dict[str, Any]]:
    """Rank members whose name contains `q`, best match first.

    Matching anywhere in the name is the point: anchoring at the first character
    made every surname unfindable, which is how most people look for a member
    whose full name they do not remember.
    """
    query = (q or "").strip().lower()
    if len(query) < MIN_QUERY_CHARS:
        return []
    limit = max(1, min(limit, MAX_LIMIT))

    params = {
        "missing": _MISSING_ID,
        "contains": f"%{query}%",
        "prefix": f"{query}%",
        # Prefix of any word in the name, so "Löfven" and "Busch" both land.
        "word": r"(^|\s)" + re.escape(query),
        "limit": limit,
    }

    try:
        return _rows(_SELECT.format(**_WITH_STATS), params)
    except pg_errors.UndefinedTable:
        # person_speech_stats comes from schema.sql and the add_person_search
        # migration. A deployment that has pulled the code but run neither should
        # still get working suggestions, just ordered less well.
        return _rows(_SELECT.format(**_WITHOUT_STATS), params)


@router.get("/suggest")
def suggest(q: str, limit: int = 8) -> List[Dict[str, Any]]:
    """Name suggestions for the @mention autocomplete and the people cards.

    Args:
        q: Partial name — a first name, a surname, or a fragment of either.
        limit: Maximum matches to return (capped at MAX_LIMIT).

    Returns:
        Ranked matches. `_key` repeats `person_id` under the name the mention
        inputs have always read it by.
    """
    people = search_people(q, limit)
    return [{**person, "_key": person["person_id"]} for person in people]
