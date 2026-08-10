"""User-facing configuration store for teamEvolver.

Reads/writes ~/.teamEvolver/config.yaml and bridges to TeamEvolverConfig.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ..config import (
    TEAM_SKILL_ROOT_PREFIX,
    VOLCENGINE_OPENVIKING_ENDPOINT,
    TeamEvolverConfig,
)
from .defaults import (
    _DEFAULT_SKILLS_DIR,
    _DEFAULTS,
    _FALLBACK_LLM_API_MODE,
    _SKILL_RELOAD_MODES,
    CONFIG_FILE,
    _coerce,
    _deep_merge,
    _first_non_empty,
    _infer_sharing_backend,
    _normalize_choice,
    _normalize_extensions,
    _normalize_non_negative_int,
    _normalize_reload_interval,
    _normalize_string_list,
    _normalize_validation_mode,
    resolve_skills_dir,
)


class ConfigStore:
    """Read/write ~/.teamEvolver/config.yaml."""

    def __init__(self, config_file: Path = CONFIG_FILE):
        self.config_file = config_file

    def exists(self) -> bool:
        return self.config_file.exists()

    def load(self) -> dict:
        if not self.config_file.exists():
            return _deep_merge({}, _DEFAULTS)
        try:
            import yaml

            with open(self.config_file, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
            merged = _deep_merge(_DEFAULTS, data)
            if "service" not in data and isinstance(data.get("proxy"), dict):
                merged["service"] = dict(merged.get("proxy") or {})
            return merged
        except Exception:
            return _deep_merge({}, _DEFAULTS)

    def save(self, data: dict):
        import yaml

        self.config_file.parent.mkdir(parents=True, exist_ok=True)
        with open(self.config_file, "w", encoding="utf-8") as f:
            yaml.dump(data, f, default_flow_style=False, allow_unicode=True)

    def get(self, dotpath: str) -> Any:
        data = self.load()
        for k in dotpath.split("."):
            if not isinstance(data, dict):
                return None
            data = data.get(k)
        return data

    def set(self, dotpath: str, value: Any):
        data = self.load()
        keys = dotpath.split(".")
        d = data
        for k in keys[:-1]:
            d = d.setdefault(k, {})
        d[keys[-1]] = _coerce(value)
        self.save(data)

    # ------------------------------------------------------------------ #
    # Bridge to TeamEvolverConfig                                            #
    # ------------------------------------------------------------------ #

    def to_config(self) -> TeamEvolverConfig:
        data = self.load()
        llm = data.get("llm", {})
        llm_provider = llm.get("provider", "openai")
        llm_api_base = llm.get("api_base", "")
        llm_api_key = llm.get("api_key", "")
        llm_model_id = llm.get("model_id", "")
        llm_api_mode = str(llm.get("api_mode", _FALLBACK_LLM_API_MODE) or _FALLBACK_LLM_API_MODE)
        llm_max_tokens = int(llm.get("max_tokens", 100000) or 100000)
        llm_temperature = float(llm.get("temperature", 0.4) if llm.get("temperature") is not None else 0.4)
        proxy = data.get("proxy", {})
        service = data.get("service", {})
        skills = data.get("skills", {})
        orouter = data.get("openrouter", {})

        sharing = data.get("sharing", {})
        evolve = data.get("evolve", {})
        dreamcycle = data.get("dreamcycle", {})
        validation = data.get("validation", {})
        sharing_backend = _infer_sharing_backend(sharing)
        sharing_endpoint = _first_non_empty(sharing, "endpoint")
        sharing_local_root = _first_non_empty(sharing, "local_root")
        sharing_skill_backend = _first_non_empty(sharing, "skill_backend")
        sharing_session_backend = _first_non_empty(sharing, "session_backend")

        skills_dir = resolve_skills_dir(skills.get("dir", str(_DEFAULT_SKILLS_DIR)))

        return TeamEvolverConfig(
            _config_file=str(self.config_file),
            # LLM forwarding
            llm_provider=llm_provider,
            llm_api_base=llm_api_base,
            llm_api_key=llm_api_key,
            llm_model_id=llm_model_id,
            llm_api_mode=llm_api_mode,
            llm_max_tokens=llm_max_tokens,
            llm_temperature=llm_temperature,
            # OpenRouter
            openrouter_app_name=orouter.get("app_name", "teamEvolver"),
            openrouter_app_url=orouter.get("app_url", ""),
            openrouter_route=orouter.get("route", "fallback"),
            openrouter_fallback_models=orouter.get("fallback_models", ""),
            openrouter_data_policy=orouter.get("data_policy", ""),
            # Service
            proxy_port=service.get("port", proxy.get("port", 52010)),
            proxy_host=service.get("host", proxy.get("host", "0.0.0.0")),
            # Skills
            use_skills=bool(skills.get("enabled", True)),
            skills_dir=skills_dir,
            skills_public_root=str(skills.get("public_root", "") or ""),
            max_context_tokens=int(data.get("max_context_tokens", 240000) or 240000),
            # Model
            model_name=llm.get("model_id") or "doubao-seed-evolving",
            # Sharing
            sharing_enabled=bool(sharing.get("enabled", True)),
            sharing_backend=sharing_backend,
            sharing_endpoint=sharing_endpoint,
            sharing_local_root=sharing_local_root,
            sharing_skill_backend=sharing_skill_backend,
            sharing_session_backend=sharing_session_backend,
            sharing_viking_endpoint=str(sharing.get("viking_endpoint", "") or VOLCENGINE_OPENVIKING_ENDPOINT),
            sharing_viking_api_key=str(sharing.get("viking_api_key", "") or ""),
            sharing_viking_personal_api_key=str(
                sharing.get("viking_personal_api_key", "")
                or sharing.get("viking_user_api_key", "")
                or ""
            ),
            sharing_viking_personal_api_keys=_normalize_string_list(
                sharing.get("viking_personal_api_keys", [])
            ),
            sharing_viking_team_api_key=str(
                sharing.get("viking_team_api_key", "")
                or sharing.get("viking_resources_api_key", "")
                or ""
            ),
            sharing_viking_account=str(sharing.get("viking_account", "") or "default"),
            sharing_viking_user=str(sharing.get("viking_user", "") or "default"),
            sharing_viking_agent=str(
                sharing.get("viking_agent", "") or TEAM_SKILL_ROOT_PREFIX
            ),
            sharing_viking_agent_id=str(
                sharing.get("viking_agent_id", "") or sharing.get("viking_user_id", "") or ""
            ),
            sharing_viking_customer_id=str(
                sharing.get("viking_customer_id", "") or sharing.get("viking_peer_id", "") or ""
            ),
            sharing_viking_root_prefix=str(
                sharing.get("viking_root_prefix", "")
                or sharing.get("root_prefix", "")
                or TEAM_SKILL_ROOT_PREFIX
            ),
            sharing_viking_group_id=str(
                sharing.get("viking_group_id", "") or sharing.get("group_id", "") or ""
            ),
            sharing_user_alias=str(sharing.get("user_alias", "") or ""),
            sharing_auto_pull_on_start=bool(sharing.get("auto_pull_on_start", True)),
            sharing_push_min_injections=int(sharing.get("push_min_injections", 5)),
            sharing_push_min_effectiveness=float(sharing.get("push_min_effectiveness", 0.3)),
            sharing_session_upload_interval=_normalize_non_negative_int(
                sharing.get("session_upload_interval", 0),
                default=0,
            ),
            sharing_skill_reload_mode=_normalize_choice(
                sharing.get("skill_reload_mode", "poll"),
                _SKILL_RELOAD_MODES,
                "poll",
            ),
            sharing_skill_reload_interval_seconds=_normalize_reload_interval(
                sharing.get("skill_reload_interval_seconds", 30),
            ),
            evolve_server_url=str(
                evolve.get("server_url", "") or "http://127.0.0.1:52010"
            ),
            evolve_evidence_enabled=bool(evolve.get("evidence_enabled", True)),
            evolve_evidence_max_entries=max(
                1, int(evolve.get("evidence_max_entries", 200))
            ),
            evolve_evidence_recent_limit=max(
                1, int(evolve.get("evidence_recent_limit", 12))
            ),
            evolve_evidence_historical_limit=max(
                0, int(evolve.get("evidence_historical_limit", 12))
            ),
            evolve_evidence_replay_cases_per_window=max(
                1, int(evolve.get("evidence_replay_cases_per_window", 1))
            ),
            evolve_evidence_change_debt_threshold=max(
                1, int(evolve.get("evidence_change_debt_threshold", 3))
            ),
            evolve_candidate_coalesce_enabled=bool(
                evolve.get("candidate_coalesce_enabled", True)
            ),
            evolve_bundle_text_extensions=_normalize_extensions(
                evolve.get("bundle_text_extensions", [".py", ".sh"])
            ),
            evolve_bundle_max_file_bytes=max(
                1, int(evolve.get("bundle_max_file_bytes", 65536) or 65536)
            ),
            evolve_bundle_max_prompt_bytes=max(
                1,
                int(evolve.get("bundle_max_prompt_bytes", 262144) or 262144),
            ),
            evolve_bundle_allow_delete=bool(
                evolve.get("bundle_allow_delete", True)
            ),
            evolve_bundle_static_checks_enabled=bool(
                evolve.get("bundle_static_checks_enabled", True)
            ),
            dreamcycle_enabled=bool(dreamcycle.get("enabled", False)),
            dreamcycle_auto_start=bool(dreamcycle.get("auto_start", False)),
            dreamcycle_daemon_command=str(
                dreamcycle.get("daemon_command", "") or "dreamcycle --daemon"
            ),
            dreamcycle_trigger_command=str(
                dreamcycle.get("trigger_command", "") or "dreamcycle --once"
            ),
            dreamcycle_viking_agent=str(
                dreamcycle.get("viking_agent", "") or "dreamcycle"
            ),
            dreamcycle_llm_base_url=str(
                dreamcycle.get("llm_base_url", "") or ""
            ),
            dreamcycle_llm_api_key=str(
                dreamcycle.get("llm_api_key", "") or ""
            ),
            dreamcycle_llm_model=str(dreamcycle.get("llm_model", "") or ""),
            validation_enabled=bool(validation.get("enabled", True)),
            validation_mode=_normalize_validation_mode(validation.get("mode", "true_replay")),
            validation_idle_after_seconds=int(validation.get("idle_after_seconds", 300)),
            validation_poll_interval_seconds=int(validation.get("poll_interval_seconds", 60)),
            validation_max_jobs_per_day=int(validation.get("max_jobs_per_day", 5)),
            validation_max_concurrency=max(1, int(validation.get("max_concurrency", 1))),
            validation_required_results=max(
                1, int(validation.get("required_results", 3))
            ),
            validation_required_approvals=max(
                1, int(validation.get("required_approvals", 2))
            ),
            validation_agentshub_url=str(validation.get("agentshub_url", "") or ""),
            validation_agentshub_api_key=str(
                validation.get("agentshub_api_key", "") or ""
            ),
        )

    def describe(self) -> str:
        """Return a human-readable summary of the current config."""
        data = self.load()
        llm = data.get("llm", {})
        skills = data.get("skills", {})
        evolve = data.get("evolve", {})
        dreamcycle = data.get("dreamcycle", {})
        effective_skills_dir = resolve_skills_dir(skills.get("dir", str(_DEFAULT_SKILLS_DIR)))
        lines = [
            f"llm.provider:    {llm.get('provider', '?')}",
            f"llm.model_id:    {llm.get('model_id', '?')}",
            f"llm.api_base:    {llm.get('api_base', '—')}",
            *(
                [
                    f"openrouter.route:    {data.get('openrouter', {}).get('route', 'fallback')}",
                    f"openrouter.fallback: {data.get('openrouter', {}).get('fallback_models', '') or '(none)'}",
                    f"openrouter.data:     {data.get('openrouter', {}).get('data_policy', '') or 'allow'}",
                ]
                if llm.get("provider") == "openrouter"
                else []
            ),
            f"service.port:    {data.get('service', {}).get('port', data.get('proxy', {}).get('port', 52010))}",
            f"skills.enabled:  {skills.get('enabled', True)}",
            f"skills.dir:      {effective_skills_dir}",
        ]
        sharing = data.get("sharing", {})
        validation = data.get("validation", {})
        if sharing.get("enabled"):
            backend = _infer_sharing_backend(sharing) or "unknown"
            skill_backend = str(sharing.get("skill_backend", "") or "").strip().lower()
            lines += [
                "sharing.enabled: True",
                f"sharing.backend: {backend}",
            ]
            if skill_backend:
                lines.append(f"sharing.skill_backend: {skill_backend}")
            if backend == "local":
                lines += [
                    f"sharing.local_root: {sharing.get('local_root', '?')}",
                ]
            elif backend == "viking":
                personal_key = (
                    sharing.get("viking_personal_api_key")
                    or sharing.get("viking_user_api_key")
                    or sharing.get("viking_api_key")
                    or ""
                )
                team_key = (
                    sharing.get("viking_team_api_key")
                    or sharing.get("viking_resources_api_key")
                    or sharing.get("viking_api_key")
                    or ""
                )
                lines += [
                    f"sharing.viking_endpoint: {sharing.get('viking_endpoint', '') or VOLCENGINE_OPENVIKING_ENDPOINT}",
                    "sharing.viking_root_prefix: "
                    f"{sharing.get('viking_root_prefix', '') or TEAM_SKILL_ROOT_PREFIX}",
                    f"sharing.viking_personal_api_key: {'present' if personal_key else 'missing'}",
                    f"sharing.viking_team_api_key: {'present' if team_key else 'missing'}",
                ]
            lines += [
                f"sharing.agent_id:    {sharing.get('viking_agent_id', '') or '(default)'}",
                f"sharing.customer_id: {sharing.get('viking_customer_id', '') or '(none)'}",
                f"sharing.alias:   {sharing.get('user_alias', '?')}",
                f"sharing.auto_pull: {sharing.get('auto_pull_on_start', False)}",
                "sharing.session_upload_interval: "
                f"{_normalize_non_negative_int(sharing.get('session_upload_interval', 0), default=0)}",
                "sharing.skill_reload_mode: "
                f"{_normalize_choice(sharing.get('skill_reload_mode', 'poll'), _SKILL_RELOAD_MODES, 'poll')}",
                "sharing.skill_reload_interval: "
                f"{_normalize_reload_interval(sharing.get('skill_reload_interval_seconds', 30))}",
            ]
        else:
            lines.append("sharing.enabled: False")
        lines += [
            f"evolve.server_url: {evolve.get('server_url', '') or 'http://127.0.0.1:52010'}",
            f"evolve.evidence_enabled: {evolve.get('evidence_enabled', True)}",
            f"evolve.evidence_windows: recent={evolve.get('evidence_recent_limit', 12)}, "
            f"historical={evolve.get('evidence_historical_limit', 12)}",
            f"evolve.change_debt_threshold: {evolve.get('evidence_change_debt_threshold', 3)}",
            f"evolve.candidate_coalesce: {evolve.get('candidate_coalesce_enabled', True)}",
            "evolve.bundle_text_extensions: "
            f"{','.join(_normalize_extensions(evolve.get('bundle_text_extensions', ['.py', '.sh'])))}",
            f"evolve.bundle_allow_delete: {evolve.get('bundle_allow_delete', True)}",
            f"evolve.bundle_static_checks: {evolve.get('bundle_static_checks_enabled', True)}",
            f"dreamcycle.enabled: {dreamcycle.get('enabled', False)}",
            f"dreamcycle.auto_start: {dreamcycle.get('auto_start', False)}",
            "dreamcycle.personal_sources: "
            f"{len(_normalize_string_list(sharing.get('viking_personal_api_keys', [])))}",
            "dreamcycle.team_target: "
            f"{'configured' if sharing.get('viking_team_api_key') else 'missing'}",
            f"validation.enabled: {validation.get('enabled', True)}",
            f"validation.mode: {_normalize_validation_mode(validation.get('mode', 'true_replay'))}",
            f"validation.idle_after: {validation.get('idle_after_seconds', 300)}",
            f"validation.poll_interval: {validation.get('poll_interval_seconds', 60)}",
            f"validation.required_results: {validation.get('required_results', 3)}",
            f"validation.required_approvals: {validation.get('required_approvals', 2)}",
            f"validation.agentshub_url: {validation.get('agentshub_url', '') or '(not set)'}",
        ]
        return "\n".join(lines)
