"""Configuration adapter for teamEvolver's built-in evolution engine."""

from __future__ import annotations

import os
from dataclasses import dataclass, field


def _first_env(*names: str, default: str = "") -> str:
    for name in names:
        value = os.environ.get(name)
        if value not in (None, ""):
            return value
    return default


def _infer_storage_backend(endpoint: str, local_root: str) -> str:
    backend = _first_env("EVOLVE_STORAGE_BACKEND", default="").strip().lower()
    if backend:
        return backend
    if local_root:
        return "local"
    if os.environ.get("EVOLVE_VIKING_ENDPOINT") or endpoint:
        return "viking"
    return ""


@dataclass
class EvolveServerConfig:
    engine: str = "workflow"

    # Storage
    storage_backend: str = ""
    storage_endpoint: str = ""
    local_root: str = ""

    # OpenViking storage backend.  Used when storage_backend == "viking".
    viking_endpoint: str = ""
    viking_api_key: str = ""
    viking_account: str = "default"
    viking_user: str = "default"
    viking_agent: str = "team-skill-evolver"
    # Identity fields sent to OpenViking for attribution. Evolved skills are
    # written to the resources namespace; customer_id may still scope isolated
    # prefixes such as ``peers/{customer_id}/`` when a caller explicitly uses it.
    viking_agent_id: str = ""
    viking_customer_id: str = ""
    # Account-scoped, team-shared resources root layout
    # (``viking://resources/{root_prefix}/...``, with an optional ``{group_id}``
    # segment when set). Skills, manifest, registry, and version bundles all live
    # here so Hermes' ``OpenVikingSkillSource`` can read them directly. Empty
    # group_id (default) means the team library has no group segment.
    viking_root_prefix: str = "team-skill-evolver"
    viking_group_id: str = ""

    # LLM
    llm_api_key: str = ""
    llm_base_url: str = "https://api.openai.com/v1"
    llm_model: str = "gpt-4o"
    llm_max_tokens: int = 128000
    llm_temperature: float = 0.4
    llm_api_type: str = "openai-completions"
    evolve_strategy: str = "dynamic_edit_conservative"
    use_success_feedback: bool = True

    # Evolution
    evolve_batch_size: int = 20
    reject_rewrite: bool = False  # Reject skill improvements that look like full rewrites
    use_session_judge: bool = True
    use_skill_verifier: bool = True
    skill_verifier_min_score: float = 0.75
    # Semantic dedup gate: an LLM checks whether a brand-new skill is redundant
    # against the existing library before it is published.
    use_skill_dedup: bool = True
    skill_dedup_max_similarity: float = 0.8
    # Progressive disclosure: stage 1 shortlists by metadata only, stage 2 only
    # fetches full content for at most this many skills, bounding prompt size.
    skill_dedup_shortlist_size: int = 5
    # Cross-cycle evidence keeps long-term skill context while recent sessions
    # remain a distinct, high-sensitivity window.
    evidence_enabled: bool = True
    evidence_max_entries: int = 200
    evidence_recent_limit: int = 12
    evidence_historical_limit: int = 12
    evidence_replay_cases_per_window: int = 1
    evidence_change_debt_threshold: int = 3
    candidate_coalesce_enabled: bool = True
    bundle_text_extensions: list[str] = field(
        default_factory=lambda: [".py", ".sh"]
    )
    bundle_max_file_bytes: int = 65536
    bundle_max_prompt_bytes: int = 262144
    bundle_allow_delete: bool = True
    bundle_static_checks_enabled: bool = True
    publish_mode: str = "validated"
    validation_required_results: int = 3
    validation_required_approvals: int = 2
    validation_min_mean_score: float = 0.75
    validation_max_rejections: int = 1
    # Human-in-the-loop: when client replay/AB validation is inconclusive (the
    # gray zone), escalate the job to a human review queue instead of leaving it
    # pending forever. Non-blocking: a reminder is surfaced each cycle until a
    # human resolves it via the dashboard review endpoint.
    human_review_enabled: bool = True
    human_review_pending_timeout_seconds: int = 86400
    debug_dump_dir: str = ""

    # Scheduling
    interval_seconds: int = 600
    http_port: int = 52010

    # Optional bearer token guarding the session-ingest endpoint. When empty the
    # endpoint is open (relies on network-level isolation). Set it to require
    # ``Authorization: Bearer <token>`` on POST /ingest_session so remote
    # machines can push sessions without holding any OpenViking credentials.
    ingest_api_key: str = ""

    # Local persistence
    history_path: str = "evolve_history.jsonl"
    processed_log_path: str = "evolve_processed.json"

    def __post_init__(self) -> None:
        self.engine = str(self.engine or "workflow").strip().lower() or "workflow"
        self.skill_verifier_min_score = max(
            0.0,
            min(1.0, float(self.skill_verifier_min_score or 0.0)),
        )
        self.skill_dedup_max_similarity = max(
            0.0,
            min(1.0, float(self.skill_dedup_max_similarity or 0.0)),
        )
        self.skill_dedup_shortlist_size = max(1, int(self.skill_dedup_shortlist_size or 1))
        self.evidence_max_entries = max(1, int(self.evidence_max_entries or 1))
        self.evidence_recent_limit = max(1, int(self.evidence_recent_limit or 1))
        self.evidence_historical_limit = max(0, int(self.evidence_historical_limit or 0))
        self.evidence_replay_cases_per_window = max(
            1, int(self.evidence_replay_cases_per_window or 1)
        )
        self.evidence_change_debt_threshold = max(
            1, int(self.evidence_change_debt_threshold or 1)
        )
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
        self.validation_required_results = max(1, int(self.validation_required_results or 1))
        self.validation_required_approvals = max(1, int(self.validation_required_approvals or 1))
        self.validation_min_mean_score = max(
            0.0,
            min(1.0, float(self.validation_min_mean_score or 0.0)),
        )
        self.validation_max_rejections = max(1, int(self.validation_max_rejections or 1))

    @classmethod
    def from_env(cls) -> "EvolveServerConfig":
        """Populate every field from environment variables."""
        storage_endpoint = _first_env("EVOLVE_STORAGE_ENDPOINT")
        local_root = _first_env("EVOLVE_STORAGE_LOCAL_ROOT", "EVOLVE_LOCAL_ROOT")
        storage_backend = _infer_storage_backend(storage_endpoint, local_root)
        engine = _first_env("EVOLVE_ENGINE", default="workflow").strip().lower() or "workflow"

        llm_api_key = os.environ.get("OPENAI_API_KEY", "")
        llm_base_url = os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1")
        llm_model = os.environ.get("EVOLVE_MODEL", "gpt-4o")
        llm_api_type = os.environ.get("EVOLVE_LLM_API_TYPE", "openai-completions")

        return cls(
            engine=engine,
            storage_backend=storage_backend,
            storage_endpoint=storage_endpoint,
            local_root=local_root,
            viking_endpoint=os.environ.get("EVOLVE_VIKING_ENDPOINT", ""),
            viking_api_key=_first_env("EVOLVE_VIKING_TEAM_API_KEY", "EVOLVE_VIKING_API_KEY"),
            viking_account=os.environ.get("EVOLVE_VIKING_ACCOUNT", "default"),
            viking_user=os.environ.get("EVOLVE_VIKING_USER", "default"),
            viking_agent=os.environ.get("EVOLVE_VIKING_AGENT", "team-skill-evolver"),
            viking_agent_id=_first_env("EVOLVE_VIKING_AGENT_ID", "EVOLVE_VIKING_USER_ID"),
            viking_customer_id=_first_env("EVOLVE_VIKING_CUSTOMER_ID", "EVOLVE_VIKING_PEER_ID"),
            viking_root_prefix=os.environ.get("EVOLVE_VIKING_ROOT_PREFIX", "team-skill-evolver"),
            viking_group_id=_first_env("EVOLVE_VIKING_GROUP_ID", "EVOLVE_VIKING_GROUP", default=""),
            llm_api_key=llm_api_key,
            llm_base_url=llm_base_url,
            llm_model=llm_model,
            llm_max_tokens=int(os.environ.get("EVOLVE_LLM_MAX_TOKENS", "100000")),
            llm_temperature=float(os.environ.get("EVOLVE_LLM_TEMPERATURE", "0.4")),
            llm_api_type=llm_api_type,
            evolve_strategy=os.environ.get("EVOLVE_STRATEGY", "dynamic_edit_conservative"),
            use_success_feedback=os.environ.get("EVOLVE_USE_SUCCESS_FEEDBACK", "1").lower() not in {"0", "false", "no"},
            evolve_batch_size=int(os.environ.get("EVOLVE_BATCH_SIZE", "20")),
            reject_rewrite=os.environ.get("EVOLVE_REJECT_REWRITE", "0").lower() in {"1", "true", "yes"},
            use_session_judge=os.environ.get("EVOLVE_USE_SESSION_JUDGE", "1").lower() not in {"0", "false", "no"},
            use_skill_verifier=os.environ.get("EVOLVE_USE_SKILL_VERIFIER", "1").lower() not in {"0", "false", "no"},
            skill_verifier_min_score=float(os.environ.get("EVOLVE_SKILL_VERIFIER_MIN_SCORE", "0.75")),
            use_skill_dedup=os.environ.get("EVOLVE_USE_SKILL_DEDUP", "1").lower() not in {"0", "false", "no"},
            skill_dedup_max_similarity=float(os.environ.get("EVOLVE_SKILL_DEDUP_MAX_SIMILARITY", "0.8")),
            skill_dedup_shortlist_size=int(os.environ.get("EVOLVE_SKILL_DEDUP_SHORTLIST_SIZE", "5")),
            evidence_enabled=os.environ.get("EVOLVE_EVIDENCE_ENABLED", "1").lower() not in {"0", "false", "no"},
            evidence_max_entries=int(os.environ.get("EVOLVE_EVIDENCE_MAX_ENTRIES", "200")),
            evidence_recent_limit=int(os.environ.get("EVOLVE_EVIDENCE_RECENT_LIMIT", "12")),
            evidence_historical_limit=int(os.environ.get("EVOLVE_EVIDENCE_HISTORICAL_LIMIT", "12")),
            evidence_replay_cases_per_window=int(
                os.environ.get("EVOLVE_EVIDENCE_REPLAY_CASES_PER_WINDOW", "1")
            ),
            evidence_change_debt_threshold=int(
                os.environ.get("EVOLVE_EVIDENCE_CHANGE_DEBT_THRESHOLD", "3")
            ),
            candidate_coalesce_enabled=os.environ.get(
                "EVOLVE_CANDIDATE_COALESCE_ENABLED", "1"
            ).lower()
            not in {"0", "false", "no"},
            bundle_text_extensions=os.environ.get(
                "EVOLVE_BUNDLE_TEXT_EXTENSIONS", ".py,.sh"
            ).split(","),
            bundle_max_file_bytes=int(
                os.environ.get("EVOLVE_BUNDLE_MAX_FILE_BYTES", "65536")
            ),
            bundle_max_prompt_bytes=int(
                os.environ.get("EVOLVE_BUNDLE_MAX_PROMPT_BYTES", "262144")
            ),
            bundle_allow_delete=os.environ.get(
                "EVOLVE_BUNDLE_ALLOW_DELETE", "1"
            ).lower()
            not in {"0", "false", "no"},
            bundle_static_checks_enabled=os.environ.get(
                "EVOLVE_BUNDLE_STATIC_CHECKS_ENABLED", "1"
            ).lower()
            not in {"0", "false", "no"},
            publish_mode=os.environ.get("EVOLVE_PUBLISH_MODE", "validated"),
            validation_required_results=int(os.environ.get("EVOLVE_VALIDATION_REQUIRED_RESULTS", "3")),
            validation_required_approvals=int(os.environ.get("EVOLVE_VALIDATION_REQUIRED_APPROVALS", "2")),
            validation_min_mean_score=float(os.environ.get("EVOLVE_VALIDATION_MIN_MEAN_SCORE", "0.75")),
            validation_max_rejections=int(os.environ.get("EVOLVE_VALIDATION_MAX_REJECTIONS", "1")),
            human_review_enabled=os.environ.get("EVOLVE_HUMAN_REVIEW_ENABLED", "1").lower() not in {"0", "false", "no"},
            human_review_pending_timeout_seconds=int(os.environ.get("EVOLVE_HUMAN_REVIEW_TIMEOUT_SECONDS", "86400")),
            interval_seconds=int(os.environ.get("EVOLVE_INTERVAL", "600")),
            http_port=int(os.environ.get("EVOLVE_PORT", "52010")),
            ingest_api_key=os.environ.get("EVOLVE_INGEST_API_KEY", ""),
            history_path=os.environ.get("EVOLVE_HISTORY_LOG", "evolve_history.jsonl"),
            processed_log_path=os.environ.get("EVOLVE_PROCESSED_LOG", "evolve_processed.json"),
        )

    @classmethod
    def from_teamEvolver_config(cls, config) -> "EvolveServerConfig":
        """Build from teamEvolver's primary configuration object."""
        engine = _first_env("EVOLVE_ENGINE", default="workflow").strip().lower() or "workflow"
        sharing_backend = str(getattr(config, "sharing_backend", "") or "").strip().lower()
        local_root = str(getattr(config, "sharing_local_root", "") or os.environ.get("EVOLVE_LOCAL_ROOT", ""))
        viking_endpoint = str(getattr(config, "sharing_viking_endpoint", "") or "")
        storage_endpoint = viking_endpoint
        llm_api_key = str(getattr(config, "llm_api_key", "") or "")
        llm_base_url = str(
            getattr(config, "llm_api_base", "")
            or "https://ark.cn-beijing.volces.com/api/v3"
        )
        llm_model = os.environ.get(
            "EVOLVE_MODEL",
            str(getattr(config, "llm_model_id", "") or "doubao-seed-evolving"),
        )
        llm_api_type = os.environ.get("EVOLVE_LLM_API_TYPE", "openai-completions")
        llm_max_tokens = int(
            os.environ.get(
                "EVOLVE_LLM_MAX_TOKENS",
                str(getattr(config, "llm_max_tokens", 100000) or 100000),
            )
        )
        llm_temperature = float(
            os.environ.get(
                "EVOLVE_LLM_TEMPERATURE",
                str(getattr(config, "llm_temperature", 0.4)),
            )
        )

        storage_backend = _first_env("EVOLVE_STORAGE_BACKEND", default="")
        if not storage_backend:
            if local_root:
                storage_backend = "local"
            elif viking_endpoint:
                storage_backend = "viking"
            elif sharing_backend in {"local", "viking"}:
                storage_backend = sharing_backend

        return cls(
            engine=engine,
            storage_backend=storage_backend,
            storage_endpoint=storage_endpoint,
            local_root=local_root,
            viking_endpoint=viking_endpoint,
            viking_api_key=str(
                getattr(config, "sharing_viking_team_api_key", "")
                or getattr(config, "sharing_viking_api_key", "")
                or ""
            ),
            viking_account=str(getattr(config, "sharing_viking_account", "") or "default"),
            viking_user=str(getattr(config, "sharing_viking_user", "") or "default"),
            viking_agent=str(getattr(config, "sharing_viking_agent", "") or "team-skill-evolver"),
            viking_agent_id=str(getattr(config, "sharing_viking_agent_id", "") or ""),
            viking_customer_id=str(getattr(config, "sharing_viking_customer_id", "") or ""),
            viking_root_prefix=str(getattr(config, "sharing_viking_root_prefix", "") or "team-skill-evolver"),
            viking_group_id=str(getattr(config, "sharing_viking_group_id", "") or ""),
            llm_api_key=llm_api_key,
            llm_base_url=llm_base_url,
            llm_model=llm_model,
            llm_max_tokens=llm_max_tokens,
            llm_temperature=llm_temperature,
            llm_api_type=llm_api_type,
            evolve_strategy=os.environ.get("EVOLVE_STRATEGY", "dynamic_edit_conservative"),
            use_success_feedback=os.environ.get("EVOLVE_USE_SUCCESS_FEEDBACK", "1").lower() not in {"0", "false", "no"},
            evolve_batch_size=int(os.environ.get("EVOLVE_BATCH_SIZE", "20")),
            reject_rewrite=os.environ.get("EVOLVE_REJECT_REWRITE", "0").lower() in {"1", "true", "yes"},
            use_session_judge=os.environ.get("EVOLVE_USE_SESSION_JUDGE", "1").lower() not in {"0", "false", "no"},
            use_skill_verifier=os.environ.get("EVOLVE_USE_SKILL_VERIFIER", "1").lower() not in {"0", "false", "no"},
            skill_verifier_min_score=float(os.environ.get("EVOLVE_SKILL_VERIFIER_MIN_SCORE", "0.75")),
            use_skill_dedup=os.environ.get("EVOLVE_USE_SKILL_DEDUP", "1").lower() not in {"0", "false", "no"},
            skill_dedup_max_similarity=float(os.environ.get("EVOLVE_SKILL_DEDUP_MAX_SIMILARITY", "0.8")),
            skill_dedup_shortlist_size=int(os.environ.get("EVOLVE_SKILL_DEDUP_SHORTLIST_SIZE", "5")),
            evidence_enabled=os.environ.get(
                "EVOLVE_EVIDENCE_ENABLED",
                "1" if getattr(config, "evolve_evidence_enabled", True) else "0",
            ).lower()
            not in {"0", "false", "no"},
            evidence_max_entries=int(
                os.environ.get(
                    "EVOLVE_EVIDENCE_MAX_ENTRIES",
                    str(getattr(config, "evolve_evidence_max_entries", 200) or 200),
                )
            ),
            evidence_recent_limit=int(
                os.environ.get(
                    "EVOLVE_EVIDENCE_RECENT_LIMIT",
                    str(getattr(config, "evolve_evidence_recent_limit", 12) or 12),
                )
            ),
            evidence_historical_limit=int(
                os.environ.get(
                    "EVOLVE_EVIDENCE_HISTORICAL_LIMIT",
                    str(getattr(config, "evolve_evidence_historical_limit", 12) or 0),
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
            candidate_coalesce_enabled=os.environ.get(
                "EVOLVE_CANDIDATE_COALESCE_ENABLED",
                "1"
                if getattr(config, "evolve_candidate_coalesce_enabled", True)
                else "0",
            ).lower()
            not in {"0", "false", "no"},
            bundle_text_extensions=list(
                getattr(config, "evolve_bundle_text_extensions", [".py", ".sh"])
                or [".py", ".sh"]
            ),
            bundle_max_file_bytes=int(
                getattr(config, "evolve_bundle_max_file_bytes", 65536) or 65536
            ),
            bundle_max_prompt_bytes=int(
                getattr(config, "evolve_bundle_max_prompt_bytes", 262144)
                or 262144
            ),
            bundle_allow_delete=bool(
                getattr(config, "evolve_bundle_allow_delete", True)
            ),
            bundle_static_checks_enabled=bool(
                getattr(config, "evolve_bundle_static_checks_enabled", True)
            ),
            publish_mode=os.environ.get("EVOLVE_PUBLISH_MODE", "validated"),
            validation_required_results=int(
                os.environ.get(
                    "EVOLVE_VALIDATION_REQUIRED_RESULTS",
                    str(getattr(config, "validation_required_results", 3) or 3),
                )
            ),
            validation_required_approvals=int(
                os.environ.get(
                    "EVOLVE_VALIDATION_REQUIRED_APPROVALS",
                    str(getattr(config, "validation_required_approvals", 2) or 2),
                )
            ),
            validation_min_mean_score=float(os.environ.get("EVOLVE_VALIDATION_MIN_MEAN_SCORE", "0.75")),
            validation_max_rejections=int(os.environ.get("EVOLVE_VALIDATION_MAX_REJECTIONS", "1")),
            human_review_enabled=os.environ.get("EVOLVE_HUMAN_REVIEW_ENABLED", "1").lower() not in {"0", "false", "no"},
            human_review_pending_timeout_seconds=int(os.environ.get("EVOLVE_HUMAN_REVIEW_TIMEOUT_SECONDS", "86400")),
            ingest_api_key=os.environ.get(
                "EVOLVE_INGEST_API_KEY",
                str(getattr(config, "proxy_api_key", "") or ""),
            ),
        )
