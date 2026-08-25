"""Account endpoints for optional zero-knowledge login.

The client does all password work locally (PBKDF2 → auth key + wrapping key);
these routes only ever see the derived auth key and opaque wrapped blobs.
See backend/services/auth.py for the primitives.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from backend.services import auth as auth_svc
from postgres_client import pg

log = logging.getLogger("riksdagen.auth")

router = APIRouter(prefix="/api/auth", tags=["auth"])

KDF_MIN_ITERATIONS = 100_000


class SignupRequest(BaseModel):
    username: str = Field(..., min_length=3, max_length=32)
    auth_key: str = Field(..., min_length=20, max_length=200)
    kdf_salt: str = Field(..., min_length=8, max_length=64)
    kdf_iterations: int = Field(default=600_000, ge=KDF_MIN_ITERATIONS, le=5_000_000)
    wrapped_dek: str = Field(..., min_length=10, max_length=500)


class LoginRequest(BaseModel):
    username: str = Field(..., min_length=1, max_length=64)
    auth_key: str = Field(..., min_length=20, max_length=200)


class ChangePasswordRequest(BaseModel):
    auth_key: str = Field(..., min_length=20, max_length=200)
    new_auth_key: str = Field(..., min_length=20, max_length=200)
    new_kdf_salt: str = Field(..., min_length=8, max_length=64)
    new_kdf_iterations: int = Field(default=600_000, ge=KDF_MIN_ITERATIONS, le=5_000_000)
    new_wrapped_dek: str = Field(..., min_length=10, max_length=500)


class AuthResponse(BaseModel):
    token: str
    user_id: str
    username: str
    wrapped_dek: str
    kdf_salt: str
    kdf_iterations: int


@router.post("/signup", response_model=AuthResponse, status_code=201)
def signup(payload: SignupRequest, request: Request) -> AuthResponse:
    auth_svc.throttle(request)
    username = payload.username.strip().lower()
    if not auth_svc.USERNAME_RE.match(username):
        raise HTTPException(
            status_code=400,
            detail="Användarnamn: 3–32 tecken, a-z, 0-9, punkt, bindestreck, understreck",
        )
    existing = pg.execute("SELECT 1 FROM users WHERE username = %s", (username,))
    if existing:
        raise HTTPException(status_code=409, detail="Användarnamnet är upptaget")

    rows = pg.execute(
        """
        INSERT INTO users (username, auth_hash, kdf_salt, kdf_iterations, wrapped_dek)
        VALUES (%s, %s, %s, %s, %s)
        RETURNING id::text AS id
        """,
        (
            username,
            auth_svc.hash_auth_key(payload.auth_key),
            payload.kdf_salt,
            payload.kdf_iterations,
            payload.wrapped_dek,
        ),
    )
    user_id = rows[0]["id"]
    token = auth_svc.mint_token(user_id)
    log.info("new account: %s", username)
    return AuthResponse(
        token=token,
        user_id=user_id,
        username=username,
        wrapped_dek=payload.wrapped_dek,
        kdf_salt=payload.kdf_salt,
        kdf_iterations=payload.kdf_iterations,
    )


@router.get("/prelogin")
def prelogin(username: str, request: Request) -> dict:
    """KDF parameters for a username. Unknown usernames get a deterministic
    fake salt so responses don't reveal whether an account exists."""
    auth_svc.throttle(request)
    username = username.strip().lower()
    rows = pg.execute(
        "SELECT kdf_salt, kdf_iterations FROM users WHERE username = %s", (username,)
    )
    if rows:
        return {"kdf_salt": rows[0]["kdf_salt"], "kdf_iterations": rows[0]["kdf_iterations"]}
    return {"kdf_salt": auth_svc.fake_kdf_salt(username), "kdf_iterations": 600_000}


@router.post("/login", response_model=AuthResponse)
def login(payload: LoginRequest, request: Request) -> AuthResponse:
    auth_svc.throttle(request)
    username = payload.username.strip().lower()
    rows = pg.execute(
        """
        SELECT id::text AS id, auth_hash, kdf_salt, kdf_iterations, wrapped_dek
        FROM users WHERE username = %s
        """,
        (username,),
    )
    if not rows or not auth_svc.verify_auth_key(payload.auth_key, rows[0]["auth_hash"]):
        raise HTTPException(status_code=401, detail="Fel användarnamn eller lösenord")
    row = rows[0]
    token = auth_svc.mint_token(row["id"])
    return AuthResponse(
        token=token,
        user_id=row["id"],
        username=username,
        wrapped_dek=row["wrapped_dek"],
        kdf_salt=row["kdf_salt"],
        kdf_iterations=row["kdf_iterations"],
    )


@router.post("/logout", status_code=204, response_model=None)
def logout(token: str | None = Depends(auth_svc.bearer_token)) -> None:
    if token:
        auth_svc.revoke_token(token)


@router.get("/me")
def me(user: dict = Depends(auth_svc.get_current_user)) -> dict:
    rows = pg.execute(
        "SELECT wrapped_dek, kdf_salt, kdf_iterations FROM users WHERE id = %s",
        (user["user_id"],),
    )
    if not rows:
        raise HTTPException(status_code=404, detail="Kontot finns inte längre")
    return {"user_id": user["user_id"], "username": user["username"], **rows[0]}


@router.post("/change-password", status_code=204, response_model=None)
def change_password(
    payload: ChangePasswordRequest,
    request: Request,
    user: dict = Depends(auth_svc.get_current_user),
    token: str | None = Depends(auth_svc.bearer_token),
) -> None:
    """Re-wrap flow: the client re-wraps the same DEK under the new password's
    KEK, so stored content needs no re-encryption. Other sessions are logged out."""
    auth_svc.throttle(request)
    rows = pg.execute("SELECT auth_hash FROM users WHERE id = %s", (user["user_id"],))
    if not rows or not auth_svc.verify_auth_key(payload.auth_key, rows[0]["auth_hash"]):
        raise HTTPException(status_code=401, detail="Fel lösenord")
    pg.execute_void(
        """
        UPDATE users
        SET auth_hash = %s, kdf_salt = %s, kdf_iterations = %s, wrapped_dek = %s
        WHERE id = %s
        """,
        (
            auth_svc.hash_auth_key(payload.new_auth_key),
            payload.new_kdf_salt,
            payload.new_kdf_iterations,
            payload.new_wrapped_dek,
            user["user_id"],
        ),
    )
    if token:
        auth_svc.revoke_other_tokens(user["user_id"], token)
