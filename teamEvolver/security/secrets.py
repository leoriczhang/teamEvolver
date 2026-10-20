"""Secret material handling.

Secret plaintext must never land in PostgreSQL, logs, trace, exceptions or task
payloads. Callers hold an opaque ``secret_ref`` (``sref_...``); the ciphertext
and a non-reversible fingerprint are persisted, and the plaintext is only
decrypted in-process on demand through :class:`SecretBox`.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets as _secrets
from dataclasses import dataclass

from cryptography.fernet import Fernet, InvalidToken

_SECRET_REF_PREFIX = "sref_"


@dataclass(frozen=True)
class SecretRef:
    """Opaque handle to an encrypted secret (safe to log/store/pass around)."""

    ref: str
    fingerprint: str


def new_secret_ref() -> str:
    """Generate a fresh opaque secret reference id."""
    return _SECRET_REF_PREFIX + _secrets.token_hex(16)


def secret_fingerprint(plaintext: str, *, salt: str = "teamEvolver.secret") -> str:
    """Non-reversible fingerprint for equality/rotation checks (not the secret).

    HMAC-SHA256 truncated — lets callers detect "same key as before" without
    being able to recover the plaintext from the stored value.
    """
    digest = hmac.new(salt.encode("utf-8"), str(plaintext).encode("utf-8"), hashlib.sha256)
    return digest.hexdigest()[:32]


def mask_secret(value: str) -> str:
    """Render a secret for human-facing output without revealing it."""
    text = str(value or "")
    if not text:
        return ""
    if len(text) <= 8:
        return "*" * len(text)
    return f"{text[:3]}…{text[-2:]} ({len(text)} chars)"


def derive_fernet_key(master: str) -> bytes:
    """Derive a urlsafe-base64 Fernet key from an arbitrary master string."""
    digest = hashlib.sha256(str(master).encode("utf-8")).digest()
    return base64.urlsafe_b64encode(digest)


class SecretBox:
    """Symmetric encrypt/decrypt around a process-held master key.

    The master key comes from bootstrap config (env), never from PG. Ciphertext
    is safe to persist; plaintext is produced only transiently in-process.
    """

    def __init__(self, master_key: str) -> None:
        if not str(master_key or "").strip():
            raise ValueError("SecretBox requires a non-empty master key")
        self._fernet = Fernet(derive_fernet_key(master_key))

    def encrypt(self, plaintext: str) -> str:
        return self._fernet.encrypt(str(plaintext).encode("utf-8")).decode("ascii")

    def decrypt(self, ciphertext: str) -> str:
        try:
            return self._fernet.decrypt(str(ciphertext).encode("ascii")).decode("utf-8")
        except InvalidToken as exc:
            # Never echo ciphertext or key material in the error.
            raise ValueError("secret decryption failed (wrong master key or corrupt ciphertext)") from exc
