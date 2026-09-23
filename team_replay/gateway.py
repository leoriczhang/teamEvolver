"""Small external Interface for optional Replay execution."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Protocol

from .contracts import (
    REPLAY_RUN_RESULT_SCHEMA_V1,
    ReplaySpec,
    skill_job_from_spec,
)


def _subject(spec: ReplaySpec) -> dict[str, Any]:
    return {
        "kind": spec.subject_kind,
        **({"id": spec.subject_id} if spec.subject_id else {}),
        **({"ids": list(spec.subject_ids)} if spec.subject_ids else {}),
    }


class ReplayGateway(Protocol):
    def evaluate(
        self,
        spec: ReplaySpec,
        *,
        case_index: int | None = None,
        timeout_seconds: int = 600,
        max_interactions: int | None = None,
    ) -> dict[str, Any]:
        """Execute one Replay specification and return an immutable result."""


@dataclass
class EmbeddedReplayGateway:
    """Adapter that runs the canonical engine in the current process."""

    evaluator: Callable[..., dict[str, Any]]

    def evaluate(
        self,
        spec: ReplaySpec,
        *,
        case_index: int | None = None,
        timeout_seconds: int = 600,
        max_interactions: int | None = None,
    ) -> dict[str, Any]:
        if spec.subject_kind not in {"skill", "skill_set"}:
            raise ValueError(
                "Embedded Skill gateway cannot materialize a Memory treatment"
            )
        interactions = max(
            1,
            int(
                max_interactions
                or spec.limits.get("max_interactions")
                or 4
            ),
        )
        result = self.evaluator(
            spec.run_id,
            job=skill_job_from_spec(spec),
            case_index=case_index,
            timeout=max(30, int(timeout_seconds or 600)),
            max_interactions=interactions,
        )
        return {
            "schema_version": REPLAY_RUN_RESULT_SCHEMA_V1,
            "run_id": spec.run_id,
            "account_id": spec.account_id,
            "subject": _subject(spec),
            "policy": {"id": spec.policy_id},
            **result,
        }


class DisabledReplayGateway:
    """Adapter used when Replay is intentionally not enabled."""

    def __init__(self, reason: str = "Replay is disabled") -> None:
        self.reason = str(reason or "Replay is disabled")

    def evaluate(
        self,
        spec: ReplaySpec,
        *,
        case_index: int | None = None,
        timeout_seconds: int = 600,
        max_interactions: int | None = None,
    ) -> dict[str, Any]:
        del case_index, timeout_seconds, max_interactions
        return {
            "schema_version": REPLAY_RUN_RESULT_SCHEMA_V1,
            "run_id": spec.run_id,
            "account_id": spec.account_id,
            "subject": _subject(spec),
            "policy": {"id": spec.policy_id},
            "status": "not_run",
            "accepted": False,
            "verdict": "inconclusive",
            "no_regression": False,
            "reason": self.reason,
            "cases": [],
        }
