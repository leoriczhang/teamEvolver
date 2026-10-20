"""TeamEvolver platform security layer.

Three concerns, deliberately small and import-free of business modules:

- ``agent_principal`` — tenant-bound Agent identity (tenant Key + ``user_id``);
 no Agent/user registry is consulted.
- ``secrets`` — symmetric encryption + opaque ``secret_ref`` handling so secret
 plaintext never enters PG, logs, trace, exceptions or task payloads.
- ``repository`` — persistence of secret references in ``secret_refs`` (RLS).
"""

from __future__ import annotations

from .agent_principal import AgentPrincipal, normalize_user_id, resolve_agent_principal
from .repository import SecretRepository
from .secrets import (
    SecretBox,
    SecretRef,
    mask_secret,
    new_secret_ref,
    secret_fingerprint,
)

__all__ = [
    "AgentPrincipal",
    "normalize_user_id",
    "resolve_agent_principal",
    "SecretBox",
    "SecretRef",
    "SecretRepository",
    "mask_secret",
    "new_secret_ref",
    "secret_fingerprint",
]
