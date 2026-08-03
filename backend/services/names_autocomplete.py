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
        A list of dicts with ``name`` and ``person_id`` fields.
    """
    query = (q or "").strip()
    if len(query) < 3:
        return []

    rows = pg.execute(
        """
        SELECT name, person_id
        FROM people
        WHERE LOWER(name) LIKE %s
        ORDER BY name
        LIMIT %s
        """,
        (f"{query.lower()}%", limit),
    )
    return [{"name": row["name"], "_key": row["person_id"]} for row in rows]
