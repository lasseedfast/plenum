"""Zero-knowledge account auth.

The client stretches the password with PBKDF2 and sends only a derived *auth
key* — the password itself never reaches the server, and the encryption key
(DEK) only ever leaves the browser wrapped. This module handles the parts the
server IS allowed to know:

- bcrypt hash/verify of the client-derived auth key
- opaque bearer tokens (random 32 bytes; sha256 stored in ``auth_tokens``,
  sliding 90-day expiry)
- FastAPI dependencies ``get_current_user`` / ``get_optional_user``
- deterministic fake KDF salts for unknown usernames so the prelogin endpoint
  can't be used to enumerate accounts
- a small in-memory per-IP throttle for the credential endpoints
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import os
import re
import secrets
import threading
import time

import bcrypt
from fastapi import Header, HTTPException, Request

from postgres_client import pg

log = logging.getLogger("riksdagen.auth")

TOKEN_TTL_DAYS = 90
# Refresh the sliding expiry at most this often, so token verification isn't a
# write per request.
_TOKEN_TOUCH_SECS = 3600

USERNAME_RE = re.compile(r"^[a-z0-9](?:[a-z0-9._-]{1,30})[a-z0-9]$")


# ── auth-key hashing ─────────────────────────────────────────────────────────


def hash_auth_key(auth_key: str) -> str:
    """bcrypt the client-derived auth key (base64, ~44 chars — under bcrypt's
    72-byte cap). The heavy stretching already happened client-side; this stops
    a DB leak from yielding usable login credentials."""
    return bcrypt.hashpw(auth_key.encode("utf-8"), bcrypt.gensalt()).decode("ascii")


def verify_auth_key(auth_key: str, auth_hash: str) -> bool:
    try:
        return bcrypt.checkpw(auth_key.encode("utf-8"), auth_hash.encode("ascii"))
    except Exception:
        return False


# ── prelogin fake salts (anti-enumeration) ───────────────────────────────────


def _prelogin_secret() -> bytes:
    """Key for deterministic fake salts. Prefers AUTH_PRELOGIN_SECRET; falls
    back to a derivation of PG_PASSWORD (stable across restarts, never sent
    anywhere); last resort is a per-process random (fake salts then vary
    across restarts, which weakens — but doesn't break — enumeration cover)."""
    explicit = os.getenv("AUTH_PRELOGIN_SECRET")
    if explicit:
        return explicit.encode("utf-8")
    pg_pw = os.getenv("PG_PASSWORD")
    if pg_pw:
        return hashlib.sha256(b"riksdagen-prelogin:" + pg_pw.encode("utf-8")).digest()
    log.warning("no AUTH_PRELOGIN_SECRET/PG_PASSWORD; using per-process fake-salt key")
    return _process_secret


_process_secret = secrets.token_bytes(32)


def fake_kdf_salt(username: str) -> str:
    """A stable, plausible-looking salt for usernames that don't exist."""
    digest = hmac.new(_prelogin_secret(), f"salt:{username}".encode(), hashlib.sha256)
    return base64.b64encode(digest.digest()[:16]).decode("ascii")


# ── tokens ───────────────────────────────────────────────────────────────────


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def mint_token(user_id: str) -> str:
    token = secrets.token_urlsafe(32)
    pg.execute_void(
        """
        INSERT INTO auth_tokens (token_hash, user_id, expires_at)
        VALUES (%s, %s, NOW() + make_interval(days => %s))
        """,
        (_token_hash(token), user_id, TOKEN_TTL_DAYS),
    )
    return token


def revoke_token(token: str) -> None:
    pg.execute_void("DELETE FROM auth_tokens WHERE token_hash = %s", (_token_hash(token),))


def revoke_other_tokens(user_id: str, keep_token: str) -> None:
    pg.execute_void(
        "DELETE FROM auth_tokens WHERE user_id = %s AND token_hash != %s",
        (user_id, _token_hash(keep_token)),
    )


def verify_token(token: str) -> dict | None:
    """{"user_id", "username"} for a live token, else None. Slides expiry."""
    th = _token_hash(token)
    rows = pg.execute(
        """
        SELECT t.user_id::text AS user_id, u.username,
               (t.last_used_at < NOW() - make_interval(secs => %s)) AS stale_touch
        FROM auth_tokens t JOIN users u ON u.id = t.user_id
        WHERE t.token_hash = %s AND t.expires_at > NOW()
        """,
        (_TOKEN_TOUCH_SECS, th),
    )
    if not rows:
        return None
    row = rows[0]
    if row.get("stale_touch"):
        try:
            pg.execute_void(
                """
                UPDATE auth_tokens
                SET last_used_at = NOW(),
                    expires_at = NOW() + make_interval(days => %s)
                WHERE token_hash = %s
                """,
                (TOKEN_TTL_DAYS, th),
            )
        except Exception:
            pass
    return {"user_id": row["user_id"], "username": row["username"]}


# ── FastAPI dependencies ─────────────────────────────────────────────────────


def get_optional_user(authorization: str | None = Header(default=None)) -> dict | None:
    if not authorization or not authorization.startswith("Bearer "):
        return None
    token = authorization[len("Bearer "):].strip()
    if not token:
        return None
    return verify_token(token)


def get_current_user(authorization: str | None = Header(default=None)) -> dict:
    user = get_optional_user(authorization)
    if user is None:
        raise HTTPException(status_code=401, detail="Inloggning krävs")
    return user


def bearer_token(authorization: str | None = Header(default=None)) -> str | None:
    if not authorization or not authorization.startswith("Bearer "):
        return None
    return authorization[len("Bearer "):].strip() or None


# ── per-IP throttle (in-memory, best-effort) ─────────────────────────────────

_THROTTLE_WINDOW_SECS = 300
_THROTTLE_MAX_ATTEMPTS = 20

_attempts: dict[str, list[float]] = {}
_attempts_lock = threading.Lock()


def client_ip(request: Request) -> str:
    fwd = request.headers.get("X-Forwarded-For")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def throttle(request: Request) -> None:
    """429 when one IP hammers the credential endpoints. In-memory: resets on
    restart and is per-worker — a guardrail, not a fortress."""
    ip = client_ip(request)
    now = time.time()
    with _attempts_lock:
        window = [t for t in _attempts.get(ip, []) if now - t < _THROTTLE_WINDOW_SECS]
        if len(window) >= _THROTTLE_MAX_ATTEMPTS:
            _attempts[ip] = window
            raise HTTPException(status_code=429, detail="För många försök — vänta en stund")
        window.append(now)
        _attempts[ip] = window
        if len(_attempts) > 10_000:  # bound memory under address churn
            _attempts.clear()
