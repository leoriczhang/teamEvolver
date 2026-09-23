"""Unified configuration for teamEvolver."""

from dataclasses import dataclass, field
from typing import Any

from team_memory.config import TeamMemoryConfig

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
class TeamEvolverConfig(TeamMemoryConfig):
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
    # Deployment-only migration switches; remove in the final retirement release.
    skills_delivery_mode: str = "push"
    agent_protocol_identity_mode: str = "dual"
    replay_adapter: str = ""
    replay_adapters_dir: str = ""

    # ------------------------------------------------------------------ #
    # skillopt rollout (DEAP /skillopt/update skill push)                 #
    # ------------------------------------------------------------------ #
    # Second outbox consumer: after a skill publish commits, push the full
    # version bundle into a persistent DEAP workspace (rollout-<timestamp>-<suffix>,
    # never deleted) via /skillopt/update.
    skillopt_rollout_enabled: bool = False
    skillopt_rollout_endpoint: str = ""
    skillopt_rollout_workspace_prefix: str = "rollout-"

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
    logging: dict = field(default_factory=dict)
    ontology: dict = field(default_factory=dict)
    experience_sync: dict = field(default_factory=dict)
    llm_provider: str = "openai"
    llm_api_base: str = "https://ark.cn-beijing.volces.com/api/v3"
    llm_api_key: str = ""
    llm_model_id: str = "doubao-seed-evolving"
    llm_api_mode: str = "chat"
    llm_max_tokens: int = 100000
    llm_temperature: float = 0.4
    # Each tenant owns an independent bounded LLM dispatcher. These values are
    # therefore per-tenant limits when supplied through tenant overrides.
    llm_max_concurrency: int = 8
    llm_queue_capacity: int = 64

    # ------------------------------------------------------------------ #
    # OpenRouter-specific (ignored for other providers)                    #
    # ------------------------------------------------------------------ #
    openrouter_app_name: str = "teamEvolver"
    openrouter_app_url: str = ""
    openrouter_route: str = "fallback"
    openrouter_fallback_models: str = ""
    openrouter_data_policy: str = ""

    # ------------------------------------------------------------------ #
    # Skill sharing and durable Skill storage                             #
    # ------------------------------------------------------------------ #
    # ``sharing_skill_backend`` selects where uploaded and evolved Skill
    # bundles, manifests, and version history are stored: ``viking`` or
    # ``local``. A local root may be a NAS mount.
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
    # Trusted Root Key used for both personal and team OpenViking namespaces.
    sharing_viking_api_key: str = ""
    # Deprecated per-user credential fields retained only for config parsing.
    # Workspace requests never use them.
    sharing_viking_personal_api_key: str = ""
    sharing_viking_personal_api_keys: list[str] = field(default_factory=list)
    # Compatibility field name for the Trusted Root Key.
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
    evolve_validation_max_rejections: int = 1
    evolve_human_review_enabled: bool = True
    evolve_human_review_timeout_seconds: int = 86400
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
    validation_runtimes: list[str] = field(default_factory=lambda: ["hermes", "deap", "agentshub", "langfuse", "doris"])
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
    # Tracing (self-reporting) target. Intentionally separate from the pull
    # source (``langfuse_host``/``langfuse_public_key``/``langfuse_secret_key``)
    # so evolution ingestion and self-tracing can point at different Langfuse
    # deployments. Empty means tracing has no dedicated target and stays off.
    langfuse_tracing_host: str = ""
    langfuse_tracing_public_key: str = ""
    langfuse_tracing_secret_key: str = ""
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
    # Deprecated: migrated into ``langfuse_mappers`` (kept only for migration).
    langfuse_mapper_enabled: bool = False
    langfuse_mapper_code: str = ""
    # Per-agent mapper registry (multi-mapper routing). Ordered list; each entry
    # is {name, enabled, note?, code, match: {trace_names, tags,
    # session_id_patterns}}. First matching enabled entry wins; empty match =
    # catch-all. Legacy mapper_enabled/mapper_code migrate into this list (see
    # integrations.langfuse_mapper.normalize_mapper_entries).
    langfuse_mappers: list[dict[str, Any]] = field(default_factory=list)

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
            "max_sessions": 1000,
        }
    )
    # Deprecated migration-only fields. Live pulls use datasource_adapter.
    # Selects the source adapter for session pulls: "langfuse" (built-in),
    # "skillopt" (legacy converter), a registered type, or a file-based type
    # discovered from ``session_ingestion/adapters/sources/<type>.py``.
    datasource_type: str = "langfuse"
    datasource_legacy_converter_code: str = ""
    datasource_legacy_project: str = ""
    datasource_legacy_options: dict[str, Any] = field(default_factory=dict)
    # Free-form connection settings for the active data source (host, token,
    # table names, ...). Passed to file-based adapters' build_adapter().
    datasource_options: dict[str, Any] = field(default_factory=dict)
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
