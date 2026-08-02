"""Deep-research API: boards, threads, and their background jobs.

All LLM work happens in a child process (see backend/services/research/jobs.py);
these routes only write rows, spawn/cancel jobs, and serve polls.

An optional ``llm`` provider override may accompany any request that spawns a
job. It rides the same stdin ``secrets`` channel as the board key — never the
persisted ``params`` column — so the user's API key exists in the request, the
spawn pipe and the child's memory, nowhere else. Because a job outlives its
request, that key stays in the detached child until the job finishes (capped by
MAX_JOB_RUNTIME_SECS, one hour). The UI says so before the user supplies one.

Encrypted boards (logged-in users): the client sends the raw board key with
every request that spawns or writes content. The key is used transiently to
encrypt inserts and is forwarded to the job child via spawn_job(secrets=…) —
it is never written to any table. Poll routes serve ciphertext as stored; the
client decrypts.
"""
from __future__ import annotations

import base64
import os
import time
from typing import List, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, Field

# Importing handlers populates the job registry (register() side effects).
import backend.services.research.handlers  # noqa: F401
from backend.services.auth import get_optional_user
from backend.services.llm_override import ProviderOverride, resolve as resolve_provider
from backend.services.research import board as board_mod
from backend.services.research import jobs

router = APIRouter(prefix="/api", tags=["research"])

# Global cap on jobs running on the server's own key — a token-budget guard.
RESEARCH_MAX_RUNNING_JOBS = int(os.getenv("RESEARCH_MAX_RUNNING_JOBS", "1"))
# Jobs on a user's own key don't touch that budget, so they get their own
# per-owner cap instead of queueing behind everyone else.
RESEARCH_MAX_BYO_JOBS_PER_OWNER = int(os.getenv("RESEARCH_MAX_BYO_JOBS_PER_OWNER", "2"))


def _require_owner(board_id: str, session: Optional[str],
                   user: Optional[dict] = None) -> dict:
    """404 unless the board exists AND belongs to the caller. Returns the
    board's {"owner_session", "user_id"} so callers can budget jobs per owner.

    Account-owned boards (user_id set) require a matching Bearer token.
    Anonymous boards belong to the browser's X-Session-Id (a localStorage
    UUID). Returning 404 (not 403) means a board's existence isn't leaked.
    A missing session id / token never matches."""
    access = board_mod.board_access(board_id)
    if access is None:
        raise HTTPException(status_code=404, detail="Research not found")
    if access.get("user_id"):
        if user is None or user["user_id"] != access["user_id"]:
            raise HTTPException(status_code=404, detail="Research not found")
        return access
    owner = access.get("owner_session") or ""
    if not session or owner != session:
        raise HTTPException(status_code=404, detail="Research not found")
    return access


def _board_key_bytes(board_id: str, board_key: Optional[str]) -> Optional[bytes]:
    """Raw board key for an encrypted board; None for plaintext boards.
    400s when the key is missing/garbled — content writes must never fall back
    to plaintext on an encrypted board."""
    board = board_mod.get_board(board_id)
    if board is None or not board.get("enc"):
        return None
    if not board_key:
        raise HTTPException(status_code=400, detail="Krypterad research kräver boardnyckel")
    try:
        key = base64.b64decode(board_key)
    except Exception:
        key = b""
    if len(key) != 32:
        raise HTTPException(status_code=400, detail="Ogiltig boardnyckel")
    return key

# Opportunistic reaper: at most once a minute, piggybacked on GET polls —
# the poll path is hot exactly when jobs are running.
_last_reap = 0.0


def _maybe_reap() -> None:
    global _last_reap
    now = time.time()
    if now - _last_reap < 60:
        return
    _last_reap = now
    try:
        jobs.reap_stale_jobs()
    except Exception:
        pass


def _check_llm(llm: Optional[ProviderOverride]) -> None:
    """400 on an unknown provider, at request time.

    A bad override must not cost the user a job that only fails half a minute
    later in the child — by then the board has already flipped to 'failed'.
    """
    if llm is None:
        return
    try:
        resolve_provider(llm)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


def _spawn_secrets(board_key: Optional[str], llm: Optional[ProviderOverride],
                   **extra) -> Optional[dict]:
    """Job inputs that must never reach the persisted ``jobs.params`` column.

    ``board_key`` is popped by execute_spec; everything else is merged into the
    child's params in memory. Note this returns a dict for plaintext boards too
    whenever an override is present — the override travels independently of
    whether the board itself is encrypted.
    """
    secrets: dict = {}
    if board_key:
        secrets["board_key"] = board_key
    if llm is not None:
        secrets["llm"] = llm.model_dump()
    secrets.update({k: v for k, v in extra.items() if v is not None})
    return secrets or None


def _byo_params(llm: Optional[ProviderOverride]) -> dict:
    """The persisted half of an override: a flag, never the key or the models.
    Lets the job cap tell whose tokens a running job is spending."""
    return {"byo": True} if llm is not None else {}


def _has_job_slot(llm: Optional[ProviderOverride],
                  access: Optional[dict] = None) -> bool:
    """Is there room to start a job right now?

    Jobs on a user's own key spend no server tokens, so they are budgeted per
    owner instead of against the single global slot everyone else shares.
    """
    if llm is not None:
        access = access or {}
        running = jobs.count_running_byo_jobs(
            access.get("owner_session"), access.get("user_id")
        )
        return running < RESEARCH_MAX_BYO_JOBS_PER_OWNER
    return jobs.count_running_jobs(server_key_only=True) < RESEARCH_MAX_RUNNING_JOBS


def _guard_spawn(board_id: Optional[str] = None,
                 llm: Optional[ProviderOverride] = None,
                 access: Optional[dict] = None) -> None:
    if board_id and jobs.running_job_for_board(board_id):
        raise HTTPException(status_code=409, detail="Ett jobb kör redan för denna research")
    if _has_job_slot(llm, access):
        return
    raise HTTPException(
        status_code=409,
        detail=("Du har redan max antal research-jobb igång — vänta tills ett blir klart"
                if llm is not None else
                "Max antal samtidiga research-jobb kör redan — försök igen om en stund"),
    )


class CreateBoardRequest(BaseModel):
    topic: str = Field(..., min_length=3, max_length=2000)
    title: Optional[str] = None
    # Logged-in users: raw per-board key (base64, 32 bytes) + the same key
    # wrapped by the user's DEK. The raw key is used transiently and forwarded
    # to the job via stdin; only the wrapped copy is stored.
    board_key: Optional[str] = None
    wrapped_board_key: Optional[str] = None
    # Optional user-supplied provider. Rides the secrets channel; see _spawn_secrets.
    llm: Optional[ProviderOverride] = None


class SeedThreadRequest(BaseModel):
    text: str = Field(..., min_length=3, max_length=2000)
    board_key: Optional[str] = None
    llm: Optional[ProviderOverride] = None


class DeepenRequest(BaseModel):
    thread_id: Optional[str] = None
    lead: Optional[dict] = None
    sweep: bool = False
    board_key: Optional[str] = None
    llm: Optional[ProviderOverride] = None


class ThreadSelection(BaseModel):
    thread_id: str
    guidance: Optional[str] = Field(default=None, max_length=2000)


class ActivateThreadsRequest(BaseModel):
    selections: List[ThreadSelection] = Field(..., min_length=1, max_length=20)
    dig: bool = True
    board_key: Optional[str] = None
    llm: Optional[ProviderOverride] = None


class ReportRequest(BaseModel):
    board_key: Optional[str] = None
    llm: Optional[ProviderOverride] = None


@router.post("/research")
def create_research(
    payload: CreateBoardRequest,
    x_session_id: Optional[str] = Header(default=None),
    user: Optional[dict] = Depends(get_optional_user),
) -> dict:
    """Create a board and start the scout job: a few grounding search rounds,
    then thread proposals. Digging waits for the user's selection
    (POST /research/{id}/threads/activate)."""
    if not x_session_id and user is None:
        raise HTTPException(status_code=400, detail="Saknar sessions-id")
    _check_llm(payload.llm)

    key: Optional[bytes] = None
    user_id: Optional[str] = None
    owner_session: Optional[str] = x_session_id
    if user is not None and payload.board_key:
        if not payload.wrapped_board_key:
            raise HTTPException(status_code=400, detail="Saknar wrapped_board_key")
        try:
            key = base64.b64decode(payload.board_key)
        except Exception:
            key = b""
        if len(key) != 32:
            raise HTTPException(status_code=400, detail="Ogiltig boardnyckel")
        user_id = user["user_id"]
        owner_session = None  # account-owned, not browser-owned
    _guard_spawn(llm=payload.llm,
                 access={"owner_session": owner_session, "user_id": user_id})

    board = board_mod.create_board(
        payload.topic, payload.title,
        owner_session=owner_session, user_id=user_id,
        wrapped_board_key=payload.wrapped_board_key if key else None, key=key,
    )
    board_mod.set_board_status(board["id"], "scouting")
    spawned = jobs.spawn_job(
        kind="research_scout", board_id=board["id"],
        params={"board_id": board["id"], **_byo_params(payload.llm)},
        secrets=_spawn_secrets(payload.board_key if key else None, payload.llm),
    )
    if spawned.get("status") == "failed":
        board_mod.set_board_status(board["id"], "failed")
        raise HTTPException(status_code=500, detail="Kunde inte starta research-jobbet")
    return {"board_id": board["id"], "job_id": spawned["job_id"]}


@router.get("/research")
def list_research(
    x_session_id: Optional[str] = Header(default=None),
    user: Optional[dict] = Depends(get_optional_user),
) -> List[dict]:
    _maybe_reap()
    return board_mod.list_boards(
        owner_session=x_session_id,
        user_id=user["user_id"] if user else None,
    )


@router.get("/research/{board_id}")
def get_research(
    board_id: UUID,
    x_session_id: Optional[str] = Header(default=None),
    user: Optional[dict] = Depends(get_optional_user),
) -> dict:
    """The poll target: board + threads + the running job (if any).
    Encrypted boards are served as stored (ciphertext); the client decrypts."""
    _maybe_reap()
    _require_owner(str(board_id), x_session_id, user)
    board = board_mod.get_board(str(board_id))
    if board is None:
        raise HTTPException(status_code=404, detail="Research not found")
    board["threads"] = board_mod.get_threads(str(board_id))
    board["job"] = jobs.running_job_for_board(str(board_id))
    return board


@router.get("/research/{board_id}/events")
def get_research_events(
    board_id: UUID,
    job_id: str,
    offset: int = 0,
    x_session_id: Optional[str] = Header(default=None),
    user: Optional[dict] = Depends(get_optional_user),
) -> dict:
    """Incremental activity ticker for a running job."""
    _require_owner(str(board_id), x_session_id, user)
    return jobs.get_events(job_id, offset)


@router.post("/research/{board_id}/threads")
def seed_thread(
    board_id: UUID,
    payload: SeedThreadRequest,
    x_session_id: Optional[str] = Header(default=None),
    user: Optional[dict] = Depends(get_optional_user),
) -> dict:
    """Add a user-seeded thread. Instant INSERT; if no job is running, spawn a
    deepen job so the thread gets researched right away. If a dig is already
    running it picks the new depth-0 thread up next (shallowest-first)."""
    access = _require_owner(str(board_id), x_session_id, user)
    _check_llm(payload.llm)
    key = _board_key_bytes(str(board_id), payload.board_key)
    thread = board_mod.insert_seed_thread(str(board_id), payload.text, key=key)
    job_id = None
    if not jobs.running_job_for_board(str(board_id)) and _has_job_slot(payload.llm, access):
        board_mod.set_board_status(str(board_id), "digging")
        spawned = jobs.spawn_job(
            kind="research_deepen", board_id=str(board_id),
            params={"board_id": str(board_id), "thread_id": thread["id"],
                    **_byo_params(payload.llm)},
            secrets=_spawn_secrets(payload.board_key if key else None, payload.llm),
        )
        job_id = spawned.get("job_id")
    return {"thread": thread, "job_id": job_id}


@router.post("/research/{board_id}/deepen")
def deepen_research(
    board_id: UUID,
    payload: DeepenRequest,
    x_session_id: Optional[str] = Header(default=None),
    user: Optional[dict] = Depends(get_optional_user),
) -> dict:
    """Spawn a deepen job: one targeted trip (thread/lead) and/or a sweep."""
    access = _require_owner(str(board_id), x_session_id, user)
    _check_llm(payload.llm)
    _guard_spawn(str(board_id), payload.llm, access)
    key = _board_key_bytes(str(board_id), payload.board_key)
    params = {
        "board_id": str(board_id),
        "thread_id": payload.thread_id,
        "sweep": payload.sweep or not (payload.thread_id or payload.lead),
        **_byo_params(payload.llm),
    }
    if key is not None:
        # The lead carries content (its text may quote findings) — for
        # encrypted boards it must ride the secrets channel, never the
        # persisted params.
        secrets = _spawn_secrets(payload.board_key, payload.llm, lead=payload.lead)
    else:
        params["lead"] = payload.lead
        secrets = _spawn_secrets(None, payload.llm)
    board_mod.set_board_status(str(board_id), "digging")
    spawned = jobs.spawn_job(
        kind="research_deepen", board_id=str(board_id),
        params=params, secrets=secrets,
    )
    if spawned.get("status") == "failed":
        board_mod.set_board_status(str(board_id), "failed")
        raise HTTPException(status_code=500, detail="Kunde inte starta jobbet")
    return {"job_id": spawned["job_id"]}


@router.post("/research/{board_id}/threads/activate")
def activate_threads(
    board_id: UUID,
    payload: ActivateThreadsRequest,
    x_session_id: Optional[str] = Header(default=None),
    user: Optional[dict] = Depends(get_optional_user),
) -> dict:
    """Approve proposed threads (optionally with per-thread guidance) and, by
    default, dig them: a sweep picks up the newly activated depth-0 threads
    shallowest-first."""
    access = _require_owner(str(board_id), x_session_id, user)
    _check_llm(payload.llm)
    key = _board_key_bytes(str(board_id), payload.board_key)
    # Reserve the job slot before any write, so a 409 leaves the proposals
    # untouched (the user can retry without losing their selection).
    if payload.dig:
        _guard_spawn(str(board_id), payload.llm, access)

    activated: List[str] = []
    for sel in payload.selections:
        guidance = " ".join((sel.guidance or "").split()).strip() or None
        if board_mod.activate_thread(sel.thread_id, str(board_id), guidance=guidance, key=key):
            activated.append(sel.thread_id)
    if not activated:
        raise HTTPException(status_code=400, detail="Inga förslag att gräva i")
    board_mod.bump_revision(str(board_id))

    job_id = None
    if payload.dig:
        board_mod.set_board_status(str(board_id), "digging")
        spawned = jobs.spawn_job(
            kind="research_deepen", board_id=str(board_id),
            params={"board_id": str(board_id), "sweep": True, **_byo_params(payload.llm)},
            secrets=_spawn_secrets(payload.board_key if key else None, payload.llm),
        )
        if spawned.get("status") == "failed":
            board_mod.set_board_status(str(board_id), "awaiting")
            raise HTTPException(status_code=500, detail="Kunde inte starta jobbet")
        job_id = spawned["job_id"]
    return {"activated": activated, "job_id": job_id}


@router.post("/research/{board_id}/threads/{thread_id}/archive")
def archive_thread(
    board_id: UUID,
    thread_id: UUID,
    x_session_id: Optional[str] = Header(default=None),
    user: Optional[dict] = Depends(get_optional_user),
) -> dict:
    """Dismiss a thread (typically an unwanted proposal). Status-only write —
    no board key needed; the row is kept, just hidden."""
    _require_owner(str(board_id), x_session_id, user)
    return {"archived": board_mod.archive_thread(str(thread_id), str(board_id))}


@router.post("/research/{board_id}/report")
def generate_report(
    board_id: UUID,
    payload: ReportRequest,
    x_session_id: Optional[str] = Header(default=None),
    user: Optional[dict] = Depends(get_optional_user),
) -> dict:
    """Write (or rewrite) the single board report from the thread answers."""
    access = _require_owner(str(board_id), x_session_id, user)
    _check_llm(payload.llm)
    _guard_spawn(str(board_id), payload.llm, access)
    key = _board_key_bytes(str(board_id), payload.board_key)
    board_mod.set_board_status(str(board_id), "reporting")
    spawned = jobs.spawn_job(
        kind="research_report", board_id=str(board_id),
        params={"board_id": str(board_id), **_byo_params(payload.llm)},
        secrets=_spawn_secrets(payload.board_key if key else None, payload.llm),
    )
    if spawned.get("status") == "failed":
        # The board already holds researched content — never leave it 'reporting'.
        board_mod.set_board_status(str(board_id), "ready")
        raise HTTPException(status_code=500, detail="Kunde inte starta rapportjobbet")
    return {"job_id": spawned["job_id"]}


@router.post("/research/{board_id}/cancel")
def cancel_research(
    board_id: UUID,
    x_session_id: Optional[str] = Header(default=None),
    user: Optional[dict] = Depends(get_optional_user),
) -> dict:
    _require_owner(str(board_id), x_session_id, user)
    job = jobs.running_job_for_board(str(board_id))
    if job is None:
        return {"cancelled": False}
    jobs.request_cancel(job["job_id"])
    return {"cancelled": True, "job_id": job["job_id"]}


@router.delete("/research/{board_id}", status_code=204)
def delete_research(
    board_id: UUID,
    x_session_id: Optional[str] = Header(default=None),
    user: Optional[dict] = Depends(get_optional_user),
) -> None:
    _require_owner(str(board_id), x_session_id, user)
    job = jobs.running_job_for_board(str(board_id))
    if job is not None:
        jobs.request_cancel(job["job_id"])
    if not board_mod.delete_board(str(board_id)):
        raise HTTPException(status_code=404, detail="Research not found")
