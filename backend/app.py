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
            "WHERE c.relname = 'speech_chunks' AND a.attname = 'embedding'"
        )
        if rows and rows[0]["typmod"] and rows[0]["typmod"] > 0:
            actual_dim = rows[0]["typmod"]
            if actual_dim != PARLIAMENT.embeddings.dimension:
                raise RuntimeError(
                    f"speech_chunks.embedding is vector({actual_dim}) but parliament.yaml "
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
    """Serve the user guide as plain text — the single source for all guide links.

    Resolved through CONTENT_DIR, so a deployment can ship its own guide without
    editing the repository.
    """
    guide = PARLIAMENT.read_content("guide_file")
    if not guide:
        raise HTTPException(status_code=404, detail="No guide configured")
    return guide


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


@app.get("/api/talk/{speech_id}")
def get_talk(speech_id: str) -> dict:
    """
    Fetch a single talk document by its ID.

    Accepts either:
    - A full id like "speeches/H40911"
    - Just the key like "H40911"

    Returns the talk with person info merged in, plus previous/next navigation.
    """
    bare_id = speech_id.split("/", 1)[-1]  # strip "speeches/" prefix if present

    rows = pg.execute(
        """
        SELECT
            t.id, t.text, t.speaker_name, t.party,
            t.date::text AS date, t.activity_type, t.section_title,
            t.title, t.sequence, t.is_reply,
            COALESCE(t.url_session, t.url_video) AS url_session,
            t.url_audio, t.summary, t.person_id,
            p.image_url_medium, p.first_name, p.last_name, p.constituency, p.status AS person_status
        FROM speeches t
        LEFT JOIN people p ON t.person_id = p.person_id
        WHERE t.id = %s
        """,
        (bare_id,),
    )

    if not rows:
        raise HTTPException(status_code=404, detail=f"Talk not found: {speech_id}")

    row = rows[0]
    num = row.get("sequence")

    # Previous / next navigation within the same date + debate type
    prev_rows, next_rows = [], []
    if num is not None:
        prev_rows = pg.execute(
            """
            SELECT id FROM speeches
            WHERE date = %s::date AND activity_type = %s AND sequence = %s
            LIMIT 1
            """,
            (row["date"], row["activity_type"], num - 1),
        )
        next_rows = pg.execute(
            """
            SELECT id FROM speeches
            WHERE date = %s::date AND activity_type = %s AND sequence = %s
            LIMIT 1
            """,
            (row["date"], row["activity_type"], num + 1),
        )

    person = None
    if row.get("first_name") or row.get("last_name"):
        person = {
            # person_id is what makes the speaker's name a link to their profile.
            # Leaving it out silently degrades the talk page to plain text.
            "person_id": row.get("person_id"),
            "name": row.get("speaker_name"),
            "image_url_medium": row.get("image_url_medium"),
            "first_name": row.get("first_name"),
            "last_name": row.get("last_name"),
            "constituency": row.get("constituency"),
            "status": row.get("person_status"),
        }

    return {
        "text": row.get("text"),
        "speaker_name": row.get("speaker_name"),
        "party": row.get("party"),
        "date": row.get("date"),
        "activity_type": row.get("activity_type"),
        "section_title": row.get("section_title"),
        "title": row.get("title"),
        "sequence": num,
        "is_reply": row.get("is_reply"),
        "url_session": row.get("url_session"),
        "url_audio": row.get("url_audio"),
        "summary": row.get("summary"),
        "person": person,
        "navigation": {
            "previous": f"speeches/{prev_rows[0]['id']}" if prev_rows else None,
            "next": f"speeches/{next_rows[0]['id']}" if next_rows else None,
        },
    }


# Fields surfaced per yrkande from the raw dokforslag JSON.
_MOTION_YRKANDE_KEYS = ("number", "text", "committee_recommendation", "chamber_decision", "handled_in")


@app.get("/api/motion/{doc_id}")
def get_motion(doc_id: str) -> dict:
    """
    Fetch a single motion document by its doc_id.

    Accepts either a full id like "documents/HD02846" or just the key "HD02846".
    Returns a shape parallel to /api/talk (speaker_name/party/date/title/text
    populated for shared rendering) plus motion-specific fields: authors,
    yrkanden with committee/chamber outcomes, pdf/document links.
    """
    bare_id = doc_id.split("/", 1)[-1]  # strip "documents/" prefix if present

    rows = pg.execute(
        """
        SELECT doc_id, session_label, designation, subtype, committee, status,
               date::text AS date, title, subtitle, text, has_text,
               parties, author_names, proposals_raw, url_pdf, url_html
        FROM documents
        WHERE doc_id = %s
        """,
        (bare_id,),
    )
    if not rows:
        raise HTTPException(status_code=404, detail=f"Motion not found: {doc_id}")
    row = rows[0]

    # Authors joined with people for portraits / MP-page links.
    author_rows = pg.execute(
        """
        SELECT a.ordinal, a.name, a.party, a.role, a.person_id,
               p.image_url_medium, p.first_name, p.last_name, p.constituency,
               p.status AS person_status
        FROM document_authors a
        LEFT JOIN people p ON a.person_id = p.person_id
        WHERE a.doc_id = %s
        ORDER BY a.ordinal
        """,
        (bare_id,),
    )
    authors = [
        {
            "name": a.get("name"),
            "party": a.get("party"),
            "role": a.get("role"),
            "person_id": a.get("person_id"),
            "first_name": a.get("first_name"),
            "image_url_medium": (a.get("image_url_medium") or "").replace("http://", "https://") or None,
            "constituency": a.get("constituency"),
            "status": a.get("person_status"),
        }
        for a in author_rows
    ]

    # Primary author becomes the "person" card, mirroring speeches' speaker.
    person = None
    if authors and (authors[0].get("person_id") or authors[0].get("image_url_medium")):
        first = authors[0]
        person = {
            "image_url_medium": first.get("image_url_medium"),
            "first_name": first.get("first_name"),
            "person_id": first.get("person_id"),
            "constituency": first.get("constituency"),
            "status": first.get("status"),
        }

    proposals_raw = row.get("proposals_raw") or []
    if isinstance(proposals_raw, str):
        import json as _json
        proposals_raw = _json.loads(proposals_raw)
    yrkanden = [
        {k: f.get(k) for k in _MOTION_YRKANDE_KEYS if f.get(k) is not None}
        for f in proposals_raw
        if isinstance(f, dict)
    ]

    author_names = row.get("author_names") or []
    speaker_name = ", ".join(author_names[:3]) + (" m.fl." if len(author_names) > 3 else "")

    return {
        "kind": "motion",
        "doc_id": row.get("doc_id"),
        # Shared-shape fields so talk-oriented UI code renders without changes:
        "speaker_name": speaker_name,
        "party": "/".join(row.get("parties") or []),
        "date": row.get("date"),
        "title": row.get("title"),
        "text": row.get("text"),
        "summary": None,
        "person": person,
        # Motion-specific:
        "subtitle": row.get("subtitle"),
        "session_label": row.get("session_label"),
        "designation": row.get("designation"),
        "subtype": row.get("subtype"),
        "committee": row.get("committee"),
        "status": row.get("status"),
        "has_text": row.get("has_text"),
        "parties": row.get("parties") or [],
        "authors": authors,
        "yrkanden": yrkanden,
        "url_pdf": row.get("url_pdf"),
        "url_html": row.get("url_html"),
        "navigation": {"previous": None, "next": None},
    }
