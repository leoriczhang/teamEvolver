"""Injectable console session manager backed by shared identity state.

Peer replicas validate sessions from the shared identity store. No process-local
login state is authoritative; callers receive renewed cookies through the HTTP
runtime response hook."""

from __future__ import annotations

import secrets
import threading
import time
from typing import Any

from starlette.exceptions import HTTPException

from .identity_store import IdentityStore
from .users import _find_user, _public_user

SESSION_TTL_SECONDS = 24 * 60 * 60
SESSION_RENEW_INTERVAL_SECONDS = 5 * 60


class ConsoleSessionManager:
    """Issue, resolve, renew and revoke browser console login sessions.

    One instance is built by the composition root and shared by the auth router
    and the authorizer. It owns the small per-replica cache; the durable record
    always lives in the injected :class:`IdentityStore`.
    """

    def __init__(self, config: Any, *, ttl_seconds: float = SESSION_TTL_SECONDS,
                 renew_interval_seconds: float = SESSION_RENEW_INTERVAL_SECONDS) -> None:
        self._config = config
        self._ttl = float(ttl_seconds)
        self._renew_interval = float(renew_interval_seconds)
        self._cache: dict[str, dict[str, Any]] = {}
        self._lock = threading.RLock()

    @property
    def config(self) -> Any:
        return self._config

    @property
    def cache(self) -> dict[str, dict[str, Any]]:
        """The per-replica session cache (test visibility; not authoritative)."""
        return self._cache

    def _store(self) -> IdentityStore:
        return IdentityStore(self._config)

    def issue(self, user_id: str) -> str:
        """Create a session for *user_id*, persist it and return its token."""
        token = secrets.token_urlsafe(32)
        now = time.time()
        with self._lock:
            self._cache[token] = {
                "user_id": user_id,
                "created_at": now,
                "expires_at": now + self._ttl,
            }
            self._store().session(token, value=self._cache[token])
        return token

    def revoke(self, token: str) -> bool:
        """Delete a session everywhere (cache + durable store)."""
        with self._lock:
            self._cache.pop(token, None)
            return self._store().session(token, delete=True)

    def resolve(self, token: str) -> dict | None:
        """Return the public user for *token*, renewing lazily; else ``None``.

        A cache miss falls back to the shared store so a peer replica's login is
        recognised. Expired or disabled sessions are revoked. ``renew_token`` on
        the caller lets the HTTP layer refresh the cookie after an interval.
        """
        if not token:
            return None
        store = self._store()
        session = store.session(token)
        if not isinstance(session, dict):
            with self._lock:
                self._cache.pop(token, None)
            return None
        with self._lock:
            cached_session = self._cache.get(token)
            if cached_session is None:
                self._cache[token] = session
            else:
                # Each shared-store read returns a fresh dict. Keep one
                # canonical object per token while refreshing its persisted
                # values, so concurrent requests don't invalidate each other.
                expires_at = max(
                    float(cached_session.get("expires_at", 0) or 0),
                    float(session.get("expires_at", 0) or 0),
                )
                cached_session.update(session)
                cached_session["expires_at"] = expires_at
                session = cached_session
            if float(session.get("expires_at", 0) or 0) <= time.time():
                self.revoke(token)
                return None
        user_id = str(session.get("user_id") or "")
        if not user_id:
            return None
        data = store.load_registry()
        try:
            _idx, user = _find_user(data, user_id)
        except HTTPException:
            self.revoke(token)
            return None
        if user.get("status", "active") != "active":
            self.revoke(token)
            return None
        renewed = False
        with self._lock:
            if self._cache.get(token) is not session:
                return None  # A concurrent logout must not resurrect it.
            now = time.time()
            last_renewed = float(session.get("expires_at", 0)) - self._ttl
            if now - last_renewed >= self._renew_interval:
                session["expires_at"] = now + self._ttl
                if store.session(token, value=session, renew=True) is None:
                    return None
                renewed = True
        resolved = _public_user(user, self._config)
        if renewed:
            resolved["_renewed"] = True
        return resolved

    def users_empty(self) -> bool:
        """True when no console users exist yet (bootstrap gate)."""
        data = self._store().load_registry()
        return not bool(data.get("users"))
