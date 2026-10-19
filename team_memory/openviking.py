"""Small OpenViking identity helpers shared by Team Memory integrations."""

from __future__ import annotations

import base64
from urllib.parse import unquote

from teamEvolver.openviking import OpenVikingClient
from teamEvolver.openviking.compile import CompileClient


def target_uri(value):
    raw = str(value or "").strip().rstrip("/")
    if raw != unquote(raw) or "\\" in raw or not raw.startswith("viking://resources/"):
        raise ValueError("Memory target must be below viking://resources")
    if any(part in {"", ".", ".."} or part.startswith(".") for part in raw[19:].split("/")):
        raise ValueError("invalid Memory target URI")
    return raw


async def clients(services, payload, *, user=None):
    factory = services.extras.get("memory_client_factory")
    if factory:
        client = factory(payload, user)
    elif services.extras.get("compile_client_factory"):
        client = await services.extras["compile_client_factory"](payload, user)
    else:
        client = OpenVikingClient(
            endpoint=services.extras["config"].sharing_viking_endpoint,
            api_key=services.extras["config"].sharing_viking_api_key,
            account_id=payload["account_id"], user=user or payload["user_id"],
        )
    return client, CompileClient(client)


async def request(client, method, path, *, body=None, params=None, key="", shared_admin=False):
    result = await client.request(method, path, json_body=body, params=params, idempotency_key=key,
                                  shared_admin=shared_admin)
    if not result.ok:
        from teamEvolver.openviking import OpenVikingError
        raise OpenVikingError(f"OpenViking HTTP {result.status_code}", status_code=result.status_code)
    return result.data


async def copy_tree(client, source, target, key, *, shared_admin=False):
    return await request(client, "POST", "/api/v1/fs/cp",
                         body={"from_uri": source, "to_uri": target, "recursive": True},
                         key=key, shared_admin=shared_admin)


async def publish_skill(client, uri, body):
    return await request(client, "POST", "/api/v1/skills",
                         body={"skill_name": uri.rsplit("/", 1)[-1], "skill_body": body})

async def snapshot_tree(client, source, target, key):
    """Copy an existing tree; an absent tree represents an empty baseline."""
    from teamEvolver.openviking import OpenVikingError
    try:
        await copy_tree(client, source, target, key)
    except OpenVikingError as exc:
        if exc.status_code != 404:
            raise
        return {"uri": "", "empty": True}
    return {"uri": target, "empty": False}


async def ensure_skill(client, compiler, stage, uri, key):
    from .aggregation.okf_skill import DEFAULT_OKF_SKILL_BODY
    from .maintenance.maintenance_skill import DEFAULT_MAINTENANCE_SKILL_BODY
    name = uri.rsplit("/", 1)[-1]
    current = await get_skill(compiler, name)
    if not current.ok and current.status_code == 404:
        default_name = "team-memory-okf" if stage == "aggregation" else "team-memory-maintenance"
        if name != default_name:
            raise ValueError("configured Memory Skill is not installed")
        body = DEFAULT_OKF_SKILL_BODY if stage == "aggregation" else DEFAULT_MAINTENANCE_SKILL_BODY
        await request(client, "POST", "/api/v1/skills",
                      body={"data": body, "wait": True, "target_uri": "viking://agent/skills"},
                      key=key, shared_admin=True)
        current = await get_skill(compiler, name)
    if not current.ok:
        from teamEvolver.openviking import OpenVikingError
        raise OpenVikingError("Memory Skill unavailable", status_code=current.status_code)
    return current


async def get_skill(compiler, name):
    return await compiler.get_skill(
        skill_name=name, params={"target_uri": "viking://agent/skills", "include_content": "true",
                                 "include_integrity": "true"}, shared_admin=True)


def parse_openviking_key(value: str) -> tuple[str, str]:
    """Return the account and user encoded in an OpenViking API key."""
    parts = str(value or "").strip().split(".")
    if len(parts) < 3:
        return "", ""
    decoded: list[str] = []
    for part in parts[:2]:
        try:
            padding = "=" * (-len(part) % 4)
            decoded.append(base64.b64decode(part + padding).decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return "", ""
    return decoded[0].strip(), decoded[1].strip()
