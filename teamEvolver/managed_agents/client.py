"""Bounded MA transport. Redirect downloads never carry the MA credential."""

from __future__ import annotations

from collections.abc import AsyncIterator
from urllib.parse import urlsplit

import httpx

from .events import parse_sse

MAX_UPLOAD_BYTES = 30 * 1024 * 1024


class MAError(RuntimeError):
    def __init__(self, status: int, code: str):
        self.status = status
        # Upstream messages and signed URLs may contain credentials.
        self.code = code if code.replace("_", "").replace(".", "").isalnum() else "MARequestFailed"
        super().__init__(f"MA HTTP {status}: {self.code[:100]}")


def validate_url(value: str, *, allow_loopback=False) -> str:
    parsed = urlsplit(value)
    local = allow_loopback and parsed.hostname in {"localhost", "127.0.0.1", "::1"}
    if (parsed.scheme != "https" and not (local and parsed.scheme == "http")) or not parsed.hostname:
        raise ValueError("MA base URL must use HTTPS")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("MA base URL cannot contain credentials, query or fragment")
    return value.rstrip("/")


class MAClient:
    def __init__(self, base_url: str, token: str, *, transport=None, download_transport=None,
                 timeout=120, allow_loopback=False):
        self.base_url = validate_url(base_url, allow_loopback=allow_loopback)
        self.http = httpx.AsyncClient(
            base_url=self.base_url + "/", headers={"Authorization": "Bearer " + token},
            timeout=httpx.Timeout(timeout, connect=20), follow_redirects=False, transport=transport,
        )
        self.download_http = httpx.AsyncClient(
            timeout=httpx.Timeout(timeout, connect=20), follow_redirects=False,
            transport=download_transport or transport,
        )
        self.allow_loopback = allow_loopback

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        await self.close()

    async def close(self):
        await self.http.aclose()
        await self.download_http.aclose()

    @staticmethod
    def check(response):
        if response.status_code >= 300:
            try:
                code = response.json().get("error", {}).get("code", "MARequestFailed")
            except (ValueError, AttributeError):
                code = "MARequestFailed"
            raise MAError(response.status_code, str(code))

    async def request(self, method, path, **kwargs):
        # Relative URL preserves /api/v3 even when callers use leading slash.
        if "://" in path or ".." in path.split("/"):
            raise ValueError("MA request path must be relative")
        try:
            response = await self.http.request(method, path.lstrip("/"), **kwargs)
        except httpx.HTTPError as exc:
            raise MAError(502, "TransportOutcomeUnknown" if method == "POST" else "TransportFailed") from exc
        self.check(response)
        result = response.json() if response.content else {}
        # The real Files endpoint returns JSON null while no output exists.
        return {} if result is None else result

    async def page(self, path, **params):
        return await self.request("GET", path, params={"limit": 100, **params})

    async def listing(self, path, *, max_pages=100, **params):
        rows, visited = [], set()
        for _ in range(max_pages):
            result = await self.page(path, **params)
            rows.extend(result.get("data", []))
            cursor = result.get("next_page")
            if not cursor:
                return rows
            if cursor in visited:
                raise MAError(502, "PaginationLoop")
            visited.add(cursor)
            params["page"] = cursor
        raise MAError(413, "PaginationLimit")

    async def download(self, path, *, max_bytes=MAX_UPLOAD_BYTES):
        async with self.http.stream("GET", path.lstrip("/")) as response:
            if response.status_code in (301, 302, 303, 307, 308):
                location = response.headers.get("location", "")
            else:
                if response.is_error:
                    await response.aread()
                self.check(response)
                return await _bounded(response, max_bytes)
        # No Authorization/cookies from the main client. Bound each redirect.
        for _ in range(5):
            validate_url(location.split("?", 1)[0], allow_loopback=self.allow_loopback)
            async with self.download_http.stream("GET", location) as response:
                if response.status_code in (301, 302, 303, 307, 308):
                    location = str(response.url.join(response.headers.get("location", "")))
                    continue
                if response.is_error:
                    raise MAError(response.status_code, "DownloadFailed")
                return await _bounded(response, max_bytes)
        raise MAError(502, "DownloadRedirectLimit")

    async def upload_skill(self, name, archive: bytes, *, skill_id="", display_title=""):
        if len(archive) > MAX_UPLOAD_BYTES:
            raise MAError(413, "SkillUploadTooLarge")
        path = f"skills/{skill_id}/versions" if skill_id else "skills"
        return await self.request("POST", path, files={"files": (name + ".zip", archive, "application/zip")},
                                  data={"display_title": display_title or name, "description": name})

    async def history(self, session_id, thread_id=""):
        path = f"sessions/{session_id}/threads/{thread_id}/events" if thread_id else f"sessions/{session_id}/events"
        return await self.listing(path, max_pages=100, order="asc", limit=200)

    async def stream(self, session_id) -> AsyncIterator[dict]:
        async with self.http.stream("GET", f"sessions/{session_id}/events/stream",
                                    headers={"Accept": "text/event-stream"}) as response:
            if response.is_error:
                await response.aread()
                self.check(response)
            async for event in parse_sse(response.aiter_lines()):
                yield event


async def _bounded(response, max_bytes):
    length = response.headers.get("content-length")
    if length and int(length) > max_bytes:
        raise MAError(413, "DownloadTooLarge")
    data = bytearray()
    async for chunk in response.aiter_bytes():
        if len(data) + len(chunk) > max_bytes:
            raise MAError(413, "DownloadTooLarge")
        data.extend(chunk)
    return bytes(data)
