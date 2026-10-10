"""Persist console settings inside the selected tenant, including default."""
from __future__ import annotations

import asyncio
import copy
from dataclasses import asdict

from ..config_store import ConfigStore
from ..tenants.registry import get_current_tenant, reset_current_tenant, set_current_tenant

SECTIONS = {"evolve", "validation", "memory"}


def settings_data(data):
    result = copy.deepcopy(data)
    ctx = get_current_tenant()
    for section, value in (ctx.config_overrides.get("console_settings", {}) if ctx else {}).items():
        if section in SECTIONS and isinstance(value, dict):
            result.setdefault(section, {}).update(value)
    return result


async def save_settings(owner, store, data):
    """Return True when saved as tenant overrides, False for standalone mode."""
    from ..proxy.tenant_routes import get_tenant_registry

    ctx = get_current_tenant()
    registry = get_tenant_registry(owner)
    if ctx is None or registry.mode != "postgres":
        return False

    def convert(value):
        # Reuse the authoritative YAML-to-config mapping without writing a
        # temporary config or merging this request's old overrides back in.
        bridge = object.__new__(ConfigStore)
        bridge.config_file = store.config_file
        bridge.load = lambda: copy.deepcopy(value)
        token = set_current_tenant(None)
        try:
            return asdict(bridge.to_config())
        finally:
            reset_current_tenant(token)

    before = await asyncio.to_thread(convert, store.load())
    after = await asyncio.to_thread(convert, data)
    overrides = {key: value for key, value in after.items() if value != before.get(key)}
    overrides["console_settings"] = {key: data[key] for key in SECTIONS if key in data}
    await asyncio.to_thread(registry.update_tenant_config, ctx.tenant_id, overrides)
    pool = owner._get_engine_pool() if hasattr(owner, "_get_engine_pool") else None
    if pool is not None:
        await asyncio.to_thread(pool.drop, ctx.tenant_id, reason="tenant settings updated")
    apps = getattr(owner, "_embedded_evolve_apps", None)
    if isinstance(apps, dict):
        apps.pop(ctx.tenant_id, None)
    return True
