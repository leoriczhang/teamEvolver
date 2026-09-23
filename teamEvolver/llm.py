"""
Async LLM client — thin wrapper around the ``openai`` SDK.
"""

from __future__ import annotations

import asyncio
import contextvars
import os
import re
import threading
import weakref
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from typing import Any

from .observability import (
    langfuse_observation,
    update_langfuse_observation,
)

_CCR_MODEL_OVERRIDE = "deepseek-v4-flash-ga-260731"


def _env_int(name: str, default: int, *, minimum: int, maximum: int) -> int:
    try:
        value = int(os.environ.get(name, str(default)) or default)
    except (TypeError, ValueError):
        value = default
    return max(minimum, min(maximum, value))


_DEFAULT_LLM_CONCURRENCY = _env_int(
    "TEAMEVOLVER_LLM_CONCURRENCY",
    8,
    minimum=1,
    maximum=64,
)
_DEFAULT_LLM_QUEUE_CAPACITY = _env_int(
    "TEAMEVOLVER_LLM_QUEUE_CAPACITY",
    64,
    minimum=1,
    maximum=10000,
)


class LLMOverloadedError(RuntimeError):
    pass


class _TenantLLMDispatcher:
    """One bounded execution queue for one tenant's model API."""

    def __init__(self, tenant_id: str, concurrency: int, queue_capacity: int) -> None:
        self.tenant_id = tenant_id
        self.concurrency = max(1, min(64, int(concurrency or 1)))
        self.queue_capacity = max(
            self.concurrency,
            min(10000, int(queue_capacity or self.concurrency)),
        )
        label = re.sub(r"[^a-zA-Z0-9_-]+", "-", tenant_id).strip("-")[:24]
        self._executor = ThreadPoolExecutor(
            max_workers=self.concurrency,
            thread_name_prefix=f"te-llm-{label or 'default'}",
        )
        self._pending = threading.BoundedSemaphore(self.queue_capacity)
        self._loop_slots: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()
        self._slots_lock = threading.Lock()

    async def acquire(self) -> asyncio.Semaphore:
        if not self._pending.acquire(blocking=False):
            raise LLMOverloadedError(
                f"LLM queue full for tenant {self.tenant_id!r}; retry later"
            )
        loop = asyncio.get_running_loop()
        with self._slots_lock:
            slots = self._loop_slots.setdefault(
                loop,
                asyncio.Semaphore(self.concurrency),
            )
        try:
            await slots.acquire()
        except BaseException:
            self._pending.release()
            raise
        return slots

    def release(self, slots: asyncio.Semaphore) -> None:
        slots.release()
        self._pending.release()

    async def call(self, func, **kwargs):
        slots = await self.acquire()
        loop = asyncio.get_running_loop()
        try:
            context = contextvars.copy_context()
            future = loop.run_in_executor(
                self._executor,
                context.run,
                partial(func, **kwargs),
            )
        except BaseException:
            self.release(slots)
            raise

        def finished(_):
            self.release(slots)

        future.add_done_callback(finished)
        # Cancellation cannot free a slot while the synchronous HTTP call is alive.
        return await asyncio.shield(future)

    def shutdown(self) -> None:
        self._executor.shutdown(wait=True, cancel_futures=False)


_DISPATCHERS: dict[str, _TenantLLMDispatcher] = {}
_DISPATCHERS_LOCK = threading.Lock()


def _current_tenant_id() -> str:
    try:
        from .tenants.registry import current_tenant_id

        return str(current_tenant_id() or "default")
    except Exception:  # noqa: BLE001 - standalone clients use the default queue
        return "default"


def _dispatcher_for(
    tenant_id: str,
    concurrency: int,
    queue_capacity: int,
) -> _TenantLLMDispatcher:
    scope = str(tenant_id or "default").strip() or "default"
    normalized_concurrency = max(1, min(64, int(concurrency or 1)))
    normalized_capacity = max(
        normalized_concurrency,
        min(10000, int(queue_capacity or normalized_concurrency)),
    )
    with _DISPATCHERS_LOCK:
        current = _DISPATCHERS.get(scope)
        if (
            current is not None
            and current.concurrency == normalized_concurrency
            and current.queue_capacity == normalized_capacity
        ):
            return current
        dispatcher = _TenantLLMDispatcher(
            scope,
            normalized_concurrency,
            normalized_capacity,
        )
        _DISPATCHERS[scope] = dispatcher
        return dispatcher


async def _call_in_pool(func, *, dispatcher=None, **kwargs):
    active = dispatcher or _dispatcher_for(
        _current_tenant_id(),
        _DEFAULT_LLM_CONCURRENCY,
        _DEFAULT_LLM_QUEUE_CAPACITY,
    )
    return await active.call(func, **kwargs)


def _reset_dispatchers_for_testing() -> None:
    with _DISPATCHERS_LOCK:
        dispatchers = list(_DISPATCHERS.values())
        _DISPATCHERS.clear()
    for dispatcher in dispatchers:
        dispatcher.shutdown()


def _resolve_alias_model(model: str) -> str:
    raw = str(model or "").strip()
    if raw.lower().startswith("ccr/"):
        return _CCR_MODEL_OVERRIDE
    return raw


def _normalize_temperature(model: str, requested: float) -> float:
    resolved = _resolve_alias_model(model)
    normalized = str(resolved or "").strip().lower()
    if normalized in {"kimi-k2.5"}:
        return 1
    return requested


def _usage_details(response: Any) -> dict[str, int]:
    usage = getattr(response, "usage", None)
    if usage is None:
        return {}
    values = {
        "input_tokens": getattr(usage, "prompt_tokens", None),
        "output_tokens": getattr(usage, "completion_tokens", None),
        "total_tokens": getattr(usage, "total_tokens", None),
    }
    return {
        key: int(value)
        for key, value in values.items()
        if isinstance(value, (int, float))
    }


class AsyncLLMClient:
    """OpenAI-compatible async chat client.

    All calls are dispatched to a background thread so the event loop stays
    free while the synchronous ``openai`` SDK performs the HTTP round-trip.
    """

    def __init__(
        self,
        api_key: str = "",
        base_url: str = "https://api.openai.com/v1",
        model: str = "gpt-4o",
        max_tokens: int = 100000,
        temperature: float = 0.4,
        timeout_seconds: float = 600.0,
        connect_timeout_seconds: float = 30.0,
        max_retries: int = 6,
        tenant_id: str = "",
        max_concurrency: int | None = None,
        queue_capacity: int | None = None,
    ) -> None:
        import httpx
        from openai import OpenAI

        resolved_api_key = (api_key or os.environ.get("OPENAI_API_KEY", "")).strip()
        if resolved_api_key.lower().startswith("bearer "):
            resolved_api_key = resolved_api_key[7:].strip()
        resolved_base = (base_url or os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1")).rstrip("/")
        if resolved_base.endswith("/chat/completions"):
            resolved_base = resolved_base[:-len("/chat/completions")]
        self._api_key = resolved_api_key
        self._base_url = resolved_base
        self._http_timeout = httpx.Timeout(
            max(1.0, float(timeout_seconds)),
            connect=max(1.0, float(connect_timeout_seconds)),
        )
        self._client = OpenAI(
            # Newer OpenAI SDKs require a non-empty value at construction time.
            # Keep startup/config inspection available and let the real request
            # report missing credentials when no key has been configured.
            api_key=resolved_api_key or "not-configured",
            base_url=resolved_base,
            max_retries=0,
            timeout=self._http_timeout,
        )
        self._client_cache: dict[tuple[str, str], Any] = {
            (resolved_base, resolved_api_key): self._client
        }
        self._client_cache_lock = threading.Lock()
        self.model = model or os.environ.get("EVOLVE_MODEL", "gpt-4o")
        self.max_tokens = max_tokens
        self.temperature = temperature
        self.timeout_seconds = max(1.0, float(timeout_seconds))
        self.max_retries = max(1, int(max_retries))
        self.tenant_id = str(tenant_id or _current_tenant_id())
        concurrency = (
            _DEFAULT_LLM_CONCURRENCY
            if max_concurrency is None
            else max_concurrency
        )
        capacity = (
            _DEFAULT_LLM_QUEUE_CAPACITY
            if queue_capacity is None
            else queue_capacity
        )
        self._dispatcher = _dispatcher_for(
            self.tenant_id,
            concurrency,
            capacity,
        )

    def _client_for(self, *, api_key: str, base_url: str):
        """Return a cached OpenAI-compatible client for stage-level overrides."""
        resolved_key = str(api_key or "").strip()
        if resolved_key.lower().startswith("bearer "):
            resolved_key = resolved_key[7:].strip()
        resolved_base = str(base_url or self._base_url).strip().rstrip("/")
        if resolved_base.endswith("/chat/completions"):
            resolved_base = resolved_base[:-len("/chat/completions")]
        cache_key = (resolved_base, resolved_key)
        with self._client_cache_lock:
            client = self._client_cache.get(cache_key)
            if client is not None:
                return client
            from openai import OpenAI

            client = OpenAI(
                api_key=resolved_key or "not-configured",
                base_url=resolved_base,
                max_retries=0,
                timeout=self._http_timeout,
            )
            self._client_cache[cache_key] = client
            return client

    async def chat(self, messages: list[dict[str, str]], **kwargs: Any) -> str:
        """Send a chat completion request and return the assistant content."""
        requested_api_key = str(kwargs.pop("api_key", self._api_key) or "").strip()
        requested_base_url = str(kwargs.pop("base_url", self._base_url) or "").strip()
        # Providers are currently OpenAI-wire-compatible; retain this field in
        # persisted settings for UI clarity without forwarding it upstream.
        kwargs.pop("provider", None)
        client = self._client_for(
            api_key=requested_api_key,
            base_url=requested_base_url,
        )
        if not requested_api_key:
            raise RuntimeError("LLM API key is not configured")
        trace_name = str(kwargs.pop("trace_name", "") or "").strip()
        trace_tags = [
            str(tag).strip()
            for tag in (kwargs.pop("trace_tags", None) or [])
            if str(tag).strip()
        ]
        trace_metadata = kwargs.pop("trace_metadata", None) or {}
        trace_session_id = str(kwargs.pop("trace_session_id", "") or "").strip()
        trace_user_id = str(kwargs.pop("trace_user_id", "") or "").strip()
        requested_temperature = kwargs.pop("temperature", self.temperature)
        requested_model = _resolve_alias_model(
            str(kwargs.pop("model", self.model) or self.model)
        )
        merged = {
            "model": requested_model,
            "messages": messages,
            "max_completion_tokens": kwargs.pop("max_tokens", self.max_tokens),
            "temperature": _normalize_temperature(
                requested_model,
                requested_temperature,
            ),
            **kwargs,
        }

        # Reasoning models spend the token budget on hidden reasoning before
        # emitting content. When the budget is exhausted mid-reasoning the API
        # returns finish_reason=length with empty content; doubling the budget
        # and retrying lets the model finish reasoning and emit the JSON verdict.
        budget_bumps_left = 3
        budget_ceiling = 131072
        output_cap = max(0, int(os.environ.get("TEAMEVOLVER_LLM_MAX_OUTPUT_TOKENS", "0")))
        if output_cap:
            merged["max_completion_tokens"] = min(int(merged["max_completion_tokens"]), output_cap)
            budget_ceiling = min(budget_ceiling, output_cap)

        # Parameter negotiation has its own small allowance; transport retries
        # still obey max_retries below (including max_retries=1 classifiers).
        for attempt in range(self.max_retries + 2):
            generation_input: dict[str, Any] = {"messages": messages}
            if merged.get("tools"):
                generation_input["tools"] = merged["tools"]
            observation_metadata = {
                **trace_metadata,
                "component": trace_metadata.get(
                    "component",
                    "teamEvolver.llm",
                ),
                "attempt": attempt + 1,
                "max_retries": self.max_retries,
            }
            with langfuse_observation(
                name=trace_name or "teamEvolver.llm.chat",
                as_type="generation",
                input=generation_input,
                metadata=observation_metadata,
                model=requested_model,
                model_parameters={
                    key: merged[key]
                    for key in ("temperature", "max_completion_tokens")
                    if key in merged
                },
                trace_name=trace_name or "teamEvolver.llm.chat",
                session_id=trace_session_id,
                user_id=trace_user_id,
                tags=["llm", *trace_tags],
            ) as observation:
                try:
                    resp = await _call_in_pool(
                        client.chat.completions.create,
                        dispatcher=self._dispatcher,
                        **merged,
                    )
                    choice = resp.choices[0]
                    content = choice.message.content or ""
                    finish_reason = getattr(choice, "finish_reason", None)
                    update_langfuse_observation(
                        observation,
                        output=content,
                        usage_details=_usage_details(resp),
                        metadata={
                            "finish_reason": finish_reason,
                            "response_id": getattr(resp, "id", None),
                        },
                    )
                    if (
                        not content.strip()
                        and finish_reason == "length"
                        and budget_bumps_left > 0
                    ):
                        current = int(
                            merged.get("max_completion_tokens")
                            or self.max_tokens
                        )
                        if current < budget_ceiling:
                            merged["max_completion_tokens"] = min(
                                current * 2,
                                budget_ceiling,
                            )
                            budget_bumps_left -= 1
                            continue
                    return content
                except Exception as exc:
                    if isinstance(exc, LLMOverloadedError):
                        raise
                    body_text = (
                        getattr(getattr(exc, "response", None), "text", "")
                        or ""
                    )
                    status_code = getattr(
                        getattr(exc, "response", None),
                        "status_code",
                        None,
                    )
                    if (
                        status_code == 400
                        and "max_completion_tokens" in body_text
                        and "max_completion_tokens" in merged
                    ):
                        merged["max_tokens"] = merged.pop("max_completion_tokens")
                        continue
                    if (
                        status_code == 400
                        and "'temperature' is not supported" in body_text
                    ):
                        update_langfuse_observation(
                            observation,
                            level="WARNING",
                            status_message=(
                                "provider rejected temperature; retrying "
                                "without it"
                            ),
                        )
                        merged.pop("temperature", None)
                        continue
                    if (
                        status_code == 400
                        and "Stream must be set to true" in body_text
                    ):
                        content = await self._chat_via_stream(client, merged)
                        update_langfuse_observation(
                            observation,
                            output=content,
                            metadata={"transport": "stream-fallback"},
                        )
                        return content
                    if status_code is not None and status_code not in {408, 409, 429} and status_code < 500:
                        raise
                    if attempt < self.max_retries - 1:
                        import random

                        wait = min(2**attempt + random.uniform(0, 1), 30)
                        update_langfuse_observation(
                            observation,
                            level="WARNING",
                            status_message=(
                                f"{type(exc).__name__}; retrying in "
                                f"{wait:.1f}s"
                            ),
                        )
                        await asyncio.sleep(wait)
                        continue
                    raise
        raise RuntimeError("LLM response remained invalid after retry budget")

    async def _chat_via_stream(self, client: Any, body: dict[str, Any]) -> str:
        request_body = dict(body)
        request_body["stream"] = True

        def stream():
            parts = []
            with client.chat.completions.create(**request_body) as response:
                for event in response:
                    for choice in event.choices:
                        text = choice.delta.content
                        if text:
                            parts.append(text)
            return "".join(parts)

        return await _call_in_pool(stream, dispatcher=self._dispatcher)
