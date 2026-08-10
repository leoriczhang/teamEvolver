"""Unified configuration for teamEvolver."""

from dataclasses import dataclass, field

VOLCENGINE_OPENVIKING_ENDPOINT = "https://api.vikingdb.cn-beijing.volces.com/openviking"
TEAM_SKILL_ROOT_PREFIX = "team-skill-evolver"


@dataclass
class TeamEvolverConfig:
    # Internal source path used by admin routes that need to persist runtime
    # config updates back to the same file.
    _config_file: str = field(default="", repr=False)

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
    max_skills_prompt_chars: int = 30000

    # ------------------------------------------------------------------ #
    # Context window                                                       #
    # ------------------------------------------------------------------ #
    # Prompt budget retained for model-testing and validation clients. The token estimate divides
    # char count by 4, which undercounts CJK, so keep some headroom below the
    # model's hard context limit. Default targets modern 256k-context models.
    max_context_tokens: int = 240000

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
    # Skill sharing (OpenViking / local object storage)                   #
    # ------------------------------------------------------------------ #
    sharing_enabled: bool = True
    sharing_backend: str = "viking"
    sharing_endpoint: str = ""
    sharing_local_root: str = ""
    # Optional override for skill assets. When empty, sharing_backend keeps its
    # legacy behavior and is used for both skills and session artifacts.
    sharing_skill_backend: str = ""
    # Optional object-storage backend for non-skill artifacts when the skill
    # backend is reserved for the Skill registry.
    sharing_session_backend: str = ""

    # OpenViking backend (sharing.backend = "viking").
    sharing_viking_endpoint: str = VOLCENGINE_OPENVIKING_ENDPOINT
    # Backward-compatible fallback key. Prefer the scoped keys below when both
    # personal and team OpenViking spaces are configured.
    sharing_viking_api_key: str = ""
    sharing_viking_personal_api_key: str = ""
    sharing_viking_personal_api_keys: list[str] = field(default_factory=list)
    sharing_viking_team_api_key: str = ""
    sharing_viking_account: str = "default"
    sharing_viking_user: str = "default"
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
    evolve_evidence_enabled: bool = True
    evolve_evidence_max_entries: int = 200
    evolve_evidence_recent_limit: int = 12
    evolve_evidence_historical_limit: int = 12
    evolve_evidence_replay_cases_per_window: int = 1
    evolve_evidence_change_debt_threshold: int = 3
    evolve_candidate_coalesce_enabled: bool = True
    evolve_bundle_text_extensions: list[str] = field(
        default_factory=lambda: [".py", ".sh"]
    )
    evolve_bundle_max_file_bytes: int = 65536
    evolve_bundle_max_prompt_bytes: int = 262144
    evolve_bundle_allow_delete: bool = True
    evolve_bundle_static_checks_enabled: bool = True

    # ------------------------------------------------------------------ #
    # DreamCycle team-memory maintenance                                  #
    # ------------------------------------------------------------------ #
    # DreamCycle reads the personal keys configured under ``sharing`` and
    # writes through the same team key used by team skill evolution. It runs
    # an external ``dreamcycle`` engine, so it stays opt-in: enable it
    # explicitly and, if desired, let the service auto-start its daemon.
    dreamcycle_enabled: bool = False
    dreamcycle_auto_start: bool = False
    dreamcycle_daemon_command: str = "dreamcycle --daemon"
    dreamcycle_trigger_command: str = "dreamcycle --once"
    dreamcycle_viking_agent: str = "dreamcycle"
    dreamcycle_llm_base_url: str = ""
    dreamcycle_llm_api_key: str = ""
    dreamcycle_llm_model: str = ""

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
