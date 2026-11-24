"""Retained tenant key references; authentication continues to use the hash."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
from types import SimpleNamespace

from ..security import SecretBox, SecretRepository
from ..storage.postgres import PostgresObjectStore

_KEY = "tenant-credentials/agent-token.json"


class TenantCredentials:
    def __init__(self, runtime, tenant_id):
        tx = SimpleNamespace(pool=runtime)
        self.tenant_id = tenant_id
        self.objects = PostgresObjectStore(tx, tenant_id)
        master = os.environ.get("TEAMEVOLVER_SECRET_MASTER_KEY") or os.environ.get("TEAMEVOLVER_ROOT_API_KEY")
        if not master:
            raise RuntimeError("configure TEAMEVOLVER_SECRET_MASTER_KEY before retaining tenant keys")
        self.secrets = SecretRepository(tx, SecretBox(master))

    async def _retain_ref(self, conn, ref, token_hash):
        await self.objects.put(conn, _KEY, json.dumps({
            "secret_ref": ref, "token_hash": token_hash,
        }).encode())

    async def retain(self, conn, token, token_hash):
        # Repeated import of the same key reuses its existing reference.
        if await self.read(conn, token_hash) == token:
            return
        ref = await self.secrets.store(
            conn, tenant_id=self.tenant_id, plaintext=token, purpose="tenant-agent-token",
        )
        await self._retain_ref(conn, ref.ref, token_hash)

    async def read(self, conn, token_hash):
        try:
            record = json.loads(await self.objects.get(conn, _KEY))
            if record.get("token_hash") == token_hash:
                token = await self.secrets.resolve(conn, secret_ref=record["secret_ref"])
                if hmac.compare_digest(hashlib.sha256(token.encode()).hexdigest(), token_hash):
                    return token
        except (FileNotFoundError, KeyError, ValueError, TypeError):
            pass
        # Legacy MA connections may already retain this tenant's original key.
        ref = await self.secrets.find_by_sha256(conn, token_hash)
        if ref is None:
            return None
        await self._retain_ref(conn, ref, token_hash)
        return await self.secrets.resolve(conn, secret_ref=ref)
