"""Configuration adapter for teamEvolver's built-in evolution engine."""

from __future__ import annotations

import os
from dataclasses import dataclass, field

from teamEvolver.config import (
    DEFAULT_LLM_API_BASE,
    DEFAULT_LLM_MAX_CONCURRENCY,
    DEFAULT_LLM_MAX_TOKENS,
    DEFAULT_LLM_MODEL_ID,
    DEFAULT_LLM_QUEUE_CAPACITY,
    DEFAULT_LLM_TEMPERATURE,
)


def _first_env(*names: str, default: str = "") -> str:
    for name in names:
        value = os.environ.get(name)
        if value not in (None, ""):
            return value
    return default


@dataclass
class EvolveServerConfig:
    engine: str = "workflow"

    # Storage
    storage_backend: str = ""
    storage_endpoint: str = ""
    # Skill assets may use a different backend from Session/evolution state.
    # This allows PostgreSQL for hot state while Skill bundles and every
    # published version live on OpenViking or a mounted local/NAS filesystem.
    skill_storage_backend: str = ""
    skill_storage_local_root: str = ""

    # OpenViking storage backend.  Used when storage_backend == "viking".
    viking_endpoint: str = ""
    viking_api_key: str = ""
    viking_account: str = "default"
    viking_user: str = "team"
    viking_agent: str = "team-skill-evolver"
    # Account-scoped, team-shared resources root. Tenant isolation is carried
    # by object keys or PostgreSQL RLS, not legacy customer/group identifiers.
    viking_root_prefix: str = "team-skill-evolver"
    experience_pattern_root: str = ""

    # Built-in local-storage fallback: when the configured OpenViking
    # deployment is unavailable (connection error / timeout / HTTP 5xx), the
    # engine's object stores fall back to teamEvolver's own filesystem store
    # rooted at ``storage_local_root`` (empty = ~/.teamEvolver/local_store).
    storage_fallback_enabled: bool = True
    storage_local_root: str = ""

    # PostgreSQL local-state backend. Used when storage_backend == "postgres"
    # (multi-tenancy plan §2.4); empty pg_dsn derives from OV_PG_* env vars.
    pg_dsn: str = ""
    pg_schema: str = "teamevolver"
    pg_pool_min: int = 2
    pg_pool_max: int = 20
    pg_command_timeout_seconds: float = 30.0
    pg_ssl: str = "prefer"
    # Engine tenant scope (PostgreSQL RLS key). Standalone/CLI runs stay on the
    # implicit "default"; the proxy's per-tenant EnginePool overrides this field
    # per tenant when building each engine (multi-tenancy plan Phase 2).
    pg_tenant_id: str = "default"

    # Skill mirror to OpenViking: when the skill library lives on the local
    # backend, published skills are mirrored asynchronously into
    # ``viking://resources/{viking_root_prefix}/skills/<name>/`` so remote
    # Agents keep reading them. Only that subtree mirrors; registry/manifest
    # stay local. Empty spool dir = ~/.teamEvolver/skill_mirror_spool.
    skill_mirror_enabled: bool = True
    skill_mirror_spool_dir: str = ""

    # LLM
    llm_api_key: str = ""
    llm_base_url: str = "https://api.openai.com/v1"
    llm_model: str = "gpt-4o"
    llm_max_tokens: int = 128000
    llm_temperature: float = 0.4
    llm_api_type: str = "openai-completions"
    evolve_strategy: str = "dynamic_edit_conservative"

    # Evolution
    use_session_judge: bool = True
    # Cross-cycle evidence keeps long-term skill context while recent sessions
    # remain a distinct, high-sensitivity window.
    evidence_enabled: bool = True
    evidence_max_entries: int = 400
    evidence_recent_limit: int = 20
    evidence_historical_limit: int = 20
    evidence_replay_cases_per_window: int = 1
    evidence_change_debt_threshold: int = 3
    dataset_synthesis_enabled: bool = True
    dataset_test_cases: int = 2
    dataset_min_requirements: int = 12
    dataset_max_requirements: int = 24
    dataset_disclosure_batch_size: int = 4
    candidate_coalesce_enabled: bool = True
    # Max skill groups (plus the no-skill create) evolved concurrently per cycle.
    # Groups are independent branches; the only shared write (immediate-publish
    # upload) is serialized separately, so this only bounds LLM/dataset fan-out.
    max_parallel_groups: int = 8
    # Agent-loop budgets for the three skill-writing stages (plan → act →
    # submit). Rounds clamp to [2, 24]; tool calls per round to [1, 16].
    agent_max_rounds: int = 12
    agent_max_tool_calls_per_round: int = 8
    # Per-tenant cap on in-flight LLM calls. Each tenant has its own API
    # credential and dispatcher, so one tenant cannot occupy another's slots.
    llm_max_concurrency: int = 8
    llm_queue_capacity: int = 64
    # Max sessions consumed per evolution cycle (0 = unlimited). A large
    # backlog is churned down over multiple cycles with bounded blast radius.
    drain_max_per_cycle: int = 0
    # Team-evidence minima per evolution branch (skill group / no-skill create).
    # Evolution only runs when the planning evidence spans at least N distinct
    # sessions and M distinct users, so shared skills reflect team patterns
    # rather than a single user's preference or personal SOP. 0 disables a
    # check. Historical evidence-ledger sessions count toward both minima.
    min_group_sessions: int = 2
    min_group_users: int = 2
    bundle_text_extensions: list[str] = field(
        default_factory=lambda: [".py", ".sh"]
    )
    bundle_max_file_bytes: int = 262144
    bundle_max_prompt_bytes: int = 786432
    bundle_allow_delete: bool = True
    bundle_static_checks_enabled: bool = True
    publish_mode: str = "validated"
    validation_runtimes: list[str] = field(
        default_factory=lambda: ["deap", "langfuse", "doris"]
    )
    # Human-in-the-loop: when centralized replay/AB validation is inconclusive (the
    # gray zone), escalate the job to a human review queue instead of leaving it
    # pending forever. Non-blocking: a reminder is surfaced each cycle until a
    # human resolves it via the dashboard review endpoint.
    human_review_enabled: bool = True
    debug_dump_dir: str = ""

    # Scheduling
    interval_seconds: int = 600

    def __post_init__(self) -> None:
        self.engine = str(self.engine or "workflow").strip().lower() or "workflow"
        self.storage_backend = (
            str(self.storage_backend or "").strip().lower() or "local"
        )
        self.skill_storage_backend = (
            str(self.skill_storage_backend or "").strip().lower()
            or self.storage_backend
        )
        self.evidence_max_entries = max(1, int(self.evidence_max_entries or 1))
        self.evidence_recent_limit = max(1, int(self.evidence_recent_limit or 1))
        self.evidence_historical_limit = max(0, int(self.evidence_historical_limit or 0))
        self.evidence_replay_cases_per_window = max(
            1, int(self.evidence_replay_cases_per_window or 1)
        )
        self.evidence_change_debt_threshold = max(
            1, int(self.evidence_change_debt_threshold or 1)
        )
        self.dataset_test_cases = max(1, min(6, int(self.dataset_test_cases or 2)))
        self.dataset_min_requirements = max(
            1, int(self.dataset_min_requirements or 1)
        )
        self.dataset_max_requirements = max(
            self.dataset_min_requirements,
            int(self.dataset_max_requirements or self.dataset_min_requirements),
        )
        self.dataset_disclosure_batch_size = max(
            1, int(self.dataset_disclosure_batch_size or 1)
        )
        self.max_parallel_groups = max(1, int(self.max_parallel_groups or 1))
        self.llm_max_concurrency = max(
            1,
            min(64, int(self.llm_max_concurrency or 1)),
        )
        self.llm_queue_capacity = max(
            self.llm_max_concurrency,
            min(10000, int(self.llm_queue_capacity or self.llm_max_concurrency)),
        )
        self.agent_max_rounds = max(2, min(24, int(self.agent_max_rounds or 12)))
        self.agent_max_tool_calls_per_round = max(
            1, min(16, int(self.agent_max_tool_calls_per_round or 8))
        )
        self.min_group_sessions = max(0, int(self.min_group_sessions or 0))
        self.min_group_users = max(0, int(self.min_group_users or 0))
        normalized_extensions: list[str] = []
        raw_extensions = self.bundle_text_extensions
        if isinstance(raw_extensions, str):
            raw_extensions = raw_extensions.replace("\n", ",").split(",")
        for raw in raw_extensions or []:
            item = str(raw or "").strip().lower().lstrip(".")
            if not item or "/" in item or "\\" in item:
                continue
            extension = f".{item}"
            if extension not in normalized_extensions:
                normalized_extensions.append(extension)
        self.bundle_text_extensions = normalized_extensions or [".py", ".sh"]
        self.bundle_max_file_bytes = max(1, int(self.bundle_max_file_bytes or 1))
        self.bundle_max_prompt_bytes = max(
            1, int(self.bundle_max_prompt_bytes or 1)
        )
        self.publish_mode = str(self.publish_mode or "direct").strip().lower() or "direct"
        if self.publish_mode not in {"direct", "validated"}:
            self.publish_mode = "direct"
    @classmethod
    def from_teamEvolver_config(cls, config) -> "EvolveServerConfig":
        """Build from teamEvolver's primary configuration object."""
        engine = _first_env("EVOLVE_ENGINE", default="workflow").strip().lower() or "workflow"
        viking_endpoint = str(getattr(config, "sharing_viking_endpoint", "") or "")
        storage_endpoint = viking_endpoint
        llm_api_key = str(getattr(config, "llm_api_key", "") or "")
        llm_base_url = str(
            getattr(config, "llm_api_base", "")
            or DEFAULT_LLM_API_BASE
        )
        llm_model = os.environ.get(
            "EVOLVE_MODEL",
            str(getattr(config, "llm_model_id", "") or DEFAULT_LLM_MODEL_ID),
        )
        llm_api_type = os.environ.get("EVOLVE_LLM_API_TYPE", "openai-completions")
        llm_max_tokens = int(
            os.environ.get(
                "EVOLVE_LLM_MAX_TOKENS",
                str(
                    getattr(config, "llm_max_tokens", DEFAULT_LLM_MAX_TOKENS)
                    or DEFAULT_LLM_MAX_TOKENS
                ),
            )
        )
        llm_temperature = float(
            os.environ.get(
                "EVOLVE_LLM_TEMPERATURE",
                str(
                    getattr(
                        config,
                        "llm_temperature",
                        DEFAULT_LLM_TEMPERATURE,
                    )
                ),
            )
        )

        explicit_storage_backend = _first_env("EVOLVE_STORAGE_BACKEND", default="")
        storage_backend = explicit_storage_backend
        if not storage_backend:
            storage_backend = str(
                getattr(config, "sharing_session_backend", "") or ""
            ).strip().lower() or "local"
        skill_storage_backend = _first_env(
            "EVOLVE_SKILL_STORAGE_BACKEND", default=""
        ).strip().lower()
        if not skill_storage_backend:
            # Preserve the old EVOLVE_STORAGE_BACKEND behavior for standalone
            # deployments while allowing TeamEvolver's per-purpose split.
            skill_storage_backend = (
                explicit_storage_backend
                or str(
                    getattr(config, "sharing_skill_backend", "") or ""
                ).strip().lower()
                or "local"
            )

        return cls(
            engine=engine,
            storage_backend=storage_backend,
            storage_endpoint=storage_endpoint,
            skill_storage_backend=skill_storage_backend,
            skill_storage_local_root=os.environ.get(
                "EVOLVE_SKILL_STORAGE_LOCAL_ROOT",
                str(
                    getattr(config, "sharing_skill_local_root", "")
                    or getattr(config, "sharing_local_root", "")
                    or ""
                ),
            ),
            viking_endpoint=viking_endpoint,
            viking_api_key=str(getattr(config, "sharing_viking_api_key", "") or ""),
            viking_account=str(getattr(config, "sharing_viking_account", "") or "default"),
            viking_user=str(getattr(config, "sharing_viking_user", "") or "team"),
            viking_agent=str(getattr(config, "sharing_viking_agent", "") or "team-skill-evolver"),
            viking_root_prefix=str(getattr(config, "sharing_viking_root_prefix", "") or "team-skill-evolver"),
            experience_pattern_root=str(
                (
                    getattr(config, "experience_sync", {})
                    if isinstance(getattr(config, "experience_sync", {}), dict)
                    else {}
                ).get("target_directory")
                or ""
            ),
            storage_fallback_enabled=os.environ.get(
                "EVOLVE_STORAGE_FALLBACK",
                "1" if getattr(config, "sharing_local_fallback_enabled", True) else "0",
            ).lower() not in {"0", "false", "no"},
            storage_local_root=os.environ.get(
                "EVOLVE_STORAGE_LOCAL_ROOT",
                str(getattr(config, "sharing_local_root", "") or ""),
            ),
            pg_dsn=str(getattr(config, "storage_pg_dsn", "") or ""),
            pg_schema=str(getattr(config, "storage_pg_schema", "") or "teamevolver"),
            pg_pool_min=max(1, int(getattr(config, "storage_pg_pool_min", 2) or 2)),
            pg_pool_max=max(2, int(getattr(config, "storage_pg_pool_max", 20) or 20)),
            pg_command_timeout_seconds=max(
                1.0, float(getattr(config, "storage_pg_command_timeout_seconds", 30.0) or 30.0)
            ),
            pg_ssl=str(getattr(config, "storage_pg_ssl", "prefer") or "prefer"),
            pg_tenant_id="default",  # overridden per tenant by the proxy EnginePool
            skill_mirror_enabled=os.environ.get(
                "EVOLVE_SKILL_MIRROR",
                "1" if getattr(config, "sharing_skill_mirror_enabled", True) else "0",
            ).lower() not in {"0", "false", "no"},
            skill_mirror_spool_dir=os.environ.get(
                "EVOLVE_SKILL_MIRROR_SPOOL_DIR",
                str(getattr(config, "sharing_skill_mirror_spool_dir", "") or ""),
            ),
            llm_api_key=llm_api_key,
            llm_base_url=llm_base_url,
            llm_model=llm_model,
            llm_max_tokens=llm_max_tokens,
            llm_temperature=llm_temperature,
            llm_api_type=llm_api_type,
            evolve_strategy=os.environ.get("EVOLVE_STRATEGY", "dynamic_edit_conservative"),
            use_session_judge=os.environ.get(
                "EVOLVE_USE_SESSION_JUDGE",
                "1"
                if getattr(config, "evolve_use_session_judge", True)
                else "0",
            ).lower()
            not in {"0", "false", "no"},
            evidence_enabled=os.environ.get(
                "EVOLVE_EVIDENCE_ENABLED",
                "1" if getattr(config, "evolve_evidence_enabled", True) else "0",
            ).lower()
            not in {"0", "false", "no"},
            evidence_max_entries=int(
                os.environ.get(
                    "EVOLVE_EVIDENCE_MAX_ENTRIES",
                    str(getattr(config, "evolve_evidence_max_entries", 400) or 400),
                )
            ),
            evidence_recent_limit=int(
                os.environ.get(
                    "EVOLVE_EVIDENCE_RECENT_LIMIT",
                    str(getattr(config, "evolve_evidence_recent_limit", 20) or 20),
                )
            ),
            evidence_historical_limit=int(
                os.environ.get(
                    "EVOLVE_EVIDENCE_HISTORICAL_LIMIT",
                    str(getattr(config, "evolve_evidence_historical_limit", 20) or 0),
                )
            ),
            evidence_replay_cases_per_window=int(
                os.environ.get(
                    "EVOLVE_EVIDENCE_REPLAY_CASES_PER_WINDOW",
                    str(
                        getattr(
                            config,
                            "evolve_evidence_replay_cases_per_window",
                            1,
                        )
                        or 1
                    ),
                )
            ),
            evidence_change_debt_threshold=int(
                os.environ.get(
                    "EVOLVE_EVIDENCE_CHANGE_DEBT_THRESHOLD",
                    str(
                        getattr(
                            config,
                            "evolve_evidence_change_debt_threshold",
                            3,
                        )
                        or 3
                    ),
                )
            ),
            dataset_synthesis_enabled=os.environ.get(
                "EVOLVE_DATASET_SYNTHESIS_ENABLED",
                "1"
                if getattr(config, "evolve_dataset_synthesis_enabled", True)
                else "0",
            ).lower()
            not in {"0", "false", "no"},
            dataset_test_cases=int(
                os.environ.get(
                    "EVOLVE_DATASET_TEST_CASES",
                    str(getattr(config, "evolve_dataset_test_cases", 2) or 2),
                )
            ),
            dataset_min_requirements=int(
                os.environ.get(
                    "EVOLVE_DATASET_MIN_REQUIREMENTS",
                    str(
                        getattr(config, "evolve_dataset_min_requirements", 12)
                        or 12
                    ),
                )
            ),
            dataset_max_requirements=int(
                os.environ.get(
                    "EVOLVE_DATASET_MAX_REQUIREMENTS",
                    str(
                        getattr(config, "evolve_dataset_max_requirements", 24)
                        or 24
                    ),
                )
            ),
            dataset_disclosure_batch_size=int(
                os.environ.get(
                    "EVOLVE_DATASET_DISCLOSURE_BATCH_SIZE",
                    str(
                        getattr(
                            config,
                            "evolve_dataset_disclosure_batch_size",
                            4,
                        )
                        or 4
                    ),
                )
            ),
            candidate_coalesce_enabled=os.environ.get(
                "EVOLVE_CANDIDATE_COALESCE_ENABLED",
                "1"
                if getattr(config, "evolve_candidate_coalesce_enabled", True)
                else "0",
            ).lower()
            not in {"0", "false", "no"},
            max_parallel_groups=int(
                os.environ.get(
                    "EVOLVE_MAX_PARALLEL_GROUPS",
                    str(getattr(config, "evolve_max_parallel_groups", 8) or 8),
                )
            ),
            agent_max_rounds=int(
                os.environ.get(
                    "EVOLVE_AGENT_MAX_ROUNDS",
                    str(getattr(config, "evolve_agent_max_rounds", 12) or 12),
                )
            ),
            agent_max_tool_calls_per_round=int(
                os.environ.get(
                    "EVOLVE_AGENT_MAX_TOOL_CALLS_PER_ROUND",
                    str(
                        getattr(
                            config,
                            "evolve_agent_max_tool_calls_per_round",
                            8,
                        )
                        or 8
                    ),
                )
            ),
            llm_max_concurrency=int(
                os.environ.get(
                    "EVOLVE_LLM_MAX_CONCURRENCY",
                    str(
                        getattr(
                            config,
                            "llm_max_concurrency",
                            DEFAULT_LLM_MAX_CONCURRENCY,
                        )
                        or DEFAULT_LLM_MAX_CONCURRENCY
                    ),
                )
            ),
            llm_queue_capacity=int(
                os.environ.get(
                    "EVOLVE_LLM_QUEUE_CAPACITY",
                    str(
                        getattr(
                            config,
                            "llm_queue_capacity",
                            DEFAULT_LLM_QUEUE_CAPACITY,
                        )
                        or DEFAULT_LLM_QUEUE_CAPACITY
                    ),
                )
            ),
            min_group_sessions=int(
                os.environ.get(
                    "EVOLVE_MIN_GROUP_SESSIONS",
                    str(getattr(config, "evolve_min_group_sessions", 2) or 0),
                )
            ),
            min_group_users=int(
                os.environ.get(
                    "EVOLVE_MIN_GROUP_USERS",
                    str(getattr(config, "evolve_min_group_users", 2) or 0),
                )
            ),
            drain_max_per_cycle=int(
                os.environ.get(
                    "EVOLVE_DRAIN_MAX_PER_CYCLE",
                    str(getattr(config, "evolve_drain_max_per_cycle", 0) or 0),
                )
            ),
            bundle_text_extensions=list(
                getattr(config, "evolve_bundle_text_extensions", [".py", ".sh"])
                or [".py", ".sh"]
            ),
            bundle_max_file_bytes=int(
                getattr(config, "evolve_bundle_max_file_bytes", 262144) or 262144
            ),
            bundle_max_prompt_bytes=int(
                getattr(config, "evolve_bundle_max_prompt_bytes", 786432)
                or 786432
            ),
            bundle_allow_delete=bool(
                getattr(config, "evolve_bundle_allow_delete", True)
            ),
            bundle_static_checks_enabled=bool(
                getattr(config, "evolve_bundle_static_checks_enabled", True)
            ),
            publish_mode=os.environ.get(
                "EVOLVE_PUBLISH_MODE",
                str(
                    getattr(config, "evolve_publish_mode", "validated")
                    or "validated"
                ),
            ),
            validation_runtimes=list(config.validation_runtimes),
            human_review_enabled=os.environ.get(
                "EVOLVE_HUMAN_REVIEW_ENABLED",
                "1" if getattr(config, "evolve_human_review_enabled", True) else "0",
            ).lower() not in {"0", "false", "no"},
            interval_seconds=int(
                os.environ.get(
                    "EVOLVE_INTERVAL",
                    str(
                        getattr(config, "evolve_interval_seconds", 600)
                        or 600
                    ),
                )
            ),
        )
