"""Tenant authorization and short-transaction helpers for Skill mining routes."""

from __future__ import annotations

import json
from dataclasses import replace

from starlette.exceptions import HTTPException

from teamEvolver.admin.authorization import require_tenant_admin
from teamEvolver.config_runtime import TenantConfigMissing
from teamEvolver.tenants.registry import current_tenant_id, effective_config, get_current_tenant

from .. import config
from ..repository import MiningRevisionConflict, make_repository


async def read_body(request, maximum=4 * 1024 * 1024):
    raw = bytearray()
    async for chunk in request.stream():
        raw.extend(chunk)
        if len(raw) > maximum:
            raise HTTPException(413, "挖掘请求超过大小限制")
    try:
        value = json.loads(raw) if raw else {}
    except ValueError as exc:
        raise HTTPException(400, "请求必须是有效 JSON") from exc
    if not isinstance(value, dict):
        raise HTTPException(400, "请求必须是对象")
    return value


class Context:
    def __init__(self, services):
        self.services = services

    @property
    def tenant(self):
        return current_tenant_id()

    def repo(self, tenant=None, task=None):
        return make_repository(self.services, tenant or self.tenant, task=task)

    async def settings(self, request, account=""):
        require_tenant_admin(getattr(request.state, "console_user", None))
        try:
            async with self.services.tx.tenant_tx(self.tenant) as conn:
                value = await config.load(self.services, conn)
        except (TenantConfigMissing, ValueError) as exc:
            raise HTTPException(503, str(exc)) from exc
        miner = dict(value.miner_config)
        cfg = self.services.extras.get("config")
        if cfg is not None:
            cfg = effective_config(None, get_current_tenant(), cfg)
        selected = str(miner.get("account_id") or getattr(cfg, "sharing_viking_account", ""))
        if not selected or (account and account != selected):
            raise HTTPException(403, "TENANT_ACCOUNT_MISMATCH")
        miner.update(account_id=selected, user_id=miner.get("user_id") or getattr(cfg, "sharing_viking_user", "team"))
        return replace(value, miner_config=miner)


def translate(exc):
    if isinstance(exc, MiningRevisionConflict):
        return HTTPException(409, str(exc))
    if isinstance(exc, (KeyError, FileNotFoundError)):
        return HTTPException(404, str(exc))
    if isinstance(exc, RuntimeError):
        return HTTPException(409, str(exc))
    return HTTPException(400, str(exc))
