"""Resolve the global OpenViking endpoint and single Root Key from PG."""
from __future__ import annotations

import os
from dataclasses import replace

from ..security.repository import SecretRepository
from ..security.secrets import SecretBox


async def effective(services, tenant):
    cfg = services.extras["config"]
    async with services.tx.pool.admin_conn() as conn:
        row = await services.extras["config_repo"].read_global(conn)
    raw = row.config.get("sharing", {})
    if not raw:
        return cfg
    changes = {}
    fields = {"endpoint": "sharing_viking_endpoint", "deployment": "sharing_viking_deployment",
              "account": "sharing_viking_account", "team_user": "sharing_viking_user"}
    changes.update({field: raw[key] for key, field in fields.items() if key in raw})
    if raw.get("secret_ref"):
        # The single service credential lives in the default control tenant.
        master = os.environ.get("TEAMEVOLVER_SECRET_MASTER_KEY") or os.environ.get("TEAMEVOLVER_ROOT_API_KEY")
        async with services.tx.tenant_tx("default") as conn:
            changes["sharing_viking_api_key"] = await SecretRepository(
                services.tx, SecretBox(master)).resolve(conn, secret_ref=raw["secret_ref"])
    return replace(cfg, **changes)
