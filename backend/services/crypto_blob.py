"""Symmetric encryption for the zero-knowledge "v1:" blob format.

Format: "v1:" + base64(iv[12] || AES-256-GCM ciphertext+tag). This matches the
browser's WebCrypto output exactly (12-byte IV, 128-bit tag appended), so blobs
written by either side decrypt on the other.

Used by the research job pipeline: the raw per-board key arrives with a spawn
request, travels to the job child via stdin, lives only in process memory, and
encrypts/decrypts board content on its way to/from Postgres. Nothing here ever
persists a key.
"""
from __future__ import annotations

import base64
import os
from typing import Optional

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

PREFIX = "v1:"
_IV_LEN = 12


def is_encrypted(value: object) -> bool:
    return isinstance(value, str) and value.startswith(PREFIX)


def encrypt_str(plaintext: str, key: bytes) -> str:
    iv = os.urandom(_IV_LEN)
    ct = AESGCM(key).encrypt(iv, plaintext.encode("utf-8"), None)
    return PREFIX + base64.b64encode(iv + ct).decode("ascii")


def decrypt_str(blob: str, key: bytes) -> str:
    if not is_encrypted(blob):
        raise ValueError("not a v1 blob")
    raw = base64.b64decode(blob[len(PREFIX):])
    return AESGCM(key).decrypt(raw[:_IV_LEN], raw[_IV_LEN:], None).decode("utf-8")


def enc(value: Optional[str], key: Optional[bytes]) -> Optional[str]:
    """Encrypt when a key is present; passthrough otherwise (plaintext path)."""
    if key is None or value is None:
        return value
    return encrypt_str(value, key)


def dec(value: Optional[str], key: Optional[bytes]) -> Optional[str]:
    """Decrypt v1 blobs when a key is present; passthrough anything else."""
    if key is None or not is_encrypted(value):
        return value
    return decrypt_str(value, key)  # type: ignore[arg-type]
