"""Persistence for secret references (``secret_refs`` table, RLS-scoped).

Stores only the opaque ref, ciphertext, purpose and fingerprint — never the
plaintext. All access is tenant-scoped by RLS; the connection must already
carry ``app.tenant_id``.
"""

from __future__ import annotations

from typing import Any

from ..database.transaction import TransactionManager
from .secrets import SecretBox, SecretRef, new_secret_ref, secret_fingerprint


class SecretRepository:
    def __init__(self, tx: TransactionManager, box: SecretBox) -> None:
        self._tx = tx
        self._box = box

    @property
    def _schema(self) -> str:
        return self._tx.pool.schema

    async def store(self, conn: Any, *, tenant_id: str, plaintext: str, purpose: str = "") -> SecretRef:
        """Encrypt and persist *plaintext*, returning its opaque reference."""
        ref = new_secret_ref()
        fingerprint = secret_fingerprint(plaintext)
        await conn.execute(
            f"INSERT INTO {self._schema}.secret_refs "
            "(tenant_id, secret_ref, purpose, ciphertext, fingerprint) "
            "VALUES ($1, $2, $3, $4, $5)",
            tenant_id,
            ref,
            purpose,
            self._box.encrypt(plaintext),
            fingerprint,
        )
        return SecretRef(ref=ref, fingerprint=fingerprint)

    async def resolve(self, conn: Any, *, secret_ref: str) -> str:
        """Decrypt and return the plaintext for *secret_ref* (in-process only)."""
        row = await conn.fetchrow(
            f"SELECT ciphertext FROM {self._schema}.secret_refs WHERE secret_ref = $1",
            secret_ref,
        )
        if row is None:
            raise KeyError(f"unknown secret_ref: {secret_ref}")
        return self._box.decrypt(row["ciphertext"])

    async def describe(self, conn: Any, *, secret_ref: str) -> dict[str, Any] | None:
        """Return non-sensitive metadata (never the ciphertext or plaintext)."""
        row = await conn.fetchrow(
            f"SELECT secret_ref, purpose, fingerprint, created_at "
            f"FROM {self._schema}.secret_refs WHERE secret_ref = $1",
            secret_ref,
        )
        if row is None:
            return None
        return {
            "secret_ref": row["secret_ref"],
            "purpose": row["purpose"],
            "fingerprint": row["fingerprint"],
            "created_at": row["created_at"],
        }
