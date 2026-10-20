"""Platform OpenViking HTTP client.

Centralizes everything shared across Compile and other OpenViking calls:

- auth headers using the single trusted Root Key + Account/User;
- a keep-alive ``httpx.AsyncClient`` reused across requests;
- bounded retry on transient transport errors;
- a uniform :class:`OpenVikingResult` / :class:`OpenVikingError` mapping so
 callers never parse raw ``httpx.Response`` objects.

No Memory/Wiki URI rules or success post-processing live here.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

import httpx

_MAX_OUTPUT_CHARS = 4000
_RETRY_ATTEMPTS = 3
_RETRY_BASE_SECONDS = 0.5
# Status codes worth retrying (transient); 4xx (except 408/429) are deterministic.
_RETRYABLE_STATUS = frozenset({408, 429, 500, 502, 503, 504})


@dataclass(frozen=True)
class OpenVikingResult:
    ok: bool
    status_code: int
    data: Any = None
    error: str = ""


class OpenVikingError(RuntimeError):
    """Raised for a non-retryable, failed OpenViking response."""

    def __init__(self, message: str, *, status_code: int) -> None:
        super().__init__(message)
        self.status_code = status_code


class OpenVikingClient:
    """Keep-alive OpenViking HTTP client with Root Key auth and retry."""

    def __init__(
        self,
        *,
        endpoint: str,
        api_key: str,
        account_id: str,
        user: str = "",
        agent_id: str = "team-skill-evolver",
        deployment: str = "cloud",
        timeout_seconds: float = 60.0,
    ) -> None:
        self._endpoint = str(endpoint or "").strip().rstrip("/")
        self._api_key = str(api_key or "").strip()
        self._account_id = str(account_id or "").strip()
        self._user = str(user or "").strip()
        self._agent_id = agent_id
        self._deployment = str(deployment or "cloud").strip().lower()
        self._timeout = float(timeout_seconds)
        self._client: httpx.AsyncClient | None = None

    # -- identity --------------------------------------------------------- #

    def headers(self, *, shared_admin: bool = False) -> dict[str, str]:
        """Build request headers using the single Root Key for every account."""
        if not self._api_key and self._deployment != "local":
            raise OpenVikingError("OpenViking service key is not configured", status_code=503)
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "X-OpenViking-Account": self._account_id,
        }
        if self._user:
            headers["X-OpenViking-User"] = self._user
        if self._agent_id:
            headers["X-OpenViking-Agent"] = self._agent_id
        # Trusted ("local") servers require the Root Key on each request; cloud
        # servers validate it. Always send when present — never harmful.
        if self._api_key:
            headers["X-API-Key"] = self._api_key
            headers["Authorization"] = f"Bearer {self._api_key}"
        if shared_admin:
            headers["X-OpenViking-Role"] = "admin"
        return headers

    # -- connection ------------------------------------------------------- #

    def _get_client(self) -> httpx.AsyncClient:
        client = self._client
        if client is None or client.is_closed:
            client = httpx.AsyncClient(timeout=max(30.0, self._timeout), follow_redirects=False)
            self._client = client
        return client

    async def aclose(self) -> None:
        if self._client is not None and not self._client.is_closed:
            await self._client.aclose()
        self._client = None

    # -- request ---------------------------------------------------------- #

    @staticmethod
    def _parse(response: httpx.Response) -> Any:
        try:
            payload = response.json()
        except ValueError:
            return None
        if isinstance(payload, dict) and payload.get("status") == "ok" and "result" in payload:
            return payload["result"]
        return payload

    @staticmethod
    def _error_message(response: httpx.Response, payload: Any) -> str:
        if isinstance(payload, dict):
            error = payload.get("error")
            if isinstance(error, dict) and error.get("message"):
                return str(error["message"])[:_MAX_OUTPUT_CHARS]
            detail = payload.get("detail")
            if isinstance(detail, dict):
                detail = detail.get("message") or detail.get("code")
            if detail:
                return str(detail)[:_MAX_OUTPUT_CHARS]
        return str(response.text or f"HTTP {response.status_code}")[:_MAX_OUTPUT_CHARS]

    async def request(
        self,
        method: str,
        path: str,
        *,
        json_body: Any = None,
        params: dict[str, Any] | None = None,
        idempotency_key: str = "",
        shared_admin: bool = False,
        files: dict[str, Any] | None = None,
    ) -> OpenVikingResult:
        """Issue a request and map it to :class:`OpenVikingResult`.

        Retries transient transport errors and retryable status codes with
        exponential backoff; 4xx (except 408/429) returns immediately.
        """
        url = f"{self._endpoint}{path}"
        headers = self.headers(shared_admin=shared_admin)
        if files is not None:
            if json_body is not None:
                raise ValueError("multipart requests cannot include a JSON body")
            headers.pop("Content-Type", None)
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key
        client = self._get_client()

        last_response: httpx.Response | None = None
        for attempt in range(1, _RETRY_ATTEMPTS + 1):
            try:
                response = await client.request(
                    method.upper(), url, headers=headers, json=json_body, params=params,
                    **({"files": files} if files is not None else {}),
                )
            except (httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadTimeout):
                if attempt >= _RETRY_ATTEMPTS:
                    raise
                await asyncio.sleep(_RETRY_BASE_SECONDS * (2 ** (attempt - 1)))
                continue
            last_response = response
            if response.status_code in _RETRYABLE_STATUS and attempt < _RETRY_ATTEMPTS:
                await asyncio.sleep(_RETRY_BASE_SECONDS * (2 ** (attempt - 1)))
                continue
            payload = self._parse(response)
            if response.is_success:
                return OpenVikingResult(ok=True, status_code=response.status_code, data=payload)
            return OpenVikingResult(
                ok=False,
                status_code=response.status_code,
                data=payload,
                error=self._error_message(response, payload),
            )
        # Exhausted retries on a retryable status.
        assert last_response is not None
        payload = self._parse(last_response)
        return OpenVikingResult(
            ok=False,
            status_code=last_response.status_code,
            data=payload,
            error=self._error_message(last_response, payload),
        )
