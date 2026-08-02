"""Per-account settings blob — currently the user's AI provider config.

Zero-knowledge, same contract as chats and research boards: the browser
encrypts under the user's DEK and the server stores an opaque "v1:" blob. That
is what lets an API key follow a user between devices without the server (or
anyone with the database) being able to read it.

Guests have no account to hang a blob on and keep their settings in
localStorage instead; these routes are logged-in only.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from backend.services import crypto_blob
from backend.services.auth import get_current_user
from postgres_client import pg

router = APIRouter(prefix="/api/me", tags=["settings"])


class SettingsRequest(BaseModel):
    enc_settings: str = Field(..., min_length=4, max_length=20000)


@router.get("/settings")
def get_settings(user: dict = Depends(get_current_user)) -> dict:
    rows = pg.execute(
        "SELECT enc_settings, settings_updated_at::text AS updated_at "
        "FROM users WHERE id = %s",
        (user["user_id"],),
    )
    if not rows:
        raise HTTPException(status_code=404, detail="Kontot finns inte längre")
    return {"enc_settings": rows[0]["enc_settings"], "updated_at": rows[0]["updated_at"]}


@router.put("/settings", status_code=204, response_model=None)
def put_settings(payload: SettingsRequest, user: dict = Depends(get_current_user)) -> None:
    # The one server-side invariant worth asserting: this column may only ever
    # hold ciphertext. A client bug that posted a plaintext key would otherwise
    # persist it silently.
    if not crypto_blob.is_encrypted(payload.enc_settings):
        raise HTTPException(status_code=400, detail="Inställningar måste vara krypterade")
    pg.execute_void(
        "UPDATE users SET enc_settings = %s, settings_updated_at = NOW() WHERE id = %s",
        (payload.enc_settings, user["user_id"]),
    )


@router.delete("/settings", status_code=204, response_model=None)
def delete_settings(user: dict = Depends(get_current_user)) -> None:
    """'Glöm min nyckel' — drop the stored blob without touching the account."""
    pg.execute_void(
        "UPDATE users SET enc_settings = NULL, settings_updated_at = NOW() WHERE id = %s",
        (user["user_id"],),
    )
