"""
Async LLM client — thin wrapper around the ``openai`` SDK.
"""

from __future__ import annotations

import asyncio
import os
from typing import Any

from .observability import (
    langfuse_observation,
    update_langfuse_observation,
)

_CCR_MODEL_OVERRIDE = "deepseek-v4-flash-ga-260731"


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
    ) -> None:
        import httpx
        from openai import OpenAI

        resolved_api_key = api_key or os.environ.get("OPENAI_API_KEY", "")
        self._client = OpenAI(
            # Newer OpenAI SDKs require a non-empty value at construction time.
            # Keep startup/config inspection available and let the real request
            # report missing credentials when no key has been configured.
            api_key=resolved_api_key or "not-configured",
            base_url=base_url or os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1"),
            timeout=httpx.Timeout(
                max(1.0, float(timeout_seconds)),
                connect=max(1.0, float(connect_timeout_seconds)),
            ),
        )
        self.model = model or os.environ.get("EVOLVE_MODEL", "gpt-4o")
        self.max_tokens = max_tokens
        self.temperature = temperature
        self.timeout_seconds = max(1.0, float(timeout_seconds))
        self.max_retries = max(1, int(max_retries))

    async def chat(self, messages: list[dict[str, str]], **kwargs: Any) -> str:
        """Send a chat completion request and return the assistant content."""
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

        for attempt in range(self.max_retries):
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
                    resp = await asyncio.to_thread(
                        self._client.chat.completions.create,
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
                        content = await self._chat_via_stream(merged)
                        update_langfuse_observation(
                            observation,
                            output=content,
                            metadata={"transport": "stream-fallback"},
                        )
                        return content
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

    async def _chat_via_stream(self, body: dict[str, Any]) -> str:
        import json

        import httpx

        headers: dict[str, str] = {}
        api_key = getattr(self._client, "api_key", None) or os.environ.get("OPENAI_API_KEY", "")
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"

        request_body = dict(body)
        request_body["stream"] = True
        base_url = str(getattr(self._client, "base_url", "")).rstrip("/")

        content_parts: list[str] = []
        async with httpx.AsyncClient(timeout=600.0) as client:
            async with client.stream(
                "POST",
                f"{base_url}/chat/completions",
                json=request_body,
                headers=headers,
            ) as resp:
                resp.raise_for_status()
                async for line in resp.aiter_lines():
                    if not line or not line.startswith("data:"):
                        continue
                    payload = line[5:].strip()
                    if not payload or payload == "[DONE]":
                        continue
                    try:
                        event = json.loads(payload)
                    except json.JSONDecodeError:
                        continue
                    for choice in event.get("choices", []) or []:
                        delta = choice.get("delta") or {}
                        text = delta.get("content")
                        if isinstance(text, str) and text:
                            content_parts.append(text)
        return "".join(content_parts)
