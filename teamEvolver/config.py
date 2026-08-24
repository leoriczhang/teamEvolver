"""Unified configuration for teamEvolver."""

from dataclasses import dataclass, field

VOLCENGINE_OPENVIKING_ENDPOINT = "https://api.vikingdb.cn-beijing.volces.com/openviking"
# Default endpoint for a locally self-hosted ``openviking-server`` (see
# https://docs.openviking.ai server quickstart — Uvicorn binds :1933).
LOCAL_OPENVIKING_ENDPOINT = "http://localhost:1933"
TEAM_SKILL_ROOT_PREFIX = "team-skill-evolver"


def resolve_viking_endpoint(deployment: str, endpoint: str = "") -> str:
    """Resolve the effective OpenViking endpoint for a deployment mode.

    An explicit non-empty ``endpoint`` always wins (advanced override).
    Otherwise ``local`` maps to the self-hosted server address and any other
    value (``cloud`` / empty) maps to the Volcengine-hosted endpoint.
    """
    override = str(endpoint or "").strip()
    if override:
        return override
    if str(deployment or "").strip().lower() == "local":
        return LOCAL_OPENVIKING_ENDPOINT
    return VOLCENGINE_OPENVIKING_ENDPOINT


@dataclass
class TeamEvolverConfig:
    # Internal source path used by admin routes that need to persist runtime
    # config updates back to the same file.
    _config_file: str = field(default="", repr=False)

    # ------------------------------------------------------------------ #
    # Team identity                                                       #
    # ------------------------------------------------------------------ #
    team_display_name: str = "Team"

    # ------------------------------------------------------------------ #
    # Model                                                               #
    # ------------------------------------------------------------------ #
    model_name: str = "doubao-seed-evolving"

    # ------------------------------------------------------------------ #
    # Skills                                                              #
    # ------------------------------------------------------------------ #
    use_skills: bool = True
    skills_dir: str = "memory_data/skills"
    skills_public_root: str = ""
    max_skills_prompt_chars: int = 120000

    # ------------------------------------------------------------------ #
    # Context window                                                       #
    # ------------------------------------------------------------------ #
    # Prompt budget retained for model-testing and validation clients. Default
    # assumes modern models support at least a 256k token context window.
    max_context_tokens: int = 256000

    # ------------------------------------------------------------------ #
    # API Server                                                          #
    # ------------------------------------------------------------------ #
    proxy_port: int = 52010
    proxy_host: str = "0.0.0.0"

    # ------------------------------------------------------------------ #
    # LLM forwarding                                                      #
    # ------------------------------------------------------------------ #
    llm_provider: str = "openai"
    llm_api_base: str = "https://ark.cn-beijing.volces.com/api/v3"
    llm_api_key: str = ""
    llm_model_id: str = "doubao-seed-evolving"
    llm_api_mode: str = "chat"
    llm_max_tokens: int = 100000
    llm_temperature: float = 0.4

    # ------------------------------------------------------------------ #
    # OpenRouter-specific (ignored for other providers)                    #
    # ------------------------------------------------------------------ #
    openrouter_app_name: str = "teamEvolver"
    openrouter_app_url: str = ""
    openrouter_route: str = "fallback"
    openrouter_fallback_models: str = ""
    openrouter_data_policy: str = ""

    # ------------------------------------------------------------------ #
    # Skill sharing (OpenViking object storage)                           #
    # ------------------------------------------------------------------ #
    # Sharing always uses the OpenViking (``viking``) backend. The only choice
    # is the deployment: ``cloud`` (Volcengine-hosted) or ``local`` (a
    # self-hosted ``openviking-server``). They differ only by endpoint.
    sharing_enabled: bool = True
    sharing_backend: str = "viking"
    # Deployment target for the OpenViking backend: ``cloud`` or ``local``.
    sharing_viking_deployment: str = "cloud"
    sharing_endpoint: str = ""
    # Optional override for skill assets. When empty, sharing_backend keeps its
    # legacy behavior and is used for both skills and session artifacts.
    sharing_skill_backend: str = ""
    # Optional object-storage backend for non-skill artifacts when the skill
    # backend is reserved for the Skill registry.
    sharing_session_backend: str = ""

    # OpenViking backend (sharing.backend = "viking"). When empty the endpoint
    # is derived from ``sharing_viking_deployment`` (cloud vs local); a
    # non-empty value is an explicit advanced override.
    sharing_viking_endpoint: str = ""
    # Backward-compatible fallback key. Prefer the scoped keys below when both
    # personal and team OpenViking spaces are configured.
    sharing_viking_api_key: str = ""
    sharing_viking_personal_api_key: str = ""
    sharing_viking_personal_api_keys: list[str] = field(default_factory=list)
    sharing_viking_team_api_key: str = ""
    sharing_viking_account: str = "default"
    # OpenViking user namespace used as the default personal-memory space.
    # Individual teamEvolver users may override this in their personal space.
    sharing_viking_personal_user: str = ""
    # Canonical OpenViking user for team-owned Agent context.
    sharing_viking_user: str = "team"
    # wire constant: OpenViking agent namespace shared with Hermes /
    # Default shared skill namespace
    # (viking://resources/team-skill-evolver/...).
    sharing_viking_agent: str = TEAM_SKILL_ROOT_PREFIX
    # Identity fields sent to OpenViking for attribution. Skill spaces use the
    # resources namespace; customer_id may still scope per-customer prefixes
    # such as ``peers/{customer_id}/`` inside that resources root.
    sharing_viking_agent_id: str = ""
    sharing_viking_customer_id: str = ""
    # Team-shared resources layout: objects live under
    # ``viking://resources/{viking_root_prefix}/...`` (with an optional
    # ``{viking_group_id}`` segment when set) — the same namespace Hermes'
    # OpenVikingSkillSource reads team skills from. Empty group_id (default)
    # means the team library has no group segment.
    # wire constant: root prefix is the OpenViking data contract namespace, do
    # not rename
    sharing_viking_root_prefix: str = TEAM_SKILL_ROOT_PREFIX
    sharing_viking_group_id: str = ""

    sharing_user_alias: str = ""
    sharing_auto_pull_on_start: bool = True
    sharing_push_min_injections: int = 5
    sharing_push_min_effectiveness: float = 0.3
    sharing_session_upload_interval: int = 0
    sharing_skill_reload_mode: str = "poll"
    sharing_skill_reload_interval_seconds: int = 30
    users_registry_path: str = ""

    # ------------------------------------------------------------------ #
    # Evolve server integration                                           #
    # ------------------------------------------------------------------ #
    evolve_server_url: str = "http://127.0.0.1:52010"
    evolve_use_session_judge: bool = True
    evolve_publish_mode: str = "validated"
    evolve_validation_max_rejections: int = 1
    evolve_human_review_enabled: bool = True
    evolve_human_review_timeout_seconds: int = 86400
    evolve_interval_seconds: int = 600
    evolve_evidence_enabled: bool = True
    evolve_evidence_max_entries: int = 400
    evolve_evidence_recent_limit: int = 20
    evolve_evidence_historical_limit: int = 20
    evolve_evidence_replay_cases_per_window: int = 1
    evolve_evidence_change_debt_threshold: int = 3
    evolve_dataset_synthesis_enabled: bool = True
    evolve_dataset_test_cases: int = 2
    evolve_dataset_min_requirements: int = 12
    evolve_dataset_max_requirements: int = 24
    evolve_dataset_disclosure_batch_size: int = 4
    evolve_candidate_coalesce_enabled: bool = True
    evolve_max_parallel_groups: int = 4
    evolve_bundle_text_extensions: list[str] = field(
        default_factory=lambda: [".py", ".sh"]
    )
    evolve_bundle_max_file_bytes: int = 262144
    evolve_bundle_max_prompt_bytes: int = 786432
    evolve_bundle_allow_delete: bool = True
    evolve_bundle_static_checks_enabled: bool = True

    # ------------------------------------------------------------------ #
    # DreamCycle team-memory maintenance                                  #
    # ------------------------------------------------------------------ #
    # Full DreamCycle runs natively in teamEvolver: five maintenance jobs,
    # ReAct tool calls, policy enforcement, reports, scheduling and history.
    # Legacy command/two-prompt fields remain readable for config migration.
    dreamcycle_enabled: bool = False
    dreamcycle_auto_start: bool = False
    dreamcycle_daemon_command: str = "dreamcycle --daemon"
    dreamcycle_trigger_command: str = "dreamcycle --once"
    dreamcycle_viking_agent: str = "dreamcycle"
    dreamcycle_llm_base_url: str = ""
    dreamcycle_llm_api_key: str = ""
    dreamcycle_llm_model: str = ""
    dreamcycle_llm_max_tokens: int = 4096
    dreamcycle_temperature: float = 0.3
    dreamcycle_embed_model: str = ""
    dreamcycle_embed_base_url: str = ""
    dreamcycle_embed_api_key: str = ""
    dreamcycle_dedup_merge_threshold: float = 0.86
    dreamcycle_dedup_warn_threshold: float = 0.72
    dreamcycle_active_start_hour: int = 0
    dreamcycle_active_end_hour: int = 6
    dreamcycle_rounds_per_window: int = 3
    dreamcycle_round_interval_minutes: int = 90
    dreamcycle_max_turns_per_job: int = 25
    dreamcycle_max_consecutive_errors: int = 3
    dreamcycle_retry_delay_seconds: int = 300
    dreamcycle_customer_id: str = ""
    dreamcycle_state_dir: str = ""
    dreamcycle_log_level: str = "INFO"
    dreamcycle_enabled_jobs: list[str] = field(
        default_factory=lambda: [
            "team_overview",
            "deduplication",
            "cleanup",
            "onboarding_check",
            "consolidate",
        ]
    )
    dreamcycle_job_prompts: dict[str, str] = field(default_factory=dict)
    dreamcycle_job_settings: dict[str, dict] = field(default_factory=dict)
    # Deprecated simplified-engine compatibility.
    dreamcycle_interval_seconds: int = 86400
    dreamcycle_max_source_items: int = 100
    dreamcycle_max_source_chars: int = 120000
    dreamcycle_extract_prompt: str = ""
    dreamcycle_consolidate_prompt: str = ""

    # ------------------------------------------------------------------ #
    # Background validation                                               #
    # ------------------------------------------------------------------ #
    # Enabled by default so the server's validated publish_mode has clients
    # that actually run candidate-vs-baseline replay; otherwise candidates
    # would queue indefinitely and never publish.
    validation_enabled: bool = True
    validation_mode: str = "true_replay"
    validation_idle_after_seconds: int = 300
    validation_poll_interval_seconds: int = 60
    validation_max_jobs_per_day: int = 5
    validation_max_concurrency: int = 1
    validation_required_results: int = 3
    validation_required_approvals: int = 2
    # Native replay runtime for sessions produced by AgentsHub.
    validation_agentshub_url: str = ""
    validation_agentshub_api_key: str = ""

    # ------------------------------------------------------------------ #
    # Langfuse session ingestion and observability                        #
    # ------------------------------------------------------------------ #
    # Pull agent sessions directly from a Langfuse deployment (verified against
    # Langfuse v3.117.2 public REST API) and feed them into the same ingest
    # pipeline used by Hermes/AgentsHub. Auth is Basic (public_key:secret_key).
    langfuse_enabled: bool = False
    langfuse_host: str = "https://cloud.langfuse.com"
    langfuse_public_key: str = ""
    langfuse_secret_key: str = ""
    langfuse_timeout_seconds: int = 30
    # Export teamEvolver's own evolution and DreamCycle model/tool traces.
    # This is intentionally independent from ``langfuse_enabled``, which
    # controls pulling external sessions into the evolution queue.
    langfuse_tracing_enabled: bool = False
    langfuse_tracing_environment: str = "local"
    langfuse_tracing_release: str = ""
    langfuse_tracing_sample_rate: float = 1.0
    langfuse_tracing_capture_content: bool = True
    langfuse_tracing_flush_at: int = 1
    langfuse_tracing_flush_interval_seconds: float = 1.0
    # Paging knobs for the public API. ``page_limit`` is items per request;
    # ``max_sessions`` caps how many sessions a single pull will materialize.
    langfuse_page_limit: int = 50
    langfuse_max_sessions: int = 100
    # Default session-attribute filters applied when a pull does not override
    # them. ``environment`` and ``tags`` accept multiple values.
    langfuse_default_environment: list[str] = field(default_factory=list)
    langfuse_default_user_id: str = ""
    langfuse_default_tags: list[str] = field(default_factory=list)
    langfuse_default_release: str = ""
    langfuse_default_version: str = ""
    langfuse_default_trace_name: str = ""
    # Operator-authored trace mapper: when enabled, ``langfuse_mapper_code``
    # defines ``map_trace(trace, observations)`` and produces the evolution turn
    # (deep-merged over the built-in mapping). Disabled/empty uses the built-in.
    langfuse_mapper_enabled: bool = False
    langfuse_mapper_code: str = ""
