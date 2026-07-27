"""OpenAI-compatible async chat client used by optional validation workers."""

from __future__ import annotations

import asyncio
import os
from typing import Any


def _normalize_temperature(model: str, requested: float) -> float:
    normalized = str(model or "").strip().lower()
    if normalized in {"kimi-k2.5", "ccr/kimi-k2.5"}:
        return 1
    return requested


class AsyncLLMClient:
    """Thin async wrapper around the synchronous ``openai`` SDK."""

    def __init__(
        self,
        api_key: str = "",
        base_url: str = "https://ark.cn-beijing.volces.com/api/v3",
        model: str = "doubao-seed-evolving",
        max_tokens: int = 100000,
        temperature: float = 0.4,
        timeout_seconds: float = 600.0,
        connect_timeout_seconds: float = 30.0,
        max_retries: int = 6,
    ) -> None:
        import httpx
        from openai import OpenAI

        self._client = OpenAI(
            api_key=api_key or os.environ.get("OPENAI_API_KEY", ""),
            base_url=base_url or os.environ.get("OPENAI_BASE_URL", "https://ark.cn-beijing.volces.com/api/v3"),
            timeout=httpx.Timeout(float(timeout_seconds), connect=float(connect_timeout_seconds)),
        )
        self.model = model or os.environ.get("SKILLGENE_MODEL", "doubao-seed-evolving")
        self.max_tokens = max_tokens
        self.temperature = temperature
        self.max_retries = max(1, int(max_retries or 1))
        self.timeout_seconds = float(timeout_seconds)

    async def chat(self, messages: list[dict[str, str]], **kwargs: Any) -> str:
        requested_temperature = kwargs.pop("temperature", self.temperature)
        merged = {
            "model": self.model,
            "messages": messages,
            "max_completion_tokens": kwargs.pop("max_tokens", self.max_tokens),
            "temperature": _normalize_temperature(self.model, requested_temperature),
            **kwargs,
        }
        for attempt in range(self.max_retries):
            try:
                resp = await asyncio.to_thread(self._client.chat.completions.create, **merged)
                message = resp.choices[0].message
                return (getattr(message, "content", None) or getattr(message, "reasoning_content", None) or "")
            except Exception as exc:
                body_text = getattr(getattr(exc, "response", None), "text", "") or ""
                status_code = getattr(getattr(exc, "response", None), "status_code", None)
                if status_code == 400 and "'temperature' is not supported" in body_text:
                    merged.pop("temperature", None)
                    continue
                if attempt < self.max_retries - 1:
                    import random

                    await asyncio.sleep(min(2**attempt + random.uniform(0, 1), 30))
                    continue
                raise
