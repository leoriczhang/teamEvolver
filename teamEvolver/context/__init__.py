"""Agent Context module.

The context gateway for Agent runtime: identity/permission isolation, unified
retrieval + redaction returning abstract ``context_ref``, snapshots and session
state. Keeps existing capability but drops whole-JSON read-modify-write; must
not revive the old agent registration protocol.

The API factory and repository persist incremental tenant/user rows in PG.
"""

from __future__ import annotations
