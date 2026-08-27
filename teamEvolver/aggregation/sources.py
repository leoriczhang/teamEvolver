"""Account -> users -> per-category source URI expansion and batching.

The source builder is deliberately free of any LLM logic. It only:

1. Enumerates real users under an OpenViking account via the Admin API
   (request-scoped Root/Admin Key), mirroring the request shape already used by
   ``users_admin._openviking_sync`` (``GET /api/v1/admin/accounts/{account}/users``).
2. Maps each user + memory category to a canonical source URI
   (``viking://user/<uid>/memories/<category>``).
3. Batches those sources under the ``ov compile`` 16-source ceiling so a single
   compile task never exceeds the limit. Batching is per category first (all
   users' ``experiences`` compile together into one target directory), then
   split into sub-batches when a category has more than ``max_users_per_batch``
   users.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable

import httpx

# ov compile hard limit today; keep headroom below 16 so the skill/target
# roots do not push a batch over the ceiling.
_COMPILE_SOURCE_CEILING = 16
_DEFAULT_MAX_USERS_PER_BATCH = 12
_DEFAULT_ACCOUNT_USER_LIMIT = 10_000

# Built-in OpenViking memory categories worth aggregating. ``identity``/``soul``
# are intentionally excluded: they are assistant-persona files, not team
# knowledge. ``profile`` is handled as a category too (person/team-overview
# synthesis is expressed in the OKF skill, not here).
DEFAULT_MEMORY_KINDS = (
    "profile",
    "entities",
    "preferences",
    "events",
    "cases",
    "patterns",
    "trajectories",
    "experiences",
    "tools",
    "skills",
)


@dataclass(frozen=True)
class CompileBatch:
    """One planned ``ov compile`` invocation for a single memory category."""

    kind: str
    target_uri: str
    source_uris: tuple[str, ...]
    batch_index: int = 0
    total_batches: int = 1

    @property
    def group_key(self) -> str:
        """Stable identifier for incremental state and status tracking."""
        if self.total_batches > 1:
            return f"{self.kind}#{self.batch_index}"
        return self.kind


class SourceExpansionError(RuntimeError):
    """Raised when account users cannot be resolved."""


@dataclass(frozen=True, repr=False)
class AccountUserCredential:
    """One Account user and its optional request-scoped API credential."""

    user_id: str
    role: str = "user"
    api_key: str = ""
    key_prefix: str = ""


@dataclass
class AccountSourceBuilder:
    """Resolve an account's users and plan per-category compile batches."""

    endpoint: str
    api_key: str
    account_id: str
    shared_knowledge_prefix: str = "shared-knowledge"
    max_users_per_batch: int = _DEFAULT_MAX_USERS_PER_BATCH
    account_user_limit: int = _DEFAULT_ACCOUNT_USER_LIMIT
    timeout_seconds: float = 15.0
    # Users that must never be treated as an aggregation source (the team user
    # that owns the shared output, system/service users, etc.).
    excluded_user_ids: frozenset[str] = field(default_factory=frozenset)

    def _admin_headers(self) -> dict[str, str]:
        return {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "X-API-Key": self.api_key,
            "Authorization": f"Bearer {self.api_key}",
        }

    async def list_account_users(self) -> list[str]:
        """Return normalized user_ids under the account (excluding filtered).

        Uses the OpenViking Admin API with the request-scoped API key.
        Authorization failures surface as ``SourceExpansionError`` with the
        upstream message.
        """
        records = await self.list_account_user_credentials()
        return [
            record.user_id
            for record in records
            if record.user_id and record.user_id not in self.excluded_user_ids
        ]

    async def list_account_user_credentials(self) -> list[AccountUserCredential]:
        """Return Account user records without exposing them to HTTP callers."""
        if not self.endpoint:
            raise SourceExpansionError("OpenViking endpoint is not configured")
        if not self.api_key:
            raise SourceExpansionError("aggregation requires an OpenViking API key")
        limit = max(1, int(self.account_user_limit))
        url = f"{self.endpoint.rstrip('/')}/api/v1/admin/accounts/{self.account_id}/users"
        try:
            async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
                response = await client.get(
                    url,
                    params={"limit": limit},
                    headers=self._admin_headers(),
                )
        except httpx.HTTPError as exc:
            raise SourceExpansionError(f"OpenViking is unreachable: {exc}") from exc
        if response.status_code >= 400:
            raise SourceExpansionError(
                f"admin list-users failed (HTTP {response.status_code}): "
                f"{(response.text or '')[:300]}"
            )
        try:
            payload = response.json()
        except ValueError as exc:
            raise SourceExpansionError("admin list-users returned non-JSON") from exc
        records = _extract_user_credentials(payload)
        if len(records) >= limit:
            raise SourceExpansionError(
                f"admin list-users reached safety limit ({limit}); "
                "refine the Account or increase pagination support"
            )
        return records

    def plan_batches(
        self,
        user_ids: Iterable[str],
        kinds: Iterable[str],
    ) -> list[CompileBatch]:
        """Plan compile batches for the given users and memory categories."""
        users = [u for u in dict.fromkeys(user_ids) if u]
        batches: list[CompileBatch] = []
        cap = max(1, min(int(self.max_users_per_batch), _COMPILE_SOURCE_CEILING - 1))
        for kind in dict.fromkeys(kinds):
            target = self._target_uri(kind)
            chunks = [users[i : i + cap] for i in range(0, len(users), cap)] or [[]]
            total = len(chunks)
            for index, chunk in enumerate(chunks):
                source_uris = tuple(self._source_uri(uid, kind) for uid in chunk)
                if not source_uris:
                    continue
                batches.append(
                    CompileBatch(
                        kind=kind,
                        target_uri=target,
                        source_uris=source_uris,
                        batch_index=index,
                        total_batches=total,
                    )
                )
        return batches

    def _target_uri(self, kind: str) -> str:
        prefix = self.shared_knowledge_prefix.strip("/") or "shared-knowledge"
        return f"viking://resources/{prefix}/{kind}"

    @staticmethod
    def _source_uri(user_id: str, kind: str) -> str:
        return f"viking://user/{user_id}/memories/{kind}"


def _extract_user_credentials(payload: Any) -> list[AccountUserCredential]:
    """Pull normalized user records from the admin list-users response."""
    items: Any = payload
    if isinstance(payload, dict):
        items = payload.get("result", payload)
        if isinstance(items, dict):
            items = items.get("users", items.get("items", []))
    if not isinstance(items, (list, tuple)):
        return []
    records: dict[str, AccountUserCredential] = {}
    for item in items:
        if isinstance(item, str):
            user_id = item.strip()
            if user_id:
                records.setdefault(user_id, AccountUserCredential(user_id=user_id))
        elif isinstance(item, dict):
            value = item.get("user_id") or item.get("id") or item.get("name")
            if isinstance(value, str) and value.strip():
                user_id = value.strip()
                record = AccountUserCredential(
                    user_id=user_id,
                    role=str(item.get("role") or "user").strip().lower(),
                    api_key=str(item.get("api_key") or "").strip(),
                    key_prefix=str(item.get("key_prefix") or "").strip(),
                )
                existing = records.get(user_id)
                if existing is None or (record.api_key and not existing.api_key):
                    records[user_id] = record
    return list(records.values())
