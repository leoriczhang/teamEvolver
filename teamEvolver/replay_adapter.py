"""teamEvolver Adapter for the standalone :mod:`team_replay` module."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

from team_replay._util import stable_hash
from team_replay.artifacts import skill_treatment_members
from team_replay.host import ReplayHost, configure_host, current_host

from .config_store import ConfigStore


class TeamEvolverReplayHost(ReplayHost):
    """Resolve teamEvolver-owned state for the transport-neutral Replay core."""

    @staticmethod
    def _config():
        from .tenants.registry import effective_config, get_current_tenant

        return effective_config(None, get_current_tenant(), ConfigStore().to_config())

    def load_candidate_job(self, job_id: str) -> dict[str, Any] | None:
        from team_skills.candidates.store import ValidationStore

        config = self._config()
        return ValidationStore.from_config(config).load_job(job_id) or None

    def load_source_session(self, session_id: str) -> dict[str, Any] | None:
        from .session_store import SessionStore

        if not str(session_id or "").strip():
            return None
        config = self._config()
        return SessionStore.from_config(config).load_session(session_id)

    def list_source_sessions(self, *, limit: int) -> list[dict[str, Any]]:
        from .session_store import SessionStore

        config = self._config()
        store = SessionStore.from_config(config)
        sessions: list[dict[str, Any]] = []
        for row in store.list_conversations(limit=limit):
            session = store.load_session(str(row.get("session_id") or ""))
            if isinstance(session, dict):
                sessions.append(session)
        return sessions

    def load_context_snapshot(
        self,
        snapshot_id: str,
        source_session: Mapping[str, Any],
    ) -> dict[str, Any] | None:
        from .integrations.agent_principal import AgentPrincipal, normalize_user_id
        from .integrations.context_workspace import ContextStateStore
        from .tenants.registry import current_tenant_id
        runtime_context = (
            source_session.get("runtime_context")
            if isinstance(source_session.get("runtime_context"), Mapping)
            else {}
        )
        config = self._config()
        try:
            user_id = normalize_user_id(runtime_context.get("user_id") or runtime_context.get("team_evolver_user_id"))
        except ValueError:
            return None
        principal = AgentPrincipal(current_tenant_id(), config.sharing_viking_account, user_id)
        return ContextStateStore(config).load_snapshot(snapshot_id, principal=principal)

    def resolve_replay_factory(self, runtime_type: str, source_session: Mapping[str, Any]):
        from team_replay import adapter_runtime

        from .integrations.protocol_metrics import increment
        from .tenants.registry import current_tenant_id, get_current_tenant

        config = self._config()
        tenant = get_current_tenant()
        try:
            descriptor = adapter_runtime.describe(config, tenant)
            factory = adapter_runtime.load_factory(config, tenant, descriptor.get("revision"))
        except adapter_runtime.AdapterError:
            increment("replay_adapter_resolve_total", tenant_id=current_tenant_id(), result="unavailable")
            return None
        increment("replay_adapter_resolve_total", tenant_id=current_tenant_id(), result="resolved")
        return factory

    def checklist_judge_config(
        self,
        *,
        default_prompt: str,
        default_options: Mapping[str, Any],
    ) -> dict[str, Any]:
        try:
            from team_skills.evolution.prompt_studio import (
                effective_prompt,
                stage_call_options,
            )

            return {
                "system_prompt": effective_prompt(
                    "replay_checklist",
                    default_prompt,
                ),
                **stage_call_options("replay_checklist"),
            }
        except Exception:
            return {
                "system_prompt": default_prompt,
                **dict(default_options),
            }

    def judge_harness(self) -> dict[str, Any]:
        config = self._config()
        harness = {
            "base_url": str(config.llm_api_base or ""),
            "api_key": str(config.llm_api_key or ""),
            "model": str(config.llm_model_id or config.model_name or ""),
            "api_mode": str(config.llm_api_mode or ""),
            "max_tokens": int(config.llm_max_tokens or 100000),
        }
        from team_replay.judging import JUDGE_SYSTEM

        stage = self.checklist_judge_config(
            default_prompt=JUDGE_SYSTEM,
            default_options={"temperature": 0.0, "max_tokens": 8192},
        )
        for key in (
            "base_url",
            "api_key",
            "model",
            "temperature",
            "max_tokens",
        ):
            if stage.get(key) not in (None, ""):
                harness[key] = stage[key]
        harness["system_prompt"] = str(
            stage.get("system_prompt") or JUDGE_SYSTEM
        )
        return harness

    def validate_skill_treatment(
        self,
        skill: Mapping[str, Any],
    ) -> dict[str, Any]:
        from team_replay.artifacts import ReplayArtifactError
        from team_skills.candidates.static_checks import validate_candidate_bundle
        members = skill_treatment_members(skill)
        try:
            members = skill_treatment_members(skill)
        except ReplayArtifactError as exc:
            return {"passed": False, "errors": [str(exc)]}
        if len(members) == 1:
            return validate_candidate_bundle(members[0])
        results = [
            (str(member.get("name") or ""), validate_candidate_bundle(member))
            for member in members
        ]
        errors = [
            f"{name or '<unknown>'}: {error}"
            for name, result in results
            for error in result.get("errors") or []
        ]
        return {
            "passed": not errors and all(result.get("passed") for _, result in results),
            "errors": errors,
            "skills": [
                {"skill_id": name, "tree_sha256": result.get("tree_sha256")}
                for name, result in results
            ],
        }

    def materialize_skill_treatment(
        self,
        skill: Mapping[str, Any],
        target: Path,
    ) -> str:
        from team_skills.library.bundle import (
            bundle_tree_sha256,
            candidate_skill_bundle,
            write_skill_bundle,
        )

        members = skill_treatment_members(skill)
        hashes = {}
        for member in members:
            name = str(member.get("name") or "")
            destination = target if len(members) == 1 else target / name
            bundle = candidate_skill_bundle(member)
            write_skill_bundle(destination, bundle, clean=True)
            hashes[name] = bundle_tree_sha256(bundle)
        return (
            next(iter(hashes.values()))
            if len(hashes) == 1
            else stable_hash(hashes)
        )


def ensure_replay_host() -> ReplayHost:
    """Install the teamEvolver Adapter once and return it."""

    host = current_host(required=False)
    if host is None:
        host = TeamEvolverReplayHost()
        configure_host(host)
        _configure_dataset_synthesis_prompts()
    return host


def _configure_dataset_synthesis_prompts() -> None:
    """Let team_replay dataset synthesis reuse Skill Evolution prompt overrides."""
    try:
        from team_replay.datasets.synthesis import configure_prompt_provider
        from team_skills.evolution.prompt_studio import (
            effective_prompt,
            stage_call_options,
        )

        configure_prompt_provider(
            effective_prompt=effective_prompt,
            stage_call_options=stage_call_options,
        )
    except Exception:  # noqa: BLE001 - synthesis falls back to built-in defaults
        pass
