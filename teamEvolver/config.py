"""Unified configuration for teamEvolver."""

from dataclasses import dataclass, field

VOLCENGINE_OPENVIKING_ENDPOINT = "https://api.vikingdb.cn-beijing.volces.com/openviking"
# Default endpoint for a locally self-hosted ``openviking-server`` (see
# https://docs.openviking.ai server quickstart — Uvicorn binds :1933).
LOCAL_OPENVIKING_ENDPOINT = "http://localhost:1933"
TEAM_SKILL_ROOT_PREFIX = "team-skill-evolver"
DEFAULT_LLM_API_BASE = "https://api.openai.com/v1"
DEFAULT_LLM_MODEL_ID = "gpt-4o-mini"
DEFAULT_LLM_API_MODE = "chat"
DEFAULT_LLM_MAX_TOKENS = 16384
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
class TeamEvolverConfig:
    # Internal source path used by admin routes that need to persist runtime
    _config_file: str = field(default="", repr=False)

    # ------------------------------------------------------------------ #
    # Team Memory (owned conceptually by ``team_memory`` module).          #
    # These flat fields mirror ``team_memory.config.TeamMemoryConfig`` so   #
    # the platform global config stays dependency-free (platform must not   #
    # import business packages). ``team_memory`` remains the authoritative  #
    # source of this field set; ``team_memory`` tests assert the two stay   #
    # in sync. Keep additions mirrored in both places.                      #
    # ------------------------------------------------------------------ #
    # Maintenance is a scheduled ``ov cp`` + ``ov compile`` run.
    memory_maintenance_enabled: bool = False
    memory_maintenance_auto_start: bool = False
    memory_maintenance_interval_seconds: int = 86400
    # Cross-user memory aggregation (deterministic staging + ov compile).
    aggregation_enabled: bool = False
    aggregation_shared_knowledge_prefix: str = "shared-knowledge"
    aggregation_okf_skill_uri: str = "viking://agent/skills/team-memory-okf"
    aggregation_maintenance_skill_uri: str = "viking://agent/skills/team-memory-maintenance"
    aggregation_staging_dir: str = "staging"
    aggregation_kinds: list[str] = field(default_factory=list)
    aggregation_account_user_limit: int = 50_000
    aggregation_account_user_page_size: int = 1_000
    aggregation_phase1_concurrency: int = 6
    aggregation_merge_fan_in: int = 4
    aggregation_merge_concurrency: int = 4
    aggregation_partition_threshold: int = 512
    aggregation_partition_count: int = 256
    aggregation_preserve_manual_edits: bool = False
    aggregation_incremental_publish: bool = False
    aggregation_publish_batch_users: int = 8
    aggregation_run_detail_limit: int = 2_000
    aggregation_compile_runtime_timeout_seconds: int = 3000
    aggregation_merge_group_max_attempts: int = 2
    aggregation_state_dir: str = ""

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
    # reading them from ``viking://resources/{root_prefix}/skills/<name>/``.
    # OpenViking outages never block evolution, deliveries retry with backoff.

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
    # PostgreSQL local-state storage              #
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
    # Drain throttling: queued sessions are read in batches so a large backlog
    # does not saturate the (self-hosted) OpenViking storage. Raise batch size
    # or lower the delay to drain faster on a beefier storage instance.
    # Max sessions consumed per evolution cycle (0 = unlimited). Lets a large
    # backlog be fed once and churned down over multiple cycles with bounded
    # blast radius per cycle.

    # ------------------------------------------------------------------ #
    # Background validation                                               #
    # ------------------------------------------------------------------ #
    # Enabled by default so the server's validated publish_mode has clients
    # that actually run candidate-vs-baseline replay; otherwise candidates
    # would queue indefinitely and never publish.
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
    # Per-tenant daily pull schedule. The default tenant reads this value
    # from config.yaml; non-default tenants override the complete dictionary
    # in tenants.config through the dedicated datasource API.
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
