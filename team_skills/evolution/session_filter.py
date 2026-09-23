"""Session value classification before entering skill evolution."""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any

from teamEvolver.llm import AsyncLLMClient
from teamEvolver.tenants.registry import current_tenant_id

logger = logging.getLogger(__name__)
_DEFAULT_CLASSIFIER_TIMEOUT_SECONDS = 60


@dataclass
class SessionValueClassifier:
    """Classify whether a session is worth entering the evolution queue."""

    client: AsyncLLMClient | None = None

    @classmethod
    def from_config(cls, config) -> "SessionValueClassifier":
        """Build the tenant-scoped LLM client used by ``analyze_session``.

        The standalone session-filter LLM stage was merged into
        ``stages/analyze.py``; this classifier no longer calls the model
        itself. ``from_config`` still constructs the client so ``analyze`` can
        reuse it. A full Prompt Studio connection override is also sufficient
        to construct the client when the tenant defaults are empty.
        """
        try:
            from team_skills.evolution.prompt_studio import stage_call_options

            stage_options = stage_call_options("analyze_session", config)
        except Exception:  # noqa: BLE001 - malformed overrides fail validation later
            stage_options = {}
        api_key = str(
            stage_options.get("api_key")
            or getattr(config, "llm_api_key", "")
            or ""
        ).strip()
        base_url = str(
            stage_options.get("base_url")
            or getattr(config, "llm_api_base", "")
            or ""
        ).strip()
        model = str(
            stage_options.get("model")
            or getattr(config, "llm_model_id", "")
            or getattr(config, "model_name", "")
            or ""
        ).strip()
        if not api_key or not base_url or not model:
            return cls(client=None)
        try:
            timeout_seconds = max(
                1.0,
                float(
                    os.environ.get(
                        "TEAMEVOLVER_SESSION_CLASSIFIER_TIMEOUT_S",
                        str(_DEFAULT_CLASSIFIER_TIMEOUT_SECONDS),
                    )
                ),
            )
        except ValueError:
            timeout_seconds = _DEFAULT_CLASSIFIER_TIMEOUT_SECONDS
        try:
            return cls(
                client=AsyncLLMClient(
                    api_key=api_key,
                    base_url=base_url,
                    model=model,
                    timeout_seconds=timeout_seconds,
                    connect_timeout_seconds=min(3.0, timeout_seconds),
                    max_retries=1,
                    tenant_id=current_tenant_id(),
                    max_concurrency=int(
                        getattr(config, "llm_max_concurrency", 8) or 8
                    ),
                    queue_capacity=int(
                        getattr(config, "llm_queue_capacity", 64) or 64
                    ),
                )
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("[SessionFilter] classifier client unavailable: %s", exc)
            return cls(client=None)

    async def classify(self, session: dict[str, Any]) -> dict[str, Any]:
        """Run mandatory merged analysis; never return a heuristic substitute."""
        from team_skills.evolution.stages.analyze import (
            SessionAnalysisError,
            analyze_session,
        )

        if self.client is None:
            raise SessionAnalysisError(
                "Session Analyze requires a configured LLM client"
            )
        return await analyze_session(self.client, session)
