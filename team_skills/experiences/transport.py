"""Idempotent OpenViking file projection with bounded, guarded HTTP calls."""
from __future__ import annotations

from urllib.parse import urlsplit

import httpx

from team_skills.evolution.repository import canonical, digest
from team_skills.library.repository import SkillLibraryError

ROOT = "viking://resources/agent_knowledge_workspace/input/proven_experiences"


def target_config(config):
    raw = config.get("experience_sync") or {}
    allowed = {"enabled", "endpoint", "account_id", "user_id", "secret_ref", "target_directory", "interval_seconds"}
    if not isinstance(raw, dict) or set(raw) - allowed or type(raw.get("enabled", False)) is not bool:
        raise SkillLibraryError("invalid experience_sync configuration")
    directory = raw.get("target_directory", ROOT)
    if (not isinstance(directory, str) or not directory.startswith("viking://resources/")
            or len(directory) > 1024 or len(directory.split("/")) > 32
            or any(part in {"", ".", ".."} for part in directory[len("viking://resources/"):].split("/"))
            or any(c in directory for c in "\\%?#") or any(c.isspace() or ord(c) < 32 for c in directory)):
        raise SkillLibraryError("invalid experience target directory")
    try:
        url = urlsplit(str(raw.get("endpoint") or ""))
        valid = url.scheme in {"https", "http"} and url.hostname and not (
            url.username or url.password or url.query or url.fragment)
        _ = url.port
    except ValueError:
        valid = False
    fields = {k: str(raw.get(k) or "") for k in ("endpoint", "account_id", "user_id", "secret_ref")}
    if not valid or any(not value or len(value) > 2048 or any(ord(c) < 32 for c in value)
                        for value in fields.values()):
        raise SkillLibraryError("experience target needs endpoint, account_id, user_id and secret_ref")
    return {**fields, "endpoint": fields["endpoint"].rstrip("/"), "target_directory": directory}


class SyncFailure(Exception):
    def __init__(self, code, status=None, *, retryable=False, receipt=None):
        self.code, self.status, self.retryable = code, status, retryable
        self.receipt = receipt or {}
        super().__init__(code)


async def project(*, target, token, tenant_id, event, before_send, transport=None):
    uri = (f"{target['target_directory']}/{digest(tenant_id)[:24]}/{event['subject_type']}/"
           f"{digest(event['subject_id'])}.json")
    headers = {
        "X-API-Key": token, "X-OpenViking-Account": target["account_id"],
        "X-OpenViking-User": target["user_id"], "X-OpenViking-Agent": "team-skill-evolver",
    }
    receipt = {"content_written": False, "semantic_status": "unknown", "vector_status": "unknown"}
    async with httpx.AsyncClient(timeout=30, follow_redirects=False, transport=transport) as client:
        async def request(method, path, *, body=None, params=None, suffix=""):
            await before_send()
            try:
                async with client.stream(
                    method, target["endpoint"] + path, json=body, params=params,
                    headers={**headers, "Idempotency-Key": event["event_id"] + ":" + suffix},
                ) as response:
                    data = bytearray()
                    async for chunk in response.aiter_bytes():
                        if len(data) + len(chunk) > 2 * 1024 * 1024:
                            raise SyncFailure("OV_RESPONSE_TOO_LARGE", receipt=receipt)
                        data.extend(chunk)
                    status = response.status_code
                if not 200 <= status < 300:
                    raise SyncFailure(f"OV_HTTP_{status}", status,
                                      retryable=status in {408, 429} or status >= 500, receipt=receipt)
                return bytes(data)
            except httpx.RequestError:
                raise SyncFailure("OV_NETWORK_ERROR", retryable=True, receipt=receipt) from None

        def parse(raw):
            import json
            try:
                value = json.loads(raw)
                if not isinstance(value, dict):
                    raise ValueError()
                return value
            except (ValueError, TypeError):
                raise SyncFailure("OV_INVALID_RESPONSE", retryable=True, receipt=receipt) from None

        async def matches():
            try:
                data = await request("GET", "/api/v1/content/download", params={"uri": uri})
            except SyncFailure as exc:
                if exc.status == 404:
                    return False
                raise
            return digest(parse(data)) == event["content_hash"]

        if event["attempt"] and await matches():
            receipt["content_written"] = True
            if event["semantic_status"] == event["vector_status"] == "complete":
                return {**receipt, "semantic_status": "complete", "vector_status": "complete", "http_status": 200}
        parts = uri[len("viking://resources/"):].split("/")
        for i in range(1, len(parts)):
            parent = "viking://resources/" + "/".join(parts[:i])
            try:
                await request("POST", "/api/v1/fs/mkdir", body={"uri": parent}, suffix=digest(parent))
            except SyncFailure as exc:
                if exc.status != 409:
                    raise
                stat = parse(await request("GET", "/api/v1/fs/stat", params={"uri": parent}))
                result = stat.get("result") or {}
                if stat.get("status") != "ok" or result.get("uri") != parent or result.get("isDir") is not True:
                    raise SyncFailure("OV_PATH_TYPE_CONFLICT", 409, receipt=receipt) from None
        body = {"uri": uri, "content": canonical(event["document"]), "mode": "replace", "wait": True, "timeout": 25}
        try:
            written = await request("POST", "/api/v1/content/write", body=body, suffix="replace")
        except SyncFailure as exc:
            if exc.status != 404:
                raise
            body["mode"] = "create"
            try:
                written = await request("POST", "/api/v1/content/write", body=body, suffix="create")
            except SyncFailure as race:
                if race.status != 409:
                    raise
                body["mode"] = "replace"
                written = await request("POST", "/api/v1/content/write", body=body, suffix="replace-race")
        value = parse(written)
        result = value.get("result") or {}
        if (value.get("status") != "ok" or not isinstance(result, dict)
                or result.get("uri") != uri or result.get("content_updated") is not True):
            raise SyncFailure("OV_UNCONFIRMED_WRITE", retryable=True, receipt=receipt)
        receipt["content_written"] = True
        for key in ("semantic_status", "vector_status"):
            if result.get(key) in {"complete", "failed", "queued", "skipped"}:
                receipt[key] = result[key]
        if receipt["semantic_status"] != "complete" or receipt["vector_status"] != "complete":
            raise SyncFailure("OV_INDEX_NOT_READY", retryable=True, receipt=receipt)
        if not await matches():
            raise SyncFailure("OV_CONTENT_MISMATCH", retryable=True, receipt=receipt)
        return {**receipt, "http_status": 200}
