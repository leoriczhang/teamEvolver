"""User-facing configuration store for teamEvolver.

Reads/writes ~/.teamEvolver/config.yaml and bridges to TeamEvolverConfig.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from ..config import (
    TEAM_SKILL_ROOT_PREFIX,
    TeamEvolverConfig,
    resolve_viking_endpoint,
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
    _normalize_viking_deployment,
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
        os.chmod(self.config_file, 0o600)

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
        team = data.get("team", {}) if isinstance(data.get("team"), dict) else {}
        team_display_name = str(
            os.environ.get("EVOLVE_TEAM_DISPLAY_NAME")
            or team.get("display_name")
            or "Team"
        ).strip() or "Team"
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
        aggregation = data.get("aggregation", {}) if isinstance(data.get("aggregation"), dict) else {}
        dreamcycle_prompts = dreamcycle.get("prompts") if isinstance(dreamcycle.get("prompts"), dict) else {}
        dreamcycle_job_prompts = (
            dreamcycle.get("job_prompts")
            if isinstance(dreamcycle.get("job_prompts"), dict)
            else {}
        )
        dreamcycle_job_settings = (
            dreamcycle.get("job_settings")
            if isinstance(dreamcycle.get("job_settings"), dict)
            else {}
        )
        langfuse = data.get("langfuse", {}) if isinstance(data.get("langfuse"), dict) else {}
        sharing_backend = _infer_sharing_backend(sharing)
        sharing_endpoint = _first_non_empty(sharing, "endpoint")
        sharing_skill_backend = _first_non_empty(sharing, "skill_backend")
        sharing_session_backend = _first_non_empty(sharing, "session_backend")
        sharing_viking_deployment = _normalize_viking_deployment(
            sharing.get("viking_deployment")
        )
        # Resolve the effective endpoint: an explicit viking_endpoint wins,
        # otherwise it derives from the cloud/local deployment choice.
        sharing_viking_endpoint = resolve_viking_endpoint(
            sharing_viking_deployment,
            str(sharing.get("viking_endpoint", "") or ""),
        )

        skills_dir = resolve_skills_dir(skills.get("dir", str(_DEFAULT_SKILLS_DIR)))

        return TeamEvolverConfig(
            _config_file=str(self.config_file),
            team_display_name=team_display_name,
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
            max_context_tokens=int(data.get("max_context_tokens", 256000) or 256000),
            # Model
            model_name=llm.get("model_id") or "doubao-seed-evolving",
            # Sharing
            sharing_enabled=bool(sharing.get("enabled", True)),
            sharing_backend=sharing_backend,
            sharing_viking_deployment=sharing_viking_deployment,
            sharing_endpoint=sharing_endpoint,
            sharing_skill_backend=sharing_skill_backend,
            sharing_session_backend=sharing_session_backend,
            sharing_viking_endpoint=sharing_viking_endpoint,
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
            sharing_viking_personal_user=str(
                sharing.get("viking_personal_user", "") or ""
            ),
            # ``default`` was the legacy bootstrap identity. Team-owned
            # memories/resources now live under the canonical ``team`` user.
            sharing_viking_user=(
                "team"
                if str(sharing.get("viking_user", "") or "team").strip()
                in {"", "default"}
                else str(sharing.get("viking_user", "")).strip()
            ),
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
            evolve_use_session_judge=bool(
                evolve.get("use_session_judge", True)
            ),
            evolve_publish_mode=_normalize_choice(
                evolve.get("publish_mode", "validated"),
                {"direct", "validated"},
                "validated",
            ),
            evolve_validation_max_rejections=max(
                1, int(evolve.get("validation_max_rejections", 1))
            ),
            evolve_human_review_enabled=bool(
                evolve.get("human_review_enabled", True)
            ),
            evolve_human_review_timeout_seconds=max(
                1, int(evolve.get("human_review_timeout_seconds", 86400))
            ),
            evolve_interval_seconds=max(
                1, int(evolve.get("interval_seconds", 600))
            ),
            evolve_evidence_enabled=bool(evolve.get("evidence_enabled", True)),
            evolve_evidence_max_entries=max(
                1, int(evolve.get("evidence_max_entries", 400))
            ),
            evolve_evidence_recent_limit=max(
                1, int(evolve.get("evidence_recent_limit", 20))
            ),
            evolve_evidence_historical_limit=max(
                0, int(evolve.get("evidence_historical_limit", 20))
            ),
            evolve_evidence_replay_cases_per_window=max(
                1, int(evolve.get("evidence_replay_cases_per_window", 1))
            ),
            evolve_evidence_change_debt_threshold=max(
                1, int(evolve.get("evidence_change_debt_threshold", 3))
            ),
            evolve_dataset_synthesis_enabled=bool(
                evolve.get("dataset_synthesis_enabled", True)
            ),
            evolve_dataset_test_cases=max(
                1, int(evolve.get("dataset_test_cases", 2))
            ),
            evolve_dataset_min_requirements=max(
                1, int(evolve.get("dataset_min_requirements", 12))
            ),
            evolve_dataset_max_requirements=max(
                1, int(evolve.get("dataset_max_requirements", 24))
            ),
            evolve_dataset_disclosure_batch_size=max(
                1, int(evolve.get("dataset_disclosure_batch_size", 4))
            ),
            evolve_candidate_coalesce_enabled=bool(
                evolve.get("candidate_coalesce_enabled", True)
            ),
            evolve_max_parallel_groups=max(
                1, int(evolve.get("max_parallel_groups", 4) or 4)
            ),
            evolve_bundle_text_extensions=_normalize_extensions(
                evolve.get("bundle_text_extensions", [".py", ".sh"])
            ),
            evolve_bundle_max_file_bytes=max(
                1, int(evolve.get("bundle_max_file_bytes", 262144) or 262144)
            ),
            evolve_bundle_max_prompt_bytes=max(
                1,
                int(evolve.get("bundle_max_prompt_bytes", 786432) or 786432),
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
            dreamcycle_llm_max_tokens=max(
                1, int(dreamcycle.get("llm_max_tokens", 4096) or 4096)
            ),
            dreamcycle_temperature=max(
                0.0,
                min(
                    2.0,
                    float(
                        dreamcycle.get("temperature", 0.3)
                        if dreamcycle.get("temperature") is not None
                        else 0.3
                    ),
                ),
            ),
            dreamcycle_embed_model=str(
                dreamcycle.get("embed_model", "") or ""
            ),
            dreamcycle_embed_base_url=str(
                dreamcycle.get("embed_base_url", "") or ""
            ),
            dreamcycle_embed_api_key=str(
                dreamcycle.get("embed_api_key", "") or ""
            ),
            dreamcycle_dedup_merge_threshold=max(
                -1.0,
                min(
                    1.0,
                    float(
                        dreamcycle.get("dedup_merge_threshold", 0.86)
                        if dreamcycle.get("dedup_merge_threshold") is not None
                        else 0.86
                    ),
                ),
            ),
            dreamcycle_dedup_warn_threshold=max(
                -1.0,
                min(
                    1.0,
                    float(
                        dreamcycle.get("dedup_warn_threshold", 0.72)
                        if dreamcycle.get("dedup_warn_threshold") is not None
                        else 0.72
                    ),
                ),
            ),
            dreamcycle_active_start_hour=max(
                0,
                min(
                    23,
                    int(dreamcycle.get("active_start_hour", 0) or 0),
                ),
            ),
            dreamcycle_active_end_hour=max(
                0,
                min(
                    23,
                    int(
                        dreamcycle.get("active_end_hour", 6)
                        if dreamcycle.get("active_end_hour") is not None
                        else 6
                    ),
                ),
            ),
            dreamcycle_rounds_per_window=max(
                1,
                int(dreamcycle.get("rounds_per_window", 3) or 3),
            ),
            dreamcycle_round_interval_minutes=max(
                1,
                int(
                    dreamcycle.get("round_interval_minutes", 90)
                    or 90
                ),
            ),
            dreamcycle_max_turns_per_job=max(
                1,
                int(dreamcycle.get("max_turns_per_job", 25) or 25),
            ),
            dreamcycle_max_consecutive_errors=max(
                1,
                int(
                    dreamcycle.get("max_consecutive_errors", 3)
                    or 3
                ),
            ),
            dreamcycle_retry_delay_seconds=max(
                1,
                int(dreamcycle.get("retry_delay_seconds", 300) or 300),
            ),
            dreamcycle_customer_id=str(
                dreamcycle.get("customer_id")
                or dreamcycle.get("peer_id")
                or ""
            ),
            dreamcycle_state_dir=str(
                dreamcycle.get("state_dir", "") or ""
            ),
            dreamcycle_log_level=str(
                dreamcycle.get("log_level", "") or "INFO"
            ),
            dreamcycle_enabled_jobs=(
                _normalize_string_list(dreamcycle.get("enabled_jobs"))
                if "enabled_jobs" in dreamcycle
                else [
                    "team_overview",
                    "deduplication",
                    "cleanup",
                    "onboarding_check",
                    "consolidate",
                ]
            ),
            dreamcycle_job_prompts={
                str(key): str(value)
                for key, value in dreamcycle_job_prompts.items()
                if str(key).strip() and str(value).strip()
            },
            dreamcycle_job_settings={
                str(key): dict(value)
                for key, value in dreamcycle_job_settings.items()
                if str(key).strip() and isinstance(value, dict)
            },
            dreamcycle_interval_seconds=max(
                60, int(dreamcycle.get("interval_seconds", 86400) or 86400)
            ),
            dreamcycle_max_source_items=max(
                1, int(dreamcycle.get("max_source_items", 100) or 100)
            ),
            dreamcycle_max_source_chars=max(
                1000, int(dreamcycle.get("max_source_chars", 120000) or 120000)
            ),
            dreamcycle_extract_prompt=str(
                dreamcycle_prompts.get("extract", "")
            ),
            dreamcycle_consolidate_prompt=str(
                dreamcycle_prompts.get("consolidate", "")
            ),
            aggregation_enabled=bool(aggregation.get("enabled", False)),
            aggregation_shared_knowledge_prefix=str(
                aggregation.get("shared_knowledge_prefix", "") or "shared-knowledge"
            ),
            aggregation_okf_skill_uri=str(
                aggregation.get("okf_skill_uri", "")
                or "viking://agent/skills/team-memory-okf"
            ),
            aggregation_insight_skill_uri=str(
                aggregation.get("insight_skill_uri", "") or ""
            ),
            aggregation_root_api_key=str(aggregation.get("root_api_key", "") or ""),
            aggregation_key_seed=str(
                aggregation.get("key_seed", "") or "teamevolver-aggregation"
            ),
            aggregation_staging_dir=str(aggregation.get("staging_dir", "") or "staging"),
            aggregation_kinds=_normalize_string_list(aggregation.get("kinds")),
            aggregation_max_users_per_batch=max(
                1, int(aggregation.get("max_users_per_batch", 12) or 12)
            ),
            aggregation_phase1_concurrency=max(
                1, int(aggregation.get("phase1_concurrency", 6) or 6)
            ),
            aggregation_merge_fan_in=max(
                2, min(15, int(aggregation.get("merge_fan_in", 12) or 12))
            ),
            aggregation_compile_runtime_timeout_seconds=max(
                60, int(aggregation.get("compile_runtime_timeout_seconds", 3000) or 3000)
            ),
            aggregation_state_dir=str(aggregation.get("state_dir", "") or ""),
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
            langfuse_enabled=bool(langfuse.get("enabled", False)),
            langfuse_host=str(
                langfuse.get("host", "") or "https://cloud.langfuse.com"
            ).rstrip("/"),
            langfuse_public_key=str(langfuse.get("public_key", "") or ""),
            langfuse_secret_key=str(langfuse.get("secret_key", "") or ""),
            langfuse_tracing_enabled=bool(
                langfuse.get("tracing_enabled", False)
            ),
            langfuse_tracing_environment=str(
                langfuse.get("tracing_environment", "") or "local"
            ),
            langfuse_tracing_release=str(
                langfuse.get("tracing_release", "") or ""
            ),
            langfuse_tracing_sample_rate=max(
                0.0,
                min(
                    1.0,
                    float(langfuse.get("tracing_sample_rate", 1.0) or 0.0),
                ),
            ),
            langfuse_tracing_capture_content=bool(
                langfuse.get("tracing_capture_content", True)
            ),
            langfuse_tracing_flush_at=max(
                1, int(langfuse.get("tracing_flush_at", 1) or 1)
            ),
            langfuse_tracing_flush_interval_seconds=max(
                0.1,
                float(
                    langfuse.get("tracing_flush_interval_seconds", 1.0)
                    or 1.0
                ),
            ),
            langfuse_timeout_seconds=max(
                1, int(langfuse.get("timeout_seconds", 30) or 30)
            ),
            langfuse_page_limit=max(
                1, min(100, int(langfuse.get("page_limit", 50) or 50))
            ),
            langfuse_max_sessions=max(
                1, int(langfuse.get("max_sessions", 100) or 100)
            ),
            langfuse_default_environment=_normalize_string_list(
                langfuse.get("default_environment", [])
            ),
            langfuse_default_user_id=str(langfuse.get("default_user_id", "") or ""),
            langfuse_default_tags=_normalize_string_list(
                langfuse.get("default_tags", [])
            ),
            langfuse_default_release=str(langfuse.get("default_release", "") or ""),
            langfuse_default_version=str(langfuse.get("default_version", "") or ""),
            langfuse_default_trace_name=str(
                langfuse.get("default_trace_name", "") or ""
            ),
            langfuse_mapper_enabled=bool(langfuse.get("mapper_enabled", False)),
            langfuse_mapper_code=str(langfuse.get("mapper_code", "") or ""),
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
            f"team.display_name: {self.to_config().team_display_name}",
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
            if backend == "viking":
                deployment = _normalize_viking_deployment(sharing.get("viking_deployment"))
                endpoint = resolve_viking_endpoint(
                    deployment, str(sharing.get("viking_endpoint", "") or "")
                )
                personal_key = (
                    sharing.get("viking_personal_api_key")
                    or sharing.get("viking_user_api_key")
                    or sharing.get("viking_api_key")
                    or ""
                )
                service_key = (
                    sharing.get("viking_team_api_key")
                    or sharing.get("viking_resources_api_key")
                    or sharing.get("viking_api_key")
                    or ""
                )
                lines += [
                    f"sharing.viking_deployment: {deployment}",
                    f"sharing.viking_endpoint: {endpoint}",
                    "sharing.viking_root_prefix: "
                    f"{sharing.get('viking_root_prefix', '') or TEAM_SKILL_ROOT_PREFIX}",
                    f"sharing.viking_personal_api_key: {'present' if personal_key else 'missing'}",
                    f"sharing.service_api_key: {'present' if service_key else 'missing'}",
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
            f"evolve.evidence_windows: recent={evolve.get('evidence_recent_limit', 20)}, "
            f"historical={evolve.get('evidence_historical_limit', 20)}",
            f"evolve.change_debt_threshold: {evolve.get('evidence_change_debt_threshold', 3)}",
            f"evolve.dataset_synthesis: {evolve.get('dataset_synthesis_enabled', True)}",
            f"evolve.dataset_test_cases: {evolve.get('dataset_test_cases', 2)}",
            f"evolve.candidate_coalesce: {evolve.get('candidate_coalesce_enabled', True)}",
            "evolve.bundle_text_extensions: "
            f"{','.join(_normalize_extensions(evolve.get('bundle_text_extensions', ['.py', '.sh'])))}",
            f"evolve.bundle_allow_delete: {evolve.get('bundle_allow_delete', True)}",
            f"evolve.bundle_static_checks: {evolve.get('bundle_static_checks_enabled', True)}",
            f"dreamcycle.enabled: {dreamcycle.get('enabled', False)}",
            f"dreamcycle.auto_start: {dreamcycle.get('auto_start', False)}",
            "dreamcycle.personal_sources: "
            f"{len(_normalize_string_list(sharing.get('viking_personal_api_keys', [])))}",
            "dreamcycle.service_target: "
            f"{'configured' if sharing.get('viking_team_api_key') else 'missing'}",
            f"validation.enabled: {validation.get('enabled', True)}",
            f"validation.mode: {_normalize_validation_mode(validation.get('mode', 'true_replay'))}",
            f"validation.idle_after: {validation.get('idle_after_seconds', 300)}",
            f"validation.poll_interval: {validation.get('poll_interval_seconds', 60)}",
            f"validation.required_results: {validation.get('required_results', 3)}",
            f"validation.required_approvals: {validation.get('required_approvals', 2)}",
            f"validation.agentshub_url: {validation.get('agentshub_url', '') or '(not set)'}",
        ]
        langfuse = data.get("langfuse", {}) if isinstance(data.get("langfuse"), dict) else {}
        lines.append(f"langfuse.enabled: {bool(langfuse.get('enabled', False))}")
        lines.append(
            "langfuse.tracing_enabled: "
            f"{bool(langfuse.get('tracing_enabled', False))}"
        )
        if langfuse.get("enabled") or langfuse.get("tracing_enabled"):
            lines += [
                f"langfuse.host: {str(langfuse.get('host', '') or 'https://cloud.langfuse.com').rstrip('/')}",
                f"langfuse.public_key: {'present' if langfuse.get('public_key') else 'missing'}",
                f"langfuse.secret_key: {'present' if langfuse.get('secret_key') else 'missing'}",
                "langfuse.tracing_environment: "
                f"{langfuse.get('tracing_environment', 'local') or 'local'}",
                "langfuse.tracing_sample_rate: "
                f"{langfuse.get('tracing_sample_rate', 1.0)}",
                f"langfuse.max_sessions: {langfuse.get('max_sessions', 100)}",
                "langfuse.default_environment: "
                f"{','.join(_normalize_string_list(langfuse.get('default_environment', []))) or '(any)'}",
                f"langfuse.default_user_id: {langfuse.get('default_user_id', '') or '(any)'}",
                "langfuse.default_tags: "
                f"{','.join(_normalize_string_list(langfuse.get('default_tags', []))) or '(any)'}",
            ]
        return "\n".join(lines)
