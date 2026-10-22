"""Adapt the Skill compiler protocol to the single platform OpenViking client."""

from __future__ import annotations

import asyncio
import inspect
import io
import re
import zipfile

from starlette.exceptions import HTTPException

from teamEvolver.openviking import OpenVikingClient
from teamEvolver.openviking.settings import effective as openviking_config
from teamEvolver.tenants.registry import current_tenant_id

from .skill_package import (  # noqa: F401
    _skill_content_signature,
    _skill_package_files,
    _skill_package_matches,
)


def settings_parts(settings):
    if hasattr(settings, "miner_config"):
        return settings.miner_config, settings.config
    return settings.get("miner", settings.get("miner_config", {})), settings.get("settings", settings.get("config", {}))


async def create_client(services, settings):
    miner, _ = settings_parts(settings)
    factory = (services.extras.get("skill_mining_client_factory")
               or services.extras.get("miner_client_factory")
               or services.extras.get("compile_client_factory"))
    if factory:
        client = factory(miner)
        return await client if inspect.isawaitable(client) else client
    cfg = await openviking_config(services, current_tenant_id())
    return OpenVikingClient(endpoint=cfg.sharing_viking_endpoint, api_key=cfg.sharing_viking_api_key,
                            account_id=miner["account_id"], user=miner.get("user_id", ""),
                            deployment=cfg.sharing_viking_deployment)


async def request(client, method, path, *, body=None, params=None, key="", shared_admin=False):
    result = await client.request(method, path, json_body=body, params=params,
                                  idempotency_key=key, shared_admin=shared_admin)
    if not result.ok:
        raise HTTPException(result.status_code, result.error or f"OpenViking HTTP {result.status_code}")
    return result.data


class CompileClientCompat:
    """Keep the original domain envelope; connection/retry/auth remain platform owned."""

    def __init__(self, endpoint="", account_id="", user_id="", api_key="", agent_id="team-skill-evolver", *,
                 client=None, guard=None):
        if client is None:
            raise ValueError("Skill compiler requires an injected platform client")
        self.client = client
        self.guard = guard

    async def _guard(self):
        if self.guard is not None:
            await self.guard()

    @staticmethod
    def envelope(result):
        return {"ok": result.ok, "exit_code": 0 if result.ok else result.status_code,
                "stderr": result.error, "result": result.data}

    async def get_skill(self, *, skill_name):
        await self._guard()
        if not re.fullmatch(r"[A-Za-z0-9_-]+", skill_name):
            raise ValueError("invalid Skill name")
        result = await self.client.request("GET", f"/api/v1/skills/{skill_name}", shared_admin=True,
            params={"target_uri": "viking://agent/skills", "include_content": "true",
                    "include_files": "true", "include_integrity": "true"})
        envelope = self.envelope(result)
        if result.ok and (not isinstance(result.data, dict)
                          or result.data.get("root_uri", "").rstrip("/") != f"viking://agent/skills/{skill_name}"):
            envelope.update(ok=False, exit_code=404, stderr="Shared compiler Skill root mismatch")
        return envelope

    async def install_skill(self, *, skill_name, skill_body, parent_uri="viking://agent/skills",
                            version_message="", auxiliary_files=None):
        await self._guard()
        body = {"data": skill_body, "wait": True, "target_uri": parent_uri,
                "source_metadata": {"type": "api", "operation": "install", "version_message": version_message,
                                    "source": "inline_content"}}
        if auxiliary_files is not None:
            def archive():
                files = _skill_package_files(skill_name, skill_body, auxiliary_files)
                stream = io.BytesIO()
                with zipfile.ZipFile(stream, "w", compression=zipfile.ZIP_DEFLATED) as target:
                    for path, content in sorted(files.items()):
                        target.writestr(path, content)
                return stream.getvalue()
            package = await asyncio.to_thread(archive)
            await self._guard()
            uploaded = await self.client.request("POST", "/api/v1/resources/temp_upload", shared_admin=True,
                files={"file": (skill_name + ".zip", package, "application/zip")})
            if not uploaded.ok:
                return self.envelope(uploaded)
            identifier = uploaded.data.get("temp_file_id") if isinstance(uploaded.data, dict) else None
            if not isinstance(identifier, str) or not identifier.strip():
                raise HTTPException(502, "OpenViking did not return a Skill package upload ID")
            body.pop("data")
            body["temp_file_id"] = identifier
            body["source_metadata"]["source"] = "temp_upload"
        await self._guard()
        result = await self.client.request("POST", "/api/v1/skills", json_body=body, shared_admin=True)
        return self.envelope(result)

    async def mkdir(self, uri):
        await self._guard()
        result = await self.client.request("POST", "/api/v1/fs/mkdir", json_body={"uri": uri})
        if not result.ok and result.status_code != 409 and "ALREADY_EXISTS" not in result.error.upper():
            raise HTTPException(result.status_code, result.error)
