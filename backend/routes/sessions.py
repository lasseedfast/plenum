from __future__ import annotations

import json
from typing import Literal, List, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from postgres_client import pg
from backend.services.auth import get_current_user, get_optional_user

router = APIRouter(prefix="/api", tags=["sessions"])


class SessionUpsertRequest(BaseModel):
    session_type: Literal["general", "mp"]
    person_id: Optional[str] = None
    initial_speech_id: Optional[str] = None
    llm_messages: List[dict] = []
    turns: List[dict] = []
    focus_ids: List[str] = []
    # Owned sessions: all content (messages/turns/focus_ids AND the MP identity)
    # arrives as one client-encrypted blob; plaintext fields above stay empty.
    enc_payload: Optional[str] = None
    enc_title: Optional[str] = None


class SessionResponse(BaseModel):
    id: str
    session_type: str
    person_id: Optional[str]
    initial_speech_id: Optional[str]
    llm_messages: List[dict]
    turns: List[dict]
    focus_ids: List[str]
    enc_payload: Optional[str] = None


def _session_owner(session_id: str) -> Optional[str]:
    """user_id of an existing session ('' when unowned), None when missing."""
    rows = pg.execute(
        "SELECT user_id::text AS user_id FROM chat_sessions WHERE id = %s",
        (session_id,),
    )
    if not rows:
        return None
    return rows[0]["user_id"] or ""


@router.get("/sessions/{session_id}", response_model=SessionResponse)
def get_session(
    session_id: UUID,
    user: Optional[dict] = Depends(get_optional_user),
) -> SessionResponse:
    rows = pg.execute(
        """
        SELECT id::text, session_type, person_id, initial_speech_id,
               llm_messages, turns, focus_ids, user_id::text AS user_id, enc_payload
        FROM chat_sessions
        WHERE id = %s
          AND (user_id IS NOT NULL OR last_activity > NOW() - INTERVAL '7 days')
        """,
        (str(session_id),),
    )
    if not rows:
        raise HTTPException(status_code=404, detail="Session not found or expired")

    row = rows[0]
    # Owned sessions are only served to their owner. 404 (not 403) so the
    # existence of someone else's session never leaks.
    if row.get("user_id") and (user is None or user["user_id"] != row["user_id"]):
        raise HTTPException(status_code=404, detail="Session not found or expired")

    return SessionResponse(
        id=row["id"],
        session_type=row["session_type"],
        person_id=row.get("person_id"),
        initial_speech_id=row.get("initial_speech_id"),
        llm_messages=row["llm_messages"] or [],
        turns=row["turns"] or [],
        focus_ids=list(row["focus_ids"] or []),
        enc_payload=row.get("enc_payload"),
    )


@router.put("/sessions/{session_id}", status_code=204, response_model=None)
def upsert_session(
    session_id: UUID,
    payload: SessionUpsertRequest,
    user: Optional[dict] = Depends(get_optional_user),
) -> None:
    if payload.enc_payload is not None and user is None:
        raise HTTPException(status_code=401, detail="Inloggning krävs för krypterade sessioner")

    owner = _session_owner(str(session_id))
    if owner and (user is None or user["user_id"] != owner):
        # Never let an anonymous/mismatched PUT overwrite an owned session.
        raise HTTPException(status_code=404, detail="Session not found or expired")

    if user is not None and payload.enc_payload is not None:
        pg.execute_void(
            """
            INSERT INTO chat_sessions
                (id, session_type, user_id, enc_payload, enc_title, last_activity)
            VALUES (%s, %s, %s, %s, %s, NOW())
            ON CONFLICT (id) DO UPDATE SET
                user_id       = EXCLUDED.user_id,
                enc_payload   = EXCLUDED.enc_payload,
                enc_title     = EXCLUDED.enc_title,
                -- Claiming a previously anonymous session: scrub the plaintext.
                llm_messages  = '[]'::jsonb,
                turns         = '[]'::jsonb,
                focus_ids     = '{}',
                person_id = NULL,
                initial_speech_id = NULL,
                last_activity = NOW()
            """,
            (
                str(session_id),
                payload.session_type,
                user["user_id"],
                payload.enc_payload,
                payload.enc_title,
            ),
        )
    else:
        pg.execute_void(
            """
            INSERT INTO chat_sessions
                (id, session_type, person_id, initial_speech_id,
                 llm_messages, turns, focus_ids, last_activity)
            VALUES (%s, %s, %s, %s, %s::jsonb, %s::jsonb, %s, NOW())
            ON CONFLICT (id) DO UPDATE SET
                llm_messages  = EXCLUDED.llm_messages,
                turns         = EXCLUDED.turns,
                focus_ids     = EXCLUDED.focus_ids,
                last_activity = NOW()
            """,
            (
                str(session_id),
                payload.session_type,
                payload.person_id,
                payload.initial_speech_id,
                json.dumps(payload.llm_messages),
                json.dumps(payload.turns),
                payload.focus_ids,
            ),
        )

    # Opportunistically clean up expired ANONYMOUS sessions (owned ones persist)
    try:
        pg.execute_void(
            """
            DELETE FROM chat_sessions
            WHERE user_id IS NULL AND last_activity < NOW() - INTERVAL '7 days'
            """,
        )
    except Exception:
        pass


# ── My chats (owned sessions; titles are ciphertext, decrypted client-side) ──

class MyChatRow(BaseModel):
    id: str
    session_type: str
    enc_title: Optional[str]
    created_at: str
    last_activity: str


@router.get("/me/chats", response_model=List[MyChatRow])
def list_my_chats(user: dict = Depends(get_current_user)) -> List[MyChatRow]:
    rows = pg.execute(
        """
        SELECT id::text, session_type, enc_title,
               created_at::text AS created_at, last_activity::text AS last_activity
        FROM chat_sessions
        WHERE user_id = %s
        ORDER BY last_activity DESC
        LIMIT 200
        """,
        (user["user_id"],),
    )
    return [MyChatRow(**row) for row in rows]


@router.delete("/me/chats/{session_id}", status_code=204, response_model=None)
def delete_my_chat(session_id: UUID, user: dict = Depends(get_current_user)) -> None:
    rows = pg.execute(
        "DELETE FROM chat_sessions WHERE id = %s AND user_id = %s RETURNING id",
        (str(session_id), user["user_id"]),
    )
    if not rows:
        raise HTTPException(status_code=404, detail="Chat not found")


# ── Snapshots (frozen, shareable read-only views — plaintext by choice) ─────

class SnapshotCreateRequest(BaseModel):
    session_type: Literal["general", "mp"]
    person_id: Optional[str] = None
    initial_speech_id: Optional[str] = None
    llm_messages: List[dict] = []
    turns: List[dict] = []
    focus_ids: List[str] = []


class SnapshotCreateResponse(BaseModel):
    id: str


class SnapshotResponse(BaseModel):
    id: str
    session_type: str
    person_id: Optional[str]
    initial_speech_id: Optional[str] = None
    turns: List[dict]
    llm_messages: List[dict] = []
    focus_ids: List[str] = []
    created_at: str


@router.post("/snapshots", response_model=SnapshotCreateResponse, status_code=201)
def create_snapshot(payload: SnapshotCreateRequest) -> SnapshotCreateResponse:
    import uuid as uuid_lib
    snapshot_id = str(uuid_lib.uuid4())
    pg.execute_void(
        """
        INSERT INTO chat_snapshots
            (id, session_type, person_id, initial_speech_id, llm_messages, turns, focus_ids, last_activity)
        VALUES (%s, %s, %s, %s, %s::jsonb, %s::jsonb, %s, NOW())
        """,
        (
            snapshot_id,
            payload.session_type,
            payload.person_id,
            payload.initial_speech_id,
            json.dumps(payload.llm_messages),
            json.dumps(payload.turns),
            payload.focus_ids,
        ),
    )
    try:
        pg.execute_void(
            "DELETE FROM chat_snapshots WHERE last_activity < NOW() - INTERVAL '7 days'",
        )
    except Exception:
        pass

    return SnapshotCreateResponse(id=snapshot_id)


class ForkSnapshotResponse(BaseModel):
    session_id: str


@router.post("/snapshots/{snapshot_id}/fork", response_model=ForkSnapshotResponse, status_code=201)
def fork_snapshot(snapshot_id: UUID) -> ForkSnapshotResponse:
    """Server-side fork for anonymous visitors. Logged-in clients fork locally
    instead (fetch → encrypt → PUT) so the copy is encrypted from the start."""
    import uuid as uuid_lib
    rows = pg.execute(
        """
        SELECT session_type, person_id, initial_speech_id, llm_messages, turns, focus_ids
        FROM chat_snapshots
        WHERE id = %s
        """,
        (str(snapshot_id),),
    )
    if not rows:
        raise HTTPException(status_code=404, detail="Snapshot not found")

    row = rows[0]
    session_id = str(uuid_lib.uuid4())
    pg.execute_void(
        """
        INSERT INTO chat_sessions
            (id, session_type, person_id, initial_speech_id,
             llm_messages, turns, focus_ids, last_activity)
        VALUES (%s, %s, %s, %s, %s::jsonb, %s::jsonb, %s, NOW())
        """,
        (
            session_id,
            row["session_type"],
            row.get("person_id"),
            row.get("initial_speech_id"),
            json.dumps(row["llm_messages"] or []),
            json.dumps(row["turns"] or []),
            list(row["focus_ids"] or []),
        ),
    )
    try:
        pg.execute_void(
            "UPDATE chat_snapshots SET last_activity = NOW() WHERE id = %s",
            (str(snapshot_id),),
        )
    except Exception:
        pass

    return ForkSnapshotResponse(session_id=session_id)


@router.get("/snapshots/{snapshot_id}", response_model=SnapshotResponse)
def get_snapshot(snapshot_id: UUID) -> SnapshotResponse:
    rows = pg.execute(
        """
        SELECT id::text, session_type, person_id, initial_speech_id,
               turns, llm_messages, focus_ids, created_at::text
        FROM chat_snapshots
        WHERE id = %s
        """,
        (str(snapshot_id),),
    )
    if not rows:
        raise HTTPException(status_code=404, detail="Snapshot not found")

    row = rows[0]
    return SnapshotResponse(
        id=row["id"],
        session_type=row["session_type"],
        person_id=row.get("person_id"),
        initial_speech_id=row.get("initial_speech_id"),
        turns=row["turns"] or [],
        llm_messages=row["llm_messages"] or [],
        focus_ids=list(row["focus_ids"] or []),
        created_at=row["created_at"],
    )
