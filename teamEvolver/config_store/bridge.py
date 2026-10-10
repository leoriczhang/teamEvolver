"""User-facing configuration store for teamEvolver.

Reads/writes ~/.teamEvolver/config.yaml and bridges to TeamEvolverConfig.
"""

from __future__ import annotations

import errno
import os
import tempfile
from pathlib import Path
from typing import Any

from team_memory.config import memory_config_values

from ..config import (
    DEFAULT_LLM_API_BASE,
    DEFAULT_LLM_API_MODE,
    DEFAULT_LLM_MAX_CONCURRENCY,
    DEFAULT_LLM_MAX_TOKENS,
    DEFAULT_LLM_MODEL_ID,
    DEFAULT_LLM_QUEUE_CAPACITY,
    DEFAULT_LLM_TEMPERATURE,
    TEAM_SKILL_ROOT_PREFIX,
    TeamEvolverConfig,
    resolve_viking_endpoint,
)
from .defaults import (
    _DEFAULT_SKILL_RUNTIME_CACHE_DIR,
    _DEFAULTS,
    CONFIG_DIR,
    CONFIG_FILE,
    _coerce,
    _deep_merge,
    _normalize_choice,
    _normalize_extensions,
    _normalize_string_list,
    _normalize_validation_mode,
    _normalize_viking_deployment,
    resolve_skill_runtime_cache_dir,
)

def _validate_config_shape(data: Any) -> dict:
    if not isinstance(data, dict):
        raise ValueError("configuration root must be an object")

    unknown_sections = sorted(set(data) - set(_DEFAULTS))
    if unknown_sections:
        raise ValueError(
            "unsupported configuration sections: "
            + ", ".join(unknown_sections)
        )

    errors: list[str] = []
    for section_name, value in data.items():
        defaults = _DEFAULTS.get(section_name)
        if isinstance(defaults, dict):
            if not isinstance(value, dict):
                errors.append(f"{section_name} must be an object")
                continue
            unknown = sorted(set(value) - set(defaults))
            if unknown:
                errors.append(
                    f"unsupported {section_name} fields: "
                    + ", ".join(unknown)
                )
            for child_name, child in value.items():
                child_defaults = defaults.get(child_name)
                if isinstance(child_defaults, dict):
                    if not isinstance(child, dict):
                        errors.append(
                            f"{section_name}.{child_name} must be an object"
                        )
                        continue
                    unknown_children = sorted(
                        set(child) - set(child_defaults)
                    )
                    if unknown_children:
                        errors.append(
                            f"unsupported {section_name}.{child_name} fields: "
                            + ", ".join(unknown_children)
                        )
    if errors:
        raise ValueError("; ".join(errors))
    return _deep_merge({}, data)


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

    def load(self) -> dict:
        if not self.config_file.exists():
            return _deep_merge({}, _DEFAULTS)
        try:
            import yaml

            with open(self.config_file, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
        except Exception as exc:
            raise ValueError(f"invalid configuration file: {self.config_file}") from exc
        return _deep_merge(_DEFAULTS, _validate_config_shape(data))

    def save(self, data: dict):
        import yaml

        sanitized = _validate_config_shape(data)
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
            ("TEAMEVOLVER_PG_SCHEMA", "storage_pg", "schema"),
            ("TEAMEVOLVER_LLM_API_KEY", "llm", "api_key"),
            ("TEAMEVOLVER_LLM_BASE_URL", "llm", "api_base"),
            ("TEAMEVOLVER_LLM_MODEL", "llm", "model_id"),
            ("TEAMEVOLVER_LLM_CONCURRENCY", "llm", "max_concurrency"),
            ("TEAMEVOLVER_LLM_QUEUE_CAPACITY", "llm", "queue_capacity"),
            ("TEAMEVOLVER_OV_ENDPOINT", "sharing", "viking_endpoint"),
            ("TEAMEVOLVER_OV_API_KEY", "sharing", "viking_api_key"),
            ("TEAMEVOLVER_SKILL_STORAGE_BACKEND", "sharing", "skill_backend"),
            ("TEAMEVOLVER_SKILL_STORAGE_ROOT", "sharing", "skill_local_root"),
            ("TEAMEVOLVER_LANGFUSE_TRACING_PUBLIC_KEY", "langfuse", "tracing_public_key"),
            ("TEAMEVOLVER_LANGFUSE_TRACING_SECRET_KEY", "langfuse", "tracing_secret_key"),
            ("TEAMEVOLVER_TENANT_TOKEN", "tenant", "machine_token"),
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
        llm_api_base = str(llm.get("api_base", "") or DEFAULT_LLM_API_BASE)
        llm_api_key = llm.get("api_key", "")
        llm_model_id = str(llm.get("model_id", "") or DEFAULT_LLM_MODEL_ID)
        llm_api_mode = str(
            llm.get("api_mode", DEFAULT_LLM_API_MODE)
            or DEFAULT_LLM_API_MODE
        )
        llm_max_tokens = int(
            llm.get("max_tokens", DEFAULT_LLM_MAX_TOKENS)
            or DEFAULT_LLM_MAX_TOKENS
        )
        llm_temperature = float(
            llm.get("temperature", DEFAULT_LLM_TEMPERATURE)
            if llm.get("temperature") is not None
            else DEFAULT_LLM_TEMPERATURE
        )
        llm_max_concurrency = max(
            1,
            min(
                64,
                int(
                    llm.get("max_concurrency", DEFAULT_LLM_MAX_CONCURRENCY)
                    or DEFAULT_LLM_MAX_CONCURRENCY
                ),
            ),
        )
        llm_queue_capacity = max(
            llm_max_concurrency,
            min(
                10000,
                int(
                    llm.get("queue_capacity", DEFAULT_LLM_QUEUE_CAPACITY)
                    or DEFAULT_LLM_QUEUE_CAPACITY
                ),
            ),
        )
        service = data.get("service", {})
        registration_value = os.environ.get(
            "TEAMEVOLVER_SELF_REGISTRATION_ENABLED", service.get("self_registration_enabled", True)
        )
        registration_value = str(registration_value).strip().lower()
        if registration_value not in {"true", "false", "1", "0"}:
            raise ValueError("service.self_registration_enabled must be true/false or 1/0")
        skills = data.get("skills", {})
        replay = data.get("replay", {})
        rollout = data.get("skillopt_rollout", {}) if isinstance(data.get("skillopt_rollout"), dict) else {}

        sharing = data.get("sharing", {})
        evolve = data.get("evolve", {})
        validation = data.get("validation", {})
        pg = data.get("storage_pg", {}) if isinstance(data.get("storage_pg"), dict) else {}
        tenant = data.get("tenant", {}) if isinstance(data.get("tenant"), dict) else {}
        langfuse = data.get("langfuse", {}) if isinstance(data.get("langfuse"), dict) else {}
        datasource = data.get("datasource", {}) if isinstance(data.get("datasource"), dict) else {}
        session_split = data.get("session_split", {}) if isinstance(data.get("session_split"), dict) else {}
        sharing_skill_backend = str(sharing.get("skill_backend", "") or "").strip()
        sharing_session_backend = str(
            sharing.get("session_backend", "") or ""
        ).strip()
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

        skills_runtime_cache_dir = resolve_skill_runtime_cache_dir(
            skills.get("runtime_cache_dir", str(_DEFAULT_SKILL_RUNTIME_CACHE_DIR))
        )

        config = TeamEvolverConfig(
            _config_file=str(self.config_file),
            team_display_name=team_display_name,
            # LLM forwarding
            llm_api_base=llm_api_base,
            ontology=dict(data.get("ontology") or {}),
            compile=dict(data.get("compile") or {}),
            experience_sync=dict(data.get("experience_sync") or {}),
            logging=dict(data.get("logging") or {}),
            llm_api_key=llm_api_key,
            llm_model_id=llm_model_id,
            llm_api_mode=llm_api_mode,
            llm_max_tokens=llm_max_tokens,
            llm_temperature=llm_temperature,
            llm_max_concurrency=llm_max_concurrency,
            llm_queue_capacity=llm_queue_capacity,
            # Service
            proxy_port=service.get("port", 52010),
            proxy_host=service.get("host", "0.0.0.0"),
            self_registration_enabled=registration_value in {"true", "1"},
            # Skills
            skills_runtime_cache_dir=skills_runtime_cache_dir,
            replay_adapter=str(replay.get("adapter", "") or ""),
            replay_adapters_dir=str(replay.get("adapters_dir", "") or ""),
            skillopt_rollout_enabled=bool(_coerce(rollout.get("enabled", False))),
            skillopt_rollout_endpoint=str(rollout.get("endpoint", "") or "").rstrip("/"),
            skillopt_rollout_workspace_prefix=str(
                rollout.get("workspace_prefix", "rollout-") or "rollout-"
            ),
            # Sharing
            sharing_viking_deployment=sharing_viking_deployment,
            sharing_skill_backend=sharing_skill_backend,
            sharing_session_backend=sharing_session_backend,
            sharing_local_fallback_enabled=sharing_local_fallback_enabled,
            sharing_local_root=sharing_local_root,
            sharing_skill_local_root=sharing_skill_local_root,
            sharing_skill_mirror_enabled=sharing_skill_mirror_enabled,
            sharing_skill_mirror_spool_dir=sharing_skill_mirror_spool_dir,
            sharing_viking_endpoint=sharing_viking_endpoint,
            sharing_viking_api_key=str(sharing.get("viking_api_key", "") or ""),
            sharing_viking_account=str(sharing.get("viking_account", "") or "default"),
            sharing_viking_user=str(sharing.get("viking_user", "") or "team").strip() or "team",
            sharing_viking_agent=str(
                sharing.get("viking_agent", "") or TEAM_SKILL_ROOT_PREFIX
            ),
            sharing_viking_root_prefix=str(
                sharing.get("viking_root_prefix", "") or TEAM_SKILL_ROOT_PREFIX
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
            evolve_human_review_enabled=bool(
                evolve.get("human_review_enabled", True)
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
            validation_runtimes=_normalize_string_list(
                validation.get(
                    "runtimes",
                    ["deap", "langfuse", "doris"],
                )
            ),
            validation_idle_after_seconds=int(validation.get("idle_after_seconds", 300)),
            validation_poll_interval_seconds=int(validation.get("poll_interval_seconds", 60)),
            validation_max_jobs_per_day=int(validation.get("max_jobs_per_day", 5)),
            validation_max_concurrency=max(1, int(validation.get("max_concurrency", 1))),
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
            langfuse_tracing_timeout_seconds=max(
                1, int(langfuse.get("tracing_timeout_seconds", 30) or 30)
            ),
            datasource_adapter=str(datasource.get("adapter") or ""),
            datasource_schedule=dict(datasource.get("schedule") or {}),
            datasource_adapters_dir=str(
                datasource.get("adapters_dir", "") or ""
            ),
            session_split_enabled=bool(session_split.get("enabled", True)),
        )
        from ..tenants.registry import effective_config, get_current_tenant

        return effective_config(None, get_current_tenant(), config)

    def describe(self) -> str:
        """Return a human-readable summary of the current config."""
        data = self.load()
        llm = data.get("llm", {})
        skills = data.get("skills", {})
        evolve = data.get("evolve", {})
        memory = data.get("memory", {})
        maintenance = (
            memory.get("maintenance")
            if isinstance(memory.get("maintenance"), dict)
            else {}
        )
        runtime_cache_dir = resolve_skill_runtime_cache_dir(
            skills.get("runtime_cache_dir", str(_DEFAULT_SKILL_RUNTIME_CACHE_DIR))
        )
        lines = [
            f"team.display_name: {self.to_config().team_display_name}",
            f"llm.model_id:    {llm.get('model_id', '?')}",
            f"llm.api_base:    {llm.get('api_base', '—')}",
            f"llm.concurrency: {llm.get('max_concurrency', DEFAULT_LLM_MAX_CONCURRENCY)}",
            f"llm.queue:       {llm.get('queue_capacity', DEFAULT_LLM_QUEUE_CAPACITY)}",
            f"service.port:    {data.get('service', {}).get('port', 52010)}",
            f"skills.runtime_cache_dir: {runtime_cache_dir}",
        ]
        sharing = data.get("sharing", {})
        validation = data.get("validation", {})
        pg_enabled = bool(data.get("storage_pg", {}).get("enabled", False))
        skill_backend = str(
            sharing.get("skill_backend") or ("postgres" if pg_enabled else "local")
        ).strip().lower()
        session_backend = "postgres" if pg_enabled else str(
            sharing.get("session_backend") or "local"
        ).strip().lower()
        deployment = _normalize_viking_deployment(sharing.get("viking_deployment"))
        endpoint = resolve_viking_endpoint(
            deployment, str(sharing.get("viking_endpoint", "") or "")
        )
        lines += [
            f"sharing.skill_backend: {skill_backend}",
            f"sharing.session_backend: {session_backend}",
            f"sharing.viking_deployment: {deployment}",
            f"sharing.viking_endpoint: {endpoint}",
            "sharing.viking_root_prefix: "
            f"{sharing.get('viking_root_prefix', '') or TEAM_SKILL_ROOT_PREFIX}",
            "sharing.root_api_key: "
            f"{'present' if sharing.get('viking_api_key') else 'missing'}",
        ]
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
            f"memory.maintenance.enabled: {maintenance.get('enabled', False)}",
            f"memory.maintenance.auto_start: {maintenance.get('auto_start', False)}",
            "memory.maintenance.service_target: "
            f"{'configured' if sharing.get('viking_api_key') else 'missing'}",
            f"validation.enabled: {validation.get('enabled', True)}",
            f"validation.mode: {_normalize_validation_mode(validation.get('mode', 'true_replay'))}",
            f"validation.idle_after: {validation.get('idle_after_seconds', 300)}",
            f"validation.poll_interval: {validation.get('poll_interval_seconds', 60)}",
        ]
        langfuse = data.get("langfuse", {}) if isinstance(data.get("langfuse"), dict) else {}
        lines.append(
            "langfuse.tracing_enabled: "
            f"{bool(langfuse.get('tracing_enabled', False))}"
        )
        if langfuse.get("tracing_enabled"):
            lines += [
                f"langfuse.tracing_host: {str(langfuse.get('tracing_host', '') or '(not set)').rstrip('/')}",
                "langfuse.tracing_public_key: "
                f"{'present' if langfuse.get('tracing_public_key') else 'missing'}",
                "langfuse.tracing_secret_key: "
                f"{'present' if langfuse.get('tracing_secret_key') else 'missing'}",
                "langfuse.tracing_environment: "
                f"{langfuse.get('tracing_environment', 'local') or 'local'}",
                "langfuse.tracing_sample_rate: "
                f"{langfuse.get('tracing_sample_rate', 1.0)}",
            ]
        lines.append(
            "datasource.adapter: "
            f"{str(data.get('datasource', {}).get('adapter') or '(not bound)')}"
        )
        return "\n".join(lines)
