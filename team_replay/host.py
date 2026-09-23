"""Host integration seam for the standalone Replay module."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping, Protocol

from .hooks import ReplayAdapterFactory


class ReplayHostUnavailable(RuntimeError):
    """Raised when a host-owned capability has not been configured."""


class ReplayHost(Protocol):
    """Host capabilities needed by Replay without importing the host project."""

    def load_candidate_job(self, job_id: str) -> dict[str, Any] | None: ...

    def load_source_session(self, session_id: str) -> dict[str, Any] | None: ...

    def list_source_sessions(self, *, limit: int) -> list[dict[str, Any]]: ...

    def load_context_snapshot(
        self,
        snapshot_id: str,
        source_session: Mapping[str, Any],
    ) -> dict[str, Any] | None: ...

    def resolve_replay_factory(
        self, runtime_type: str, source_session: Mapping[str, Any],
    ) -> ReplayAdapterFactory | None: ...

    def checklist_judge_config(
        self,
        *,
        default_prompt: str,
        default_options: Mapping[str, Any],
    ) -> dict[str, Any]: ...

    def judge_harness(self) -> dict[str, Any]: ...

    def validate_skill_treatment(
        self,
        skill: Mapping[str, Any],
    ) -> dict[str, Any]: ...

    def materialize_skill_treatment(
        self,
        skill: Mapping[str, Any],
        target: Path,
    ) -> str: ...


_host: ReplayHost | None = None


def configure_host(host: ReplayHost | None) -> None:
    """Install the process-local host Adapter used by Replay execution."""

    global _host
    _host = host


def current_host(*, required: bool = True) -> ReplayHost | None:
    if _host is None and required:
        raise ReplayHostUnavailable(
            "Replay host is not configured; install a host Adapter before "
            "using host-owned jobs, sessions, Skill treatments, or credentials"
        )
    return _host
