#!/usr/bin/env python
"""End-to-end test of the zero-knowledge login + encrypted sessions/research.

Mimics the browser client byte-for-byte (PBKDF2-SHA256 → HKDF split →
AES-256-GCM "v1:" blobs, matching frontend/src/crypto.ts) and drives a running
API instance. Verifies both behavior (signup/login/ownership) and the actual
at-rest state in Postgres: no plaintext content or password material anywhere.

Usage:
    .venv/bin/python scripts/test_auth_e2e.py [--base http://127.0.0.1:8899] [--keep]

Creates a throwaway account + data and deletes them afterwards (unless --keep).
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import secrets
import sys
import time
import uuid

import requests

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from cryptography.hazmat.primitives import hashes  # noqa: E402
from cryptography.hazmat.primitives.ciphers.aead import AESGCM  # noqa: E402
from cryptography.hazmat.primitives.kdf.hkdf import HKDF  # noqa: E402

from postgres_client import pg  # noqa: E402

KDF_ITERATIONS = 600_000
PREFIX = "v1:"

FAILURES: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {'✓' if ok else '✗ FAIL'} {name}" + (f" — {detail}" if detail and not ok else ""))
    if not ok:
        FAILURES.append(name)


# ── client-side crypto, mirroring frontend/src/crypto.ts ────────────────────

def derive_keys(password: str, kdf_salt_b64: str, iterations: int = KDF_ITERATIONS):
    master = hashlib.pbkdf2_hmac(
        "sha256", password.encode(), base64.b64decode(kdf_salt_b64), iterations
    )

    def expand(info: bytes) -> bytes:
        return HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=info).derive(master)

    auth_key = base64.b64encode(expand(b"riksdagen-auth-v1")).decode()
    kek = expand(b"riksdagen-enc-v1")
    return auth_key, kek


def enc_blob(key: bytes, plaintext: str) -> str:
    iv = os.urandom(12)
    ct = AESGCM(key).encrypt(iv, plaintext.encode(), None)
    return PREFIX + base64.b64encode(iv + ct).decode()


def dec_blob(key: bytes, blob: str) -> str:
    raw = base64.b64decode(blob[len(PREFIX):])
    return AESGCM(key).decrypt(raw[:12], raw[12:], None).decode()


def is_ciphertext(value) -> bool:
    return isinstance(value, str) and value.startswith(PREFIX)


# ── test phases ──────────────────────────────────────────────────────────────

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8899")
    ap.add_argument("--keep", action="store_true", help="keep the test account + data")
    args = ap.parse_args()
    base = args.base.rstrip("/")

    username = f"e2e-test-{secrets.token_hex(4)}"
    password = "korrekt häst batteri-stapel 9"
    session_uuid = str(uuid.uuid4())
    board_id = None
    user_id = None

    print(f"API: {base}   user: {username}")

    # 1 ── signup ------------------------------------------------------------
    print("\n[1] Signup")
    kdf_salt = base64.b64encode(os.urandom(16)).decode()
    auth_key, kek = derive_keys(password, kdf_salt)
    dek = os.urandom(32)
    wrapped_dek = enc_blob(kek, base64.b64encode(dek).decode())
    # NOTE frontend wraps raw DEK bytes; here we wrap its b64 — irrelevant for
    # the test since we only unwrap our own blob, but keep DB assertions exact.
    r = requests.post(f"{base}/api/auth/signup", json={
        "username": username, "auth_key": auth_key, "kdf_salt": kdf_salt,
        "kdf_iterations": KDF_ITERATIONS, "wrapped_dek": wrapped_dek,
    })
    check("signup 201", r.status_code == 201, f"{r.status_code} {r.text[:200]}")
    token = r.json()["token"]
    user_id = r.json()["user_id"]
    auth = {"Authorization": f"Bearer {token}"}

    row = pg.execute("SELECT * FROM users WHERE username = %s", (username,))[0]
    check("password not stored anywhere",
          password not in json.dumps(row, default=str))
    check("auth_hash is bcrypt (not the auth key)",
          row["auth_hash"].startswith("$2") and auth_key not in row["auth_hash"])
    check("wrapped_dek is v1 ciphertext", is_ciphertext(row["wrapped_dek"]))

    dup = requests.post(f"{base}/api/auth/signup", json={
        "username": username, "auth_key": auth_key, "kdf_salt": kdf_salt,
        "kdf_iterations": KDF_ITERATIONS, "wrapped_dek": wrapped_dek,
    })
    check("duplicate username rejected (409)", dup.status_code == 409)

    # 2 ── prelogin / login ---------------------------------------------------
    print("\n[2] Prelogin + login")
    r = requests.get(f"{base}/api/auth/prelogin", params={"username": username})
    check("prelogin returns real salt", r.json().get("kdf_salt") == kdf_salt)
    r1 = requests.get(f"{base}/api/auth/prelogin", params={"username": "no-such-user-xyz"})
    r2 = requests.get(f"{base}/api/auth/prelogin", params={"username": "no-such-user-xyz"})
    check("unknown user gets stable fake salt",
          r1.status_code == 200 and r1.json()["kdf_salt"] == r2.json()["kdf_salt"])

    bad_key, _ = derive_keys("fel lösenord", kdf_salt)
    r = requests.post(f"{base}/api/auth/login", json={"username": username, "auth_key": bad_key})
    check("wrong password rejected (401)", r.status_code == 401)

    r = requests.post(f"{base}/api/auth/login", json={"username": username, "auth_key": auth_key})
    check("login ok", r.status_code == 200, f"{r.status_code} {r.text[:200]}")
    got_dek = base64.b64decode(dec_blob(kek, r.json()["wrapped_dek"]))
    check("DEK unwraps to the original", got_dek == dek)

    # 3 ── encrypted chat session --------------------------------------------
    print("\n[3] Encrypted chat session")
    secret_text = "HEMLIG-FRÅGA om vargjakt och Anders Ygeman"
    payload = {
        "llm_messages": [{"role": "user", "content": secret_text}],
        "turns": [{"question": secret_text, "status": "ready", "answerHtml": "<p>svar</p>", "sources": []}],
        "focus_ids": ["H40911"],
        "person_id": "0123456789",
        "initial_speech_id": None,
    }
    enc_payload = enc_blob(dek, json.dumps(payload, ensure_ascii=False))
    enc_title = enc_blob(dek, json.dumps({"title": secret_text[:80]}, ensure_ascii=False))
    r = requests.put(f"{base}/api/sessions/{session_uuid}", headers=auth, json={
        "session_type": "mp", "enc_payload": enc_payload, "enc_title": enc_title,
    })
    check("owned PUT 204", r.status_code == 204, f"{r.status_code} {r.text[:200]}")

    row = pg.execute("SELECT * FROM chat_sessions WHERE id = %s", (session_uuid,))[0]
    dumped = json.dumps({k: v for k, v in row.items() if k not in ("enc_payload", "enc_title")},
                        default=str)
    check("no plaintext content in DB row", secret_text not in dumped and "0123456789" not in dumped)
    check("plaintext columns scrubbed",
          (row["llm_messages"] or []) == [] and (row["turns"] or []) == []
          and row["person_id"] is None)
    check("enc_payload decrypts to the content",
          json.loads(dec_blob(dek, row["enc_payload"])) == payload)

    r = requests.get(f"{base}/api/sessions/{session_uuid}")
    check("anonymous GET of owned session 404", r.status_code == 404)
    r = requests.put(f"{base}/api/sessions/{session_uuid}", json={
        "session_type": "mp", "llm_messages": [], "turns": [{"question": "kapning"}], "focus_ids": [],
    })
    check("anonymous PUT can't overwrite owned session", r.status_code == 404)
    r = requests.get(f"{base}/api/sessions/{session_uuid}", headers=auth)
    check("owner GET returns enc_payload",
          r.status_code == 200 and r.json().get("enc_payload") == enc_payload)

    r = requests.get(f"{base}/api/me/chats", headers=auth)
    check("me/chats lists the session",
          r.status_code == 200 and any(c["id"] == session_uuid for c in r.json()))
    check("me/chats titles are ciphertext",
          all(is_ciphertext(c["enc_title"]) for c in r.json() if c["enc_title"]))

    # 4 ── encrypted research board -------------------------------------------
    print("\n[4] Encrypted research board")
    secret_topic = "HEMLIGT-ÄMNE kärnkraftens avveckling och effektskatten"
    board_key = os.urandom(32)
    board_key_b64 = base64.b64encode(board_key).decode()
    wrapped_board_key = enc_blob(dek, base64.b64encode(board_key).decode())
    r = requests.post(f"{base}/api/research", headers=auth, json={
        "topic": secret_topic, "board_key": board_key_b64,
        "wrapped_board_key": wrapped_board_key,
    })
    if r.status_code == 409:
        print("  ! another research job is running — skipping research phase")
    else:
        check("create research 200", r.status_code == 200, f"{r.status_code} {r.text[:200]}")
        board_id = r.json()["board_id"]
        job_id = r.json()["job_id"]

        brow = pg.execute("SELECT * FROM research_boards WHERE id = %s", (board_id,))[0]
        check("board title/topic are ciphertext",
              is_ciphertext(brow["title"]) and is_ciphertext(brow["topic"]))
        check("topic decrypts with board key",
              dec_blob(board_key, brow["topic"]) == secret_topic)
        check("board linked to user, enc flag set",
              str(brow["user_id"]) == user_id and brow["enc"] is True)

        jrow = pg.execute("SELECT params FROM jobs WHERE id = %s", (job_id,))[0]
        check("jobs.params has no topic/key",
              secret_topic not in json.dumps(jrow["params"], default=str)
              and board_key_b64 not in json.dumps(jrow["params"], default=str))

        r = requests.get(f"{base}/api/research/{board_id}")
        check("anonymous GET of owned board 404", r.status_code == 404)
        r = requests.get(f"{base}/api/research", headers=auth)
        check("board in my list", any(b["id"] == board_id for b in r.json()))

        # seed a thread (works whether or not the build job is still running)
        secret_seed = "HEMLIG-TRÅD vad sa Birger Schlaug egentligen?"
        r = requests.post(f"{base}/api/research/{board_id}/threads", headers=auth,
                          json={"text": secret_seed, "board_key": board_key_b64})
        check("seed thread ok", r.status_code == 200, f"{r.status_code} {r.text[:200]}")
        trows = pg.execute(
            "SELECT title, question FROM research_threads WHERE board_id = %s", (board_id,))
        check("thread rows are ciphertext",
              all(is_ciphertext(t["title"]) and is_ciphertext(t["question"]) for t in trows))
        seeded = [t for t in trows if dec_blob(board_key, t["question"]) == secret_seed]
        check("seed decrypts with board key", len(seeded) == 1)

        r = requests.post(f"{base}/api/research/{board_id}/threads", headers=auth,
                          json={"text": "nyckel saknas"})
        check("seed without board key rejected (400)", r.status_code == 400)

        # give the job a few seconds, then confirm events carry no plaintext
        time.sleep(6)
        erows = pg.execute(
            "SELECT event FROM job_events WHERE job_id = %s ORDER BY seq", (job_id,))
        if erows:
            all_events = json.dumps([e["event"] for e in erows], default=str)
            check("job events contain no board plaintext",
                  secret_topic not in all_events and secret_seed not in all_events)
            enc_events = [e["event"] for e in erows if e["event"].get("enc")]
            check("events use enc envelope", len(enc_events) == len(erows))
            if enc_events:
                decoded = json.loads(dec_blob(board_key, enc_events[0]["enc"]))
                check("event decrypts to message content", "message" in decoded)
        else:
            print("  ! no job events yet (job may not have started) — skipped event checks")

    # 5 ── BYO provider key never reaches the database -------------------------
    print("\n[5] Provider override stays out of the DB")
    fake_api_key = "sk-or-v1-E2E-FAKE-KEY-" + secrets.token_hex(8)
    byo_board_id = None
    r = requests.post(f"{base}/api/research", headers=auth, json={
        "topic": "BYO-nyckeltest: klimatpolitik",
        "llm": {"provider_id": "openrouter", "api_key": fake_api_key,
                "smart_model": "anthropic/claude-opus-4"},
    })
    # The job will fail fast on the bogus key — that is fine, and in fact it is
    # the interesting case: the failure text is what could leak the key.
    check("create with override accepted", r.status_code == 200,
          f"{r.status_code} {r.text[:200]}")
    if r.status_code == 200:
        byo_board_id = r.json()["board_id"]
        byo_job_id = r.json()["job_id"]
        jrow = pg.execute("SELECT params FROM jobs WHERE id = %s", (byo_job_id,))[0]
        params_json = json.dumps(jrow["params"], default=str)
        check("jobs.params carries the byo flag", jrow["params"].get("byo") is True)
        check("jobs.params has no key and no llm block",
              fake_api_key not in params_json and "llm" not in jrow["params"])

        # Let it fail, then confirm neither the error column nor the event
        # stream echoed the key back out of the provider's 401 body.
        time.sleep(8)
        jrow = pg.execute(
            "SELECT status, errors FROM jobs WHERE id = %s", (byo_job_id,))[0]
        check("key absent from jobs.errors",
              fake_api_key not in json.dumps(jrow["errors"], default=str),
              f"status={jrow['status']}")
        # A dead provider must fail the job, not quietly produce a board that
        # looks finished but only echoes the user's topic back.
        check("bad key fails the job (not a hollow board)",
              jrow["status"] == "failed", f"status={jrow['status']}")
        erows = pg.execute(
            "SELECT event FROM job_events WHERE job_id = %s", (byo_job_id,))
        check("key absent from job_events",
              fake_api_key not in json.dumps([e["event"] for e in erows], default=str))

    r = requests.post(f"{base}/api/research", headers=auth, json={
        "topic": "okänd leverantör", "llm": {"provider_id": "not-a-provider",
                                             "api_key": fake_api_key},
    })
    check("unknown provider rejected at request time (400)", r.status_code == 400)

    if byo_board_id:
        requests.delete(f"{base}/api/research/{byo_board_id}", headers=auth)

    # 6 ── encrypted account settings blob -------------------------------------
    print("\n[6] Account settings blob")
    settings = {"active_provider": "openrouter",
                "providers": {"openrouter": {"api_key": fake_api_key,
                                             "smart_model": "anthropic/claude-opus-4",
                                             "fast_model": "", "editor_model": ""}},
                "use_editor": False}
    enc_settings = enc_blob(dek, json.dumps(settings))

    r = requests.get(f"{base}/api/me/settings", headers=auth)
    check("settings empty for a fresh account",
          r.status_code == 200 and r.json()["enc_settings"] is None)

    r = requests.put(f"{base}/api/me/settings", headers=auth,
                     json={"enc_settings": json.dumps(settings)})
    check("plaintext settings rejected (400)", r.status_code == 400)

    r = requests.put(f"{base}/api/me/settings", headers=auth,
                     json={"enc_settings": enc_settings})
    check("put settings 204", r.status_code == 204, f"{r.status_code} {r.text[:200]}")

    srow = pg.execute("SELECT enc_settings FROM users WHERE id = %s", (user_id,))[0]
    check("stored settings are v1 ciphertext", is_ciphertext(srow["enc_settings"]))
    check("api key not stored in the clear", fake_api_key not in srow["enc_settings"])

    r = requests.get(f"{base}/api/me/settings", headers=auth)
    check("settings round-trip",
          json.loads(dec_blob(dek, r.json()["enc_settings"])) == settings)

    r = requests.get(f"{base}/api/me/settings")
    check("settings need auth (401)", r.status_code == 401)

    # A password change re-wraps the DEK but does not change it, so a blob
    # encrypted under the DEK must survive untouched.
    new_password = "annat lösenord som är långt 42"
    new_salt = base64.b64encode(os.urandom(16)).decode()
    new_auth_key, new_kek = derive_keys(new_password, new_salt)
    r = requests.post(f"{base}/api/auth/change-password", headers=auth, json={
        "auth_key": auth_key, "new_auth_key": new_auth_key,
        "new_kdf_salt": new_salt, "new_kdf_iterations": KDF_ITERATIONS,
        "new_wrapped_dek": enc_blob(new_kek, base64.b64encode(dek).decode()),
    })
    check("change-password 204", r.status_code == 204, f"{r.status_code} {r.text[:200]}")
    r = requests.post(f"{base}/api/auth/login",
                      json={"username": username, "auth_key": new_auth_key})
    check("login with new password", r.status_code == 200)
    auth = {"Authorization": f"Bearer {r.json()['token']}"}
    auth_key = new_auth_key  # later cleanup logs in again
    dek_after = base64.b64decode(dec_blob(new_kek, r.json()["wrapped_dek"]))
    check("DEK unchanged by password change", dek_after == dek)
    r = requests.get(f"{base}/api/me/settings", headers=auth)
    check("settings still decrypt after password change",
          json.loads(dec_blob(dek, r.json()["enc_settings"])) == settings)

    r = requests.delete(f"{base}/api/me/settings", headers=auth)
    check("delete settings 204", r.status_code == 204)
    srow = pg.execute("SELECT enc_settings FROM users WHERE id = %s", (user_id,))[0]
    check("settings cleared", srow["enc_settings"] is None)

    # 7 ── anonymous flows unchanged ------------------------------------------
    print("\n[7] Anonymous flows unchanged")
    anon_uuid = str(uuid.uuid4())
    r = requests.put(f"{base}/api/sessions/{anon_uuid}", json={
        "session_type": "general",
        "llm_messages": [{"role": "user", "content": "öppen fråga"}],
        "turns": [{"question": "öppen fråga", "status": "ready"}],
        "focus_ids": [],
    })
    check("anonymous PUT 204", r.status_code == 204, f"{r.status_code} {r.text[:200]}")
    r = requests.get(f"{base}/api/sessions/{anon_uuid}")
    check("anonymous GET ok, plaintext",
          r.status_code == 200 and r.json()["llm_messages"][0]["content"] == "öppen fråga")
    pg.execute_void("DELETE FROM chat_sessions WHERE id = %s", (anon_uuid,))

    # 8 ── logout + cleanup ----------------------------------------------------
    print("\n[8] Logout + cleanup")
    r = requests.post(f"{base}/api/auth/logout", headers=auth)
    check("logout 204", r.status_code == 204)
    r = requests.get(f"{base}/api/me/chats", headers=auth)
    check("token dead after logout (401)", r.status_code == 401)

    if not args.keep:
        # fresh token to delete the board through the API (cancels its job)
        r = requests.post(f"{base}/api/auth/login",
                          json={"username": username, "auth_key": auth_key})
        auth2 = {"Authorization": f"Bearer {r.json()['token']}"}
        if board_id:
            requests.delete(f"{base}/api/research/{board_id}", headers=auth2)
        pg.execute_void("DELETE FROM users WHERE id = %s", (user_id,))
        left = pg.execute("SELECT COUNT(*) AS n FROM chat_sessions WHERE user_id = %s", (user_id,))
        check("cascade removed owned sessions", left[0]["n"] == 0)
        print("  cleaned up test account + data")

    print(f"\n{'ALL OK' if not FAILURES else f'{len(FAILURES)} FAILURES: {FAILURES}'}")
    return 0 if not FAILURES else 1


if __name__ == "__main__":
    sys.exit(main())
