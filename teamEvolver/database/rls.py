"""Row-Level-Security session context for the platform database layer.

Every tenant-scoped connection carries two session GUCs:

- ``search_path`` -> the configured schema, so unqualified identifiers resolve.
- ``app.tenant_id`` -> the current tenant; RLS policies fail closed when it is
 empty or does not match the row's ``tenant_id``.

The runtime role must be non-SUPERUSER and non-BYPASSRLS ; callers
set the GUC on every check-out because pool resets can clear session SETs.
"""

from __future__ import annotations

from typing import Any

TENANT_GUC = "app.tenant_id"


async def apply_connection_context(conn: Any, *, schema: str, tenant_id: str) -> None:
    """Pin ``search_path`` and ``app.tenant_id`` on *conn* for this check-out.

    ``conn`` is any object exposing an awaitable ``execute(sql, *args)`` (an
    asyncpg connection or a faithful test fake). ``set_config(..., false)``
    makes the setting session-local (not transaction-local), matching the
    per-acquire lifetime of a pooled connection.
    """
    await conn.execute("SELECT set_config('search_path', $1, false)", schema)
    await conn.execute(f"SELECT set_config('{TENANT_GUC}', $1, false)", str(tenant_id))


async def reset_tenant_guc(conn: Any) -> None:
    """Clear ``app.tenant_id`` so a returned connection fails closed by default."""
    await conn.execute(f"SELECT set_config('{TENANT_GUC}', '', false)")
