"""Unified configuration for teamEvolver."""

from dataclasses import dataclass, field
from typing import Any

from team_memory.config import TeamMemoryConfig

VOLCENGINE_OPENVIKING_ENDPOINT = "https://api.vikingdb.cn-beijing.volces.com/openviking"
# Default endpoint for a locally self-hosted ``openviking-server`` (see
# https://docs.openviking.ai server quickstart — Uvicorn binds :1933).
LOCAL_OPENVIKING_ENDPOINT = "http://localhost:1933"
TEAM_SKILL_ROOT_PREFIX = "team-skill-evolver"
DEFAULT_LLM_API_BASE = "https://ark.cn-beijing.volces.com/api/v3"
DEFAULT_LLM_MODEL_ID = "volcengine/glm-5.2-aicc"
DEFAULT_LLM_API_MODE = "chat"
DEFAULT_LLM_MAX_TOKENS = 100000
DEFAULT_LLM_TEMPERATURE = 0.4
DEFAULT_LLM_MAX_CONCURRENCY = 8
DEFAULT_LLM_QUEUE_CAPACITY = 64


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
class TeamEvolverConfig(TeamMemoryConfig):
    # Internal source path used by admin routes that need to persist runtime
    # config updates back to the same file.
    _config_file: str = field(default="", repr=False)

    # ------------------------------------------------------------------ #
    # Team identity                                                       #
    # ------------------------------------------------------------------ #
    team_display_name: str = "Team"

    # ------------------------------------------------------------------ #
    # Skills                                                              #
    # ------------------------------------------------------------------ #
    # Rebuildable per-tenant cache used by file-based runtimes.
    # Published Skill state lives in SkillHub storage.
    skills_runtime_cache_dir: str = "~/.teamEvolver/runtime/skills"
    replay_adapter: str = ""
    replay_adapters_dir: str = ""

    # ------------------------------------------------------------------ #
    # Skillopt rollout (DEAP /skillopt/update)                            #
    # ------------------------------------------------------------------ #
    # After a Skill publish commits, materialize the full version bundle in a
    # persistent DEAP workspace (rollout-<timestamp>-<suffix>, never deleted).
    skillopt_rollout_enabled: bool = False
    skillopt_rollout_endpoint: str = ""
    skillopt_rollout_workspace_prefix: str = "rollout-"

    # ------------------------------------------------------------------ #
    # API Server                                                          #
    # ------------------------------------------------------------------ #
    proxy_port: int = 52010
    proxy_host: str = "0.0.0.0"
    self_registration_enabled: bool = True

    # ------------------------------------------------------------------ #
    # LLM forwarding                                                      #
    # ------------------------------------------------------------------ #
    logging: dict = field(default_factory=dict)
    ontology: dict = field(default_factory=dict)
    compile: dict = field(default_factory=dict)
    experience_sync: dict = field(default_factory=dict)
    llm_api_base: str = DEFAULT_LLM_API_BASE
    llm_api_key: str = ""
    llm_model_id: str = DEFAULT_LLM_MODEL_ID
    llm_api_mode: str = DEFAULT_LLM_API_MODE
    llm_max_tokens: int = DEFAULT_LLM_MAX_TOKENS
    llm_temperature: float = DEFAULT_LLM_TEMPERATURE
    # Each tenant owns an independent bounded LLM dispatcher. These values are
    # therefore per-tenant limits when supplied through tenant overrides.
    llm_max_concurrency: int = DEFAULT_LLM_MAX_CONCURRENCY
    llm_queue_capacity: int = DEFAULT_LLM_QUEUE_CAPACITY

    # ------------------------------------------------------------------ #
    # Durable Skill storage and OpenViking integration                    #
    # ------------------------------------------------------------------ #
    # Deployment target for the OpenViking backend: ``cloud`` or ``local``.
    sharing_viking_deployment: str = "cloud"
    # Optional override for skill assets. Empty resolves to local storage, or
    # PostgreSQL when storage_pg is enabled.
    sharing_skill_backend: str = ""
    # Optional object-storage backend for non-Skill artifacts.
    sharing_session_backend: str = ""

    # Per-purpose backend split. When PostgreSQL is enabled, empty per-purpose
    # values resolve to PostgreSQL so replicas share session state, validation
    # artifacts, Skill manifests, versions and outboxes. Without PostgreSQL,
    # empty values keep the single-process local default. Explicit values remain
    # supported for mounted NAS or OpenViking deployments.

    # When the team skill library is on the local backend, evolved/pushed
    # skills are mirrored asynchronously to OpenViking so remote Agents can keep
    # reading them from ``viking://resources/{root_prefix}/skills/<name>/``.
    # Only that subtree is mirrored — registry/manifest stay local.
    sharing_skill_mirror_enabled: bool = True
    # Local spool directory for pending mirror deliveries; empty means
    # ~/.teamEvolver/skill_mirror_spool. The spool makes mirroring durable:
    # OpenViking outages never block evolution, deliveries retry with backoff.
    sharing_skill_mirror_spool_dir: str = ""

    # Built-in local-storage fallback. When enabled (default), any object store
    # built for an OpenViking deployment that is currently unavailable
    # (connection error / timeout / HTTP 5xx) falls back to teamEvolver's own
    # filesystem store rooted at ``sharing_local_root`` so ingest / evolution /
    # validation keep working through an outage. 4xx responses (e.g. auth
    # misconfiguration) never trigger the fallback — the server answered, so
    # the error must surface instead of being masked by local writes. Data
    # written locally during an outage stays local (no automatic sync-back).
    sharing_local_fallback_enabled: bool = True
    # Empty means ~/.teamEvolver/local_store.
    sharing_local_root: str = ""
    # Dedicated filesystem root for Skill bundles and their version history.
    # Empty inherits ``sharing_local_root``. Production local deployments
    # should point this at a mounted persistent/NAS directory.
    sharing_skill_local_root: str = ""

    # OpenViking backend (sharing.backend = "viking"). When empty the endpoint
    # is derived from ``sharing_viking_deployment`` (cloud vs local); a
    # non-empty value is an explicit advanced override.
    sharing_viking_endpoint: str = ""
    # Single Trusted Root Key for all tenant OpenViking operations.
    sharing_viking_api_key: str = ""
    sharing_viking_account: str = "default"
    # Canonical OpenViking user for team-owned Agent context.
    sharing_viking_user: str = "team"
    # Default shared skill namespace
    # (viking://resources/team-skill-evolver/...).
    sharing_viking_agent: str = TEAM_SKILL_ROOT_PREFIX
    # wire constant: root prefix is the OpenViking data contract namespace, do
    # not rename
    sharing_viking_root_prefix: str = TEAM_SKILL_ROOT_PREFIX
    # ------------------------------------------------------------------ #
    # PostgreSQL local-state storage (multi-tenancy plan §2.4)             #
    # ------------------------------------------------------------------ #
    # Backends created with backend="postgres" land here. Uses the project's
    # dedicated pgm instance; all TeamEvolver tables live in the dedicated
    # schema below. Empty DSN derives from the TEAMEVOLVER_PG_* environment
    # variables, percent-encoding the password automatically.
    storage_pg_enabled: bool = False
    storage_pg_dsn: str = ""
    storage_pg_schema: str = "teamevolver"
    storage_pg_pool_min: int = 2
    storage_pg_pool_max: int = 20
    storage_pg_command_timeout_seconds: float = 30.0
    storage_pg_ssl: str = "prefer"  # disable | prefer | require | verify-full
    # Dedicated control-plane pool for TenantRegistry (admin/tenant lookups).
    # Separate from the main pool so background work (evolution cycles, session
    # judging) can never starve /api/tenants and other admin queries.
    storage_pg_control_pool_max: int = 5
    # Single-tenant (storage_pg disabled) machine credential. Multi-tenant
    # deployments issue one credential per tenant from the console and leave
    # this empty. Must carry the ``tevt_`` prefix — the request middleware only
    # routes that prefix into the machine-credential branch.
    tenant_machine_token: str = ""

    # ------------------------------------------------------------------ #
    # Evolve server integration                                           #
    # ------------------------------------------------------------------ #
    evolve_server_url: str = "http://127.0.0.1:52010"
    evolve_use_session_judge: bool = True
    evolve_publish_mode: str = "validated"
    evolve_human_review_enabled: bool = True
    evolve_interval_seconds: int = 600
    # Drain throttling: queued sessions are read in batches so a large backlog
    # does not saturate the (self-hosted) OpenViking storage. Raise batch size
    # or lower the delay to drain faster on a beefier storage instance.
    evolve_drain_batch_size: int = 25
    evolve_drain_batch_delay_seconds: float = 1.0
    # Max sessions consumed per evolution cycle (0 = unlimited). Lets a large
    # backlog be fed once and churned down over multiple cycles with bounded
    # blast radius per cycle.
    evolve_drain_max_per_cycle: int = 0
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
    evolve_agent_max_rounds: int = 12
    evolve_agent_max_tool_calls_per_round: int = 8
    # Team-evidence minima per evolution branch (see EvolveServerConfig).
    evolve_min_group_sessions: int = 2
    evolve_min_group_users: int = 2
    evolve_bundle_text_extensions: list[str] = field(
        default_factory=lambda: [".py", ".sh"]
    )
    evolve_bundle_max_file_bytes: int = 262144
    evolve_bundle_max_prompt_bytes: int = 786432
    evolve_bundle_allow_delete: bool = True
    evolve_bundle_static_checks_enabled: bool = True

    # ------------------------------------------------------------------ #
    # Background validation                                               #
    # ------------------------------------------------------------------ #
    # Enabled by default so the server's validated publish_mode has clients
    # that actually run candidate-vs-baseline replay; otherwise candidates
    # would queue indefinitely and never publish.
    validation_enabled: bool = True
    validation_mode: str = "true_replay"
    validation_runtimes: list[str] = field(
        default_factory=lambda: ["deap", "langfuse", "doris"]
    )
    validation_idle_after_seconds: int = 300
    validation_poll_interval_seconds: int = 60
    validation_max_jobs_per_day: int = 5
    validation_max_concurrency: int = 1
    # ------------------------------------------------------------------ #
    # Langfuse observability                                               #
    # ------------------------------------------------------------------ #
    # Inbound Session sources are configured in tenant adapter files. These
    # fields only control outbound teamEvolver model/tool tracing.
    langfuse_tracing_enabled: bool = False
    langfuse_tracing_host: str = ""
    langfuse_tracing_public_key: str = ""
    langfuse_tracing_secret_key: str = ""
    langfuse_tracing_environment: str = "local"
    langfuse_tracing_release: str = ""
    langfuse_tracing_sample_rate: float = 1.0
    langfuse_tracing_capture_content: bool = True
    langfuse_tracing_flush_at: int = 1
    langfuse_tracing_flush_interval_seconds: float = 1.0
    langfuse_tracing_timeout_seconds: int = 30

    # ------------------------------------------------------------------ #
    # Tenant adapter binding                                              #
    # ------------------------------------------------------------------ #
    datasource_adapter: str = ""
    # Per-tenant daily pull schedule. The default tenant reads this value
    # from config.yaml; non-default tenants override the complete dictionary
    # in tenants.config through the dedicated datasource API.
    datasource_schedule: dict[str, Any] = field(
        default_factory=lambda: {
            "enabled": False,
            "time": "00:00",
            "timezone": "Asia/Shanghai",
            "window": "previous_day",
        }
    )
    # Deployment-owned tenant-file directory; empty uses the adapters package.
    # Not tenant-overridable. Shared upstream code ships inside
    # session_ingestion/adapters/.
    datasource_adapters_dir: str = ""

    # ------------------------------------------------------------------ #
    # Session split (semantic topic segmentation)                          #
    # ------------------------------------------------------------------ #
    # Sessions may mix several unrelated topics with chitchat. Before value
    # classification, every session is evaluated for a semantic topic split:
    # one cheap LLM boundary call decides topic-coherent sub-sessions. Purely
    # semantic — no turn-count thresholds, time gaps, or size caps. When no
    # model is configured (or the boundary reply fails validation), sessions
    # are ingested unsplit.
    session_split_enabled: bool = True
