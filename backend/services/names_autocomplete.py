from fastapi import APIRouter
from typing import Dict, List

from postgres_client import pg

router = APIRouter(prefix="/api")


@router.get("/suggest")
def suggest(q: str, limit: int = 8) -> List[Dict[str, str]]:
    """Return name suggestions for the @ mention autocomplete.

    Args:
        q: Partial name entered after an at-sign (already trimmed by the caller).
        limit: Maximum number of matches to return (defaults to 8).

    Returns:
        A list of dicts with ``name`` and ``intressent_id`` fields.
    """
    query = (q or "").strip()
    if len(query) < 3:
        return []

    rows = pg.execute(
        """
        SELECT namn, intressent_id
        FROM people
        WHERE LOWER(namn) LIKE %s
        ORDER BY namn
        LIMIT %s
        """,
        (f"{query.lower()}%", limit),
    )
    return [{"name": row["namn"], "_key": row["intressent_id"]} for row in rows]
