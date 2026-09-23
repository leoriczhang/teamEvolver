"""User-facing configuration store for teamEvolver.

Reads/writes ~/.teamEvolver/config.yaml and bridges to TeamEvolverConfig.
"""

from __future__ import annotations

import errno
import os
import tempfile
from pathlib import Path
from typing import Any

from session_ingestion.adapters._shared.langfuse_mapper import normalize_mapper_entries
from team_memory.config import memory_config_values

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
    CONFIG_DIR,
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
        # Resolution priority:
        # 1. Explicit ``config_file`` argument (tests, --port override)
        # 2. ``TEAMEVOLVER_CONFIG_FILE`` env var (backward compat)
        # 3. ``TEAMEVOLVER_ENV`` → ``config_{env}.yaml`` in the config directory
        # 4. Default ``CONFIG_FILE``
        if config_file != CONFIG_FILE:
            self.config_file = config_file
        elif (override := os.environ.get("TEAMEVOLVER_CONFIG_FILE", "").strip()):
            self.config_file = Path(override).expanduser()
        elif (env_name := os.environ.get("TEAMEVOLVER_ENV", "").strip()):
            env_config = CONFIG_DIR / f"config_{env_name}.yaml"
            self.config_file = env_config if env_config.exists() else CONFIG_FILE
        else:
            self.config_file = CONFIG_FILE

        # Load .env file for secrets, before any config access.
        self.env_file = None
        self.env_sources = {}
        self._load_env_file()

    def _load_env_file(self):
        """Load environment-specific ``.env`` file for secrets.

        Resolution priority:
        1. ``TEAMEVOLVER_ENV_FILE`` — explicit path
        2. ``TEAMEVOLVER_ENV`` → ``.env_{env}`` in the config file's directory
        3. ``.env`` in the config file's directory (fallback)

        Uses ``override=False`` so process-level env vars always win over
        file values; the loaded vars are then picked up by the env-var
        overrides in :meth:`to_config`.
        """
        from dotenv import dotenv_values, load_dotenv

        def load(path):
            self.env_file = path
            if path.is_file():
                self.env_sources = {k: "process_environment" if k in os.environ else f"env_file:{path}"
                                    for k in dotenv_values(path)}
                load_dotenv(path, override=False)

        env_file = os.environ.get("TEAMEVOLVER_ENV_FILE", "").strip()
        if env_file:
            path = Path(env_file).expanduser()
            load(path)
            return

        env_name = os.environ.get("TEAMEVOLVER_ENV", "").strip()
        if env_name:
            env_path = self.config_file.parent / f".env_{env_name}"
            load(env_path)
            return

        env_path = self.config_file.parent / ".env"
        load(env_path)

    def exists(self) -> bool:
        return self.config_file.exists()

    @staticmethod
    def _strip_deprecated_aggregation_credentials(data: dict) -> dict:
        aggregation = data.get("aggregation")
        if isinstance(aggregation, dict):
            aggregation.pop("root_api_key", None)
        return data

    def load(self) -> dict:
        if not self.config_file.exists():
            return _deep_merge({}, _DEFAULTS)
        try:
            import yaml

            with open(self.config_file, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
            merged = self._strip_deprecated_aggregation_credentials(
                _deep_merge(_DEFAULTS, data)
            )
            if "service" not in data and isinstance(data.get("proxy"), dict):
                merged["service"] = dict(merged.get("proxy") or {})
            return merged
        except Exception as exc:
            raise ValueError(f"invalid configuration file: {self.config_file}") from exc

    def save(self, data: dict):
        import yaml

        sanitized = self._strip_deprecated_aggregation_credentials(
            _deep_merge({}, data)
        )
        self.config_file.parent.mkdir(parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(dir=self.config_file.parent, prefix=".config-")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                yaml.dump(sanitized, f, default_flow_style=False, allow_unicode=True)
                f.flush()
                os.fsync(f.fileno())
            try:
                os.replace(name, self.config_file)
            except OSError as exc:
                if exc.errno != errno.EBUSY:
                    raise
                # Platform mounts config.yaml as a single-file bind mount; a
                # mountpoint cannot be renamed over. Rewrite it in place and
                # fsync instead (same inode, so the mount stays intact).
                with open(name, "r", encoding="utf-8") as src, open(
                    self.config_file, "w", encoding="utf-8"
                ) as dst:
                    dst.write(src.read())
                    dst.flush()
                    os.fsync(dst.fileno())
        finally:
            Path(name).unlink(missing_ok=True)

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
        for env, section, key in (
            ("TEAMEVOLVER_PG_DSN", "storage_pg", "dsn"),
            ("TEAMEVOLVER_LLM_API_KEY", "llm", "api_key"),
            ("TEAMEVOLVER_LLM_BASE_URL", "llm", "api_base"),
            ("TEAMEVOLVER_LLM_MODEL", "llm", "model_id"),
            ("TEAMEVOLVER_LLM_CONCURRENCY", "llm", "max_concurrency"),
            ("TEAMEVOLVER_LLM_QUEUE_CAPACITY", "llm", "queue_capacity"),
            ("TEAMEVOLVER_OV_ENDPOINT", "sharing", "viking_endpoint"),
            ("TEAMEVOLVER_OV_API_KEY", "sharing", "viking_api_key"),
            ("TEAMEVOLVER_OV_ROOT_KEY", "sharing", "viking_team_api_key"),
            ("TEAMEVOLVER_SKILL_STORAGE_BACKEND", "sharing", "skill_backend"),
            ("TEAMEVOLVER_SKILL_STORAGE_ROOT", "sharing", "skill_local_root"),
            ("TEAMEVOLVER_LANGFUSE_TRACING_PUBLIC_KEY", "langfuse", "tracing_public_key"),
            ("TEAMEVOLVER_LANGFUSE_TRACING_SECRET_KEY", "langfuse", "tracing_secret_key"),
            ("TEAMEVOLVER_TENANT_TOKEN", "tenant", "machine_token"),
            ("TEAMEVOLVER_AGENT_IDENTITY_MODE", "agent_protocol", "identity_mode"),
            ("TEAMEVOLVER_SKILLS_DELIVERY_MODE", "skills", "delivery_mode"),
            ("TEAMEVOLVER_REPLAY_ADAPTER", "replay", "adapter"),
            ("TEAMEVOLVER_REPLAY_ADAPTERS_DIR", "replay", "adapters_dir"),
            ("TEAMEVOLVER_VALIDATION_RUNTIMES", "validation", "runtimes"),
            ("TEAMEVOLVER_SKILLOPT_ROLLOUT_ENABLED", "skillopt_rollout", "enabled"),
            ("TEAMEVOLVER_SKILLOPT_ROLLOUT_ENDPOINT", "skillopt_rollout", "endpoint"),
            ("TEAMEVOLVER_SKILLOPT_ROLLOUT_WORKSPACE_PREFIX", "skillopt_rollout", "workspace_prefix"),
        ):
            if os.environ.get(env):
                data.setdefault(section, {})[key] = os.environ[env]
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
        llm_max_concurrency = max(
            1,
            min(64, int(llm.get("max_concurrency", 8) or 8)),
        )
        llm_queue_capacity = max(
            llm_max_concurrency,
            min(10000, int(llm.get("queue_capacity", 64) or 64)),
        )
        proxy = data.get("proxy", {})
        service = data.get("service", {})
        skills = data.get("skills", {})
        agent_protocol = data.get("agent_protocol", {})
        replay = data.get("replay", {})
        rollout = data.get("skillopt_rollout", {}) if isinstance(data.get("skillopt_rollout"), dict) else {}
        identity_mode = str(agent_protocol.get("identity_mode", "dual")).strip().lower()
        delivery_mode = str(skills.get("delivery_mode", "push")).strip().lower()
        if identity_mode not in {"dual", "tenant_user"}:
            raise ValueError("agent_protocol.identity_mode must be dual or tenant_user")
        if delivery_mode not in {"push", "pull"}:
            raise ValueError("skills.delivery_mode must be push or pull")
        orouter = data.get("openrouter", {})

        sharing = data.get("sharing", {})
        evolve = data.get("evolve", {})
        validation = data.get("validation", {})
        pg = data.get("storage_pg", {}) if isinstance(data.get("storage_pg"), dict) else {}
        tenant = data.get("tenant", {}) if isinstance(data.get("tenant"), dict) else {}
        langfuse = data.get("langfuse", {}) if isinstance(data.get("langfuse"), dict) else {}
        datasource = data.get("datasource", {}) if isinstance(data.get("datasource"), dict) else {}
        session_split = data.get("session_split", {}) if isinstance(data.get("session_split"), dict) else {}
        sharing_backend = _infer_sharing_backend(sharing)
        sharing_endpoint = _first_non_empty(sharing, "endpoint")
        sharing_skill_backend = _first_non_empty(sharing, "skill_backend")
        sharing_session_backend = _first_non_empty(sharing, "session_backend")
        if pg.get("enabled"):
            sharing_session_backend = "postgres"
            if not sharing_skill_backend:
                sharing_skill_backend = "postgres"
        sharing_local_fallback_enabled = bool(sharing.get("local_fallback_enabled", True))
        sharing_local_root = str(sharing.get("local_root", "") or "")
        sharing_skill_local_root = str(
            sharing.get("skill_local_root", "") or sharing_local_root
        )
        sharing_skill_mirror_enabled = bool(sharing.get("skill_mirror_enabled", True))
        sharing_skill_mirror_spool_dir = str(sharing.get("skill_mirror_spool_dir", "") or "")
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

        config = TeamEvolverConfig(
            _config_file=str(self.config_file),
            team_display_name=team_display_name,
            # LLM forwarding
            llm_provider=llm_provider,
            llm_api_base=llm_api_base,
            ontology=dict(data.get("ontology") or {}),
            experience_sync=dict(data.get("experience_sync") or {}),
            logging=dict(data.get("logging") or {}),
            llm_api_key=llm_api_key,
            llm_model_id=llm_model_id,
            llm_api_mode=llm_api_mode,
            llm_max_tokens=llm_max_tokens,
            llm_temperature=llm_temperature,
            llm_max_concurrency=llm_max_concurrency,
            llm_queue_capacity=llm_queue_capacity,
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
            skills_delivery_mode=delivery_mode,
            agent_protocol_identity_mode=identity_mode,
            replay_adapter=str(replay.get("adapter", "") or ""),
            replay_adapters_dir=str(replay.get("adapters_dir", "") or ""),
            skillopt_rollout_enabled=bool(_coerce(rollout.get("enabled", False))),
            skillopt_rollout_endpoint=str(rollout.get("endpoint", "") or "").rstrip("/"),
            skillopt_rollout_workspace_prefix=str(
                rollout.get("workspace_prefix", "rollout-") or "rollout-"
            ),
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
            sharing_local_fallback_enabled=sharing_local_fallback_enabled,
            sharing_local_root=sharing_local_root,
            sharing_skill_local_root=sharing_skill_local_root,
            sharing_skill_mirror_enabled=sharing_skill_mirror_enabled,
            sharing_skill_mirror_spool_dir=sharing_skill_mirror_spool_dir,
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
            storage_pg_enabled=bool(pg.get("enabled", False)),
            storage_pg_dsn=str(pg.get("dsn", "") or ""),
            storage_pg_schema=str(pg.get("schema", "") or "teamevolver"),
            storage_pg_pool_min=max(1, int(pg.get("pool_min", 2))),
            storage_pg_pool_max=max(2, int(pg.get("pool_max", 20))),
            storage_pg_command_timeout_seconds=max(
                1.0, float(pg.get("command_timeout_seconds", 30.0))
            ),
            storage_pg_ssl=str(pg.get("ssl", "prefer") or "prefer"),
            storage_pg_control_pool_max=max(
                1, int(pg.get("control_pool_max", 5))
            ),
            tenant_machine_token=str(tenant.get("machine_token", "") or "").strip(),
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
            evolve_agent_max_rounds=max(
                2, min(24, int(evolve.get("agent_max_rounds", 12) or 12))
            ),
            evolve_agent_max_tool_calls_per_round=max(
                1,
                min(16, int(evolve.get("agent_max_tool_calls_per_round", 8) or 8)),
            ),
            evolve_min_group_sessions=max(
                0, int(evolve.get("min_group_sessions", 2) or 0)
            ),
            evolve_min_group_users=max(
                0, int(evolve.get("min_group_users", 2) or 0)
            ),
            evolve_drain_max_per_cycle=max(
                0, int(evolve.get("drain_max_per_cycle", 0) or 0)
            ),
            evolve_drain_batch_size=max(
                1, int(evolve.get("drain_batch_size", 25) or 25)
            ),
            evolve_drain_batch_delay_seconds=max(
                0.0, float(evolve.get("drain_batch_delay_seconds", 1.0) or 0.0)
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
            **memory_config_values(data, _normalize_string_list),
            validation_enabled=bool(validation.get("enabled", True)),
            validation_mode=_normalize_validation_mode(validation.get("mode", "true_replay")),
            validation_runtimes=_normalize_string_list(validation.get("runtimes", ["hermes", "deap", "agentshub", "langfuse", "doris"])),
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
            langfuse_tracing_host=str(
                langfuse.get("tracing_host", "") or ""
            ).rstrip("/"),
            langfuse_tracing_public_key=str(
                langfuse.get("tracing_public_key", "") or ""
            ),
            langfuse_tracing_secret_key=str(
                langfuse.get("tracing_secret_key", "") or ""
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
            datasource_adapter=str(datasource.get("adapter") or ""),
            datasource_schedule=dict(datasource.get("schedule") or {}),
            datasource_type=str(
                datasource.get("type", "") or "langfuse"
            ),
            datasource_legacy_converter_code=str(datasource.get("legacy_converter_code", "") or ""),
            datasource_legacy_project=str(datasource.get("legacy_project", "") or ""),
            datasource_legacy_options=dict(datasource.get("legacy_options") or {}),
            datasource_adapters_dir=str(
                datasource.get("adapters_dir", "") or ""
            ),
            datasource_options=dict(datasource.get("options") or {}),
            session_split_enabled=bool(session_split.get("enabled", True)),
            langfuse_mapper_enabled=bool(langfuse.get("mapper_enabled", False)),
            langfuse_mapper_code=str(langfuse.get("mapper_code", "") or ""),
            langfuse_mappers=normalize_mapper_entries(
                langfuse.get("mappers"),
                legacy_enabled=bool(langfuse.get("mapper_enabled", False)),
                legacy_code=str(langfuse.get("mapper_code", "") or ""),
            ),
        )
        from ..tenants.registry import effective_config, get_current_tenant

        return effective_config(None, get_current_tenant(), config)

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
            f"llm.concurrency: {llm.get('max_concurrency', 8)}",
            f"llm.queue:       {llm.get('queue_capacity', 64)}",
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
                service_key = (
                    sharing.get("viking_api_key")
                    or sharing.get("viking_team_api_key")
                    or sharing.get("viking_resources_api_key")
                    or ""
                )
                lines += [
                    f"sharing.viking_deployment: {deployment}",
                    f"sharing.viking_endpoint: {endpoint}",
                    "sharing.viking_root_prefix: "
                    f"{sharing.get('viking_root_prefix', '') or TEAM_SKILL_ROOT_PREFIX}",
                    f"sharing.root_api_key: {'present' if service_key else 'missing'}",
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
        lines.append(f"datasource.type: {str(data.get('datasource', {}).get('type', 'langfuse'))}")
        return "\n".join(lines)
