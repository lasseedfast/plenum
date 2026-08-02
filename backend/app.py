from __future__ import annotations

import asyncio
import os
from datetime import datetime

import httpx
from fastapi import Depends, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import PlainTextResponse
from postgres_client import pg

from parliament import PARLIAMENT
from .schemas import (
    ChatRequest,
    ChatResponse,
    FeedbackRequest,
    FeedbackResponse,
    SearchRequest,
    SearchResponse,
    TalkHit,
)
from .services import ChatService, SearchService
from backend.routes.auth import router as auth_router
from backend.routes.chat import router as chat_router
from backend.routes.research import router as research_router
from backend.routes.sessions import router as sessions_router
from backend.routes.settings import router as settings_router
from .services.names_autocomplete import router as names_autocomplete_router

app = FastAPI(title="Riksdagen API", version="0.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # tighten for production
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

search_service = SearchService()
chat_service = ChatService()

app.include_router(auth_router)
app.include_router(chat_router)
app.include_router(research_router)
app.include_router(sessions_router)
app.include_router(settings_router)
app.include_router(names_autocomplete_router)


@app.on_event("startup")
def _verify_database_matches_config() -> None:
    """Refuse to serve if the database disagrees with parliament.yaml.

    Both mismatches below fail silently rather than loudly, which is why they are
    checked here:

    * A wrong text-search configuration makes every full-text query return almost
      nothing, with no error — the app looks empty rather than broken.
    * A vector column narrower or wider than `embeddings.dimension` fails inside
      pgvector with a message that never mentions configuration.
    """
    from parliament import PARLIAMENT

    try:
        rows = pg.execute("SELECT current_setting('app.fts_config', true) AS cfg")
        configured = PARLIAMENT.language.fts_config
        actual = (rows[0]["cfg"] if rows else None) or None
        if actual and actual != configured:
            raise RuntimeError(
                f"Database app.fts_config is {actual!r} but parliament.yaml declares "
                f"{configured!r}. Fix with: "
                f"ALTER DATABASE <db> SET app.fts_config = '{configured}';"
            )

        rows = pg.execute(
            "SELECT a.atttypmod AS typmod FROM pg_attribute a "
            "JOIN pg_class c ON c.oid = a.attrelid "
            "WHERE c.relname = 'chunks' AND a.attname = 'embedding'"
        )
        if rows and rows[0]["typmod"] and rows[0]["typmod"] > 0:
            actual_dim = rows[0]["typmod"]
            if actual_dim != PARLIAMENT.embeddings.dimension:
                raise RuntimeError(
                    f"chunks.embedding is vector({actual_dim}) but parliament.yaml "
                    f"declares embeddings.dimension={PARLIAMENT.embeddings.dimension}. "
                    f"Re-embedding is required to change this."
                )
    except RuntimeError:
        raise
    except Exception as exc:
        # A database that is merely unreachable is not a configuration error;
        # let the request path report that in its own terms.
        print(f"[startup] could not verify database configuration: {exc}")


@app.on_event("startup")
def _reap_abandoned_jobs() -> None:
    """Finalize research jobs whose child died while the API was down."""
    from backend.services.research.jobs import reap_stale_jobs

    try:
        reaped = reap_stale_jobs()
        if reaped:
            print(f"[startup] reaped {reaped} abandoned research job(s)")
    except Exception as exc:
        print(f"[startup] job reaper failed: {exc}")


@app.get("/api/guide", response_class=PlainTextResponse)
def get_guide() -> str:
    """Serve user-guide.md as plain text. Single source of truth for all guide links."""
    guide_path = os.path.join(os.path.dirname(__file__), "..", "user-guide.md")
    if not os.path.exists(guide_path):
        raise HTTPException(status_code=404, detail="Guide not found")
    with open(guide_path, encoding="utf-8") as f:
        return f.read()


@app.get("/api/meta")
def meta():
    return PARLIAMENT.public_meta()


@app.post("/api/search", response_model=SearchResponse)
def search(payload: SearchRequest):
    results, stats, limit_reached = search_service.search(
        payload, include_snippets=payload.include_snippets
    )

    # Try to convert results to TalkHit objects
    hits = []
    for idx, hit in enumerate(results):
        try:
            talk_hit = TalkHit(**hit)
            # Serialize using alias so 'id' is sent to frontend, not '_id'
            hit_dict = talk_hit.dict(by_alias=True)
            hits.append(hit_dict)
        except Exception as e:
            print(f"Error converting result {idx} to TalkHit: {e}")
            print(f"Problematic result: {hit}")
            # Continue with other results instead of failing completely
            continue

    return {
        "results": hits,
        "stats": stats,
        "active_filters": {
            "parties": payload.parties,
            "people": payload.people,
            "debates": payload.debates,
            "from_year": payload.from_year,
            "to_year": payload.to_year,
            "speaker_ids": payload.speaker_ids,
            "speaker": payload.speaker,
        },
        "limit_reached": limit_reached,
    }


@app.post("/api/chat", response_model=ChatResponse)
def chat(payload: ChatRequest) -> ChatResponse:
    """
    Generate a chat answer plus citations via the retrieval-aware ChatService.

    Args:
        payload (ChatRequest): Chat history, retrieval strategy, and result limit.

    Returns:
        ChatResponse: Assistant reply and supporting sources.
    """
    if not payload.messages:
        raise HTTPException(status_code=400, detail="messages cannot be empty")

    messages = [message.dict() for message in payload.messages]
    limit = getattr(payload, "top_k", None)
    if limit is None:
        limit = getattr(payload, "limit", None)
    top_k = limit or 5

    chat_result = chat_service.get_chat_response(
        messages=messages,
        top_k=top_k,
    )
    return ChatResponse(answer=chat_result["answer"], sources=chat_result["sources"])


@app.get("/api/talk/{talk_id}")
def get_talk(talk_id: str) -> dict:
    """
    Fetch a single talk document by its ID.

    Accepts either:
    - A full id like "talks/H40911"
    - Just the key like "H40911"

    Returns the talk with person info merged in, plus previous/next navigation.
    """
    bare_id = talk_id.split("/", 1)[-1]  # strip "talks/" prefix if present

    rows = pg.execute(
        """
        SELECT
            t.id, t.anforandetext, t.talare, t.parti,
            t.datum::text AS datum, t.kammaraktivitet, t.avsnittsrubrik,
            t.titel, t.anforande_nummer, t.replik,
            COALESCE(t.url_session, t.debateurl) AS url_session,
            t.url_audio, t.summary, t.intressent_id,
            p.bild_url_192, p.tilltalsnamn, p.efternamn, p.valkrets, p.status AS person_status
        FROM talks t
        LEFT JOIN people p ON t.intressent_id = p.intressent_id
        WHERE t.id = %s
        """,
        (bare_id,),
    )

    if not rows:
        raise HTTPException(status_code=404, detail=f"Talk not found: {talk_id}")

    row = rows[0]
    num = row.get("anforande_nummer")

    # Previous / next navigation within the same date + debate type
    prev_rows, next_rows = [], []
    if num is not None:
        prev_rows = pg.execute(
            """
            SELECT id FROM talks
            WHERE datum = %s::date AND kammaraktivitet = %s AND anforande_nummer = %s
            LIMIT 1
            """,
            (row["datum"], row["kammaraktivitet"], num - 1),
        )
        next_rows = pg.execute(
            """
            SELECT id FROM talks
            WHERE datum = %s::date AND kammaraktivitet = %s AND anforande_nummer = %s
            LIMIT 1
            """,
            (row["datum"], row["kammaraktivitet"], num + 1),
        )

    person = None
    if row.get("tilltalsnamn") or row.get("efternamn"):
        person = {
            "bild_url_192": row.get("bild_url_192"),
            "tilltalsnamn": row.get("tilltalsnamn"),
            "efternamn": row.get("efternamn"),
            "valkrets": row.get("valkrets"),
            "status": row.get("person_status"),
        }

    return {
        "anforandetext": row.get("anforandetext"),
        "talare": row.get("talare"),
        "parti": row.get("parti"),
        "datum": row.get("datum"),
        "kammaraktivitet": row.get("kammaraktivitet"),
        "avsnittsrubrik": row.get("avsnittsrubrik"),
        "titel": row.get("titel"),
        "anforande_nummer": num,
        "replik": row.get("replik"),
        "url_session": row.get("url_session"),
        "url_audio": row.get("url_audio"),
        "summary": row.get("summary"),
        "person": person,
        "navigation": {
            "previous": f"talks/{prev_rows[0]['id']}" if prev_rows else None,
            "next": f"talks/{next_rows[0]['id']}" if next_rows else None,
        },
    }


# Fields surfaced per yrkande from the raw dokforslag JSON.
_MOTION_YRKANDE_KEYS = ("nummer", "lydelse", "utskottet", "kammaren", "behandlas_i")


@app.get("/api/motion/{dok_id}")
def get_motion(dok_id: str) -> dict:
    """
    Fetch a single motion document by its dok_id.

    Accepts either a full id like "motions/HD02846" or just the key "HD02846".
    Returns a shape parallel to /api/talk (talare/parti/datum/titel/anforandetext
    populated for shared rendering) plus motion-specific fields: authors,
    yrkanden with committee/chamber outcomes, pdf/document links.
    """
    bare_id = dok_id.split("/", 1)[-1]  # strip "motions/" prefix if present

    rows = pg.execute(
        """
        SELECT dok_id, rm, beteckning, subtyp, organ, status,
               datum::text AS datum, titel, undertitel, text, has_text,
               parties, author_names, forslag, pdf_url, dokument_url_html
        FROM motions
        WHERE dok_id = %s
        """,
        (bare_id,),
    )
    if not rows:
        raise HTTPException(status_code=404, detail=f"Motion not found: {dok_id}")
    row = rows[0]

    # Authors joined with people for portraits / MP-page links.
    author_rows = pg.execute(
        """
        SELECT a.ordinal, a.namn, a.partibet, a.roll, a.intressent_id,
               p.bild_url_192, p.tilltalsnamn, p.efternamn, p.valkrets,
               p.status AS person_status
        FROM motion_authors a
        LEFT JOIN people p ON a.intressent_id = p.intressent_id
        WHERE a.dok_id = %s
        ORDER BY a.ordinal
        """,
        (bare_id,),
    )
    authors = [
        {
            "namn": a.get("namn"),
            "partibet": a.get("partibet"),
            "roll": a.get("roll"),
            "intressent_id": a.get("intressent_id"),
            "tilltalsnamn": a.get("tilltalsnamn"),
            "bild_url_192": (a.get("bild_url_192") or "").replace("http://", "https://") or None,
            "valkrets": a.get("valkrets"),
            "status": a.get("person_status"),
        }
        for a in author_rows
    ]

    # Primary author becomes the "person" card, mirroring talks' speaker.
    person = None
    if authors and (authors[0].get("intressent_id") or authors[0].get("bild_url_192")):
        first = authors[0]
        person = {
            "bild_url_192": first.get("bild_url_192"),
            "tilltalsnamn": first.get("tilltalsnamn"),
            "intressent_id": first.get("intressent_id"),
            "valkrets": first.get("valkrets"),
            "status": first.get("status"),
        }

    forslag = row.get("forslag") or []
    if isinstance(forslag, str):
        import json as _json
        forslag = _json.loads(forslag)
    yrkanden = [
        {k: f.get(k) for k in _MOTION_YRKANDE_KEYS if f.get(k) is not None}
        for f in forslag
        if isinstance(f, dict)
    ]

    author_names = row.get("author_names") or []
    talare = ", ".join(author_names[:3]) + (" m.fl." if len(author_names) > 3 else "")

    return {
        "kind": "motion",
        "dok_id": row.get("dok_id"),
        # Shared-shape fields so talk-oriented UI code renders without changes:
        "talare": talare,
        "parti": "/".join(row.get("parties") or []),
        "datum": row.get("datum"),
        "titel": row.get("titel"),
        "anforandetext": row.get("text"),
        "summary": None,
        "person": person,
        # Motion-specific:
        "undertitel": row.get("undertitel"),
        "rm": row.get("rm"),
        "beteckning": row.get("beteckning"),
        "subtyp": row.get("subtyp"),
        "organ": row.get("organ"),
        "status": row.get("status"),
        "has_text": row.get("has_text"),
        "parties": row.get("parties") or [],
        "authors": authors,
        "yrkanden": yrkanden,
        "pdf_url": row.get("pdf_url"),
        "dokument_url_html": row.get("dokument_url_html"),
        "navigation": {"previous": None, "next": None},
    }
