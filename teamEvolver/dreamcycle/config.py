"""Configuration management via environment variables + .env file."""

from __future__ import annotations

import base64
import json
import os
from pathlib import Path
from typing import Optional

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class OpenVikingConfig(BaseSettings):
    """OpenViking connection settings.

    DreamCycle maintains USER MEMORY only, bound to the authenticated user it
    acts as (``X-OpenViking-User`` = ``agent_id``). Requests use the server's
    user-relative shorthand, which OpenViking normalizes to that user's own
    subtree:

        viking://user/memories/                    (own memories — maintained)
        viking://user/peers/{customer_id}/memories/ (a single peer's memories)

    Read tools may look across all users; write/archive tools are restricted to
    the authenticated user's own memory (never resources/skills/sessions).
    """
    endpoint: str = Field(default="http://127.0.0.1:1933", alias="OPENVIKING_ENDPOINT")
    api_key: str = Field(default="", alias="OPENVIKING_API_KEY")
    account: str = Field(default="", alias="OPENVIKING_ACCOUNT")
    source_api_keys_raw: str = Field(
        default="",
        alias="OPENVIKING_SOURCE_API_KEYS",
    )
    source_users_raw: str = Field(
        default="",
        alias="OPENVIKING_SOURCE_USERS",
    )
    # The authenticated OpenViking user DreamCycle acts as (X-OpenViking-User).
    # Its own ``viking://user/memories/`` is the maintained space.
    agent_id: str = Field(
        default="",
        validation_alias=AliasChoices("OPENVIKING_AGENT_ID", "OPENVIKING_TEAM_USER"),
    )
    agent: str = Field(default="dreamcycle", alias="OPENVIKING_AGENT")
    # Optional peer id. When set, the maintained space narrows to that single
    # peer's memories ``viking://user/peers/{customer_id}/memories/``. When
    # empty, it maintains the authenticated user's own ``viking://user/memories/``.
    customer_id: str = Field(
        default="",
        validation_alias=AliasChoices("OPENVIKING_CUSTOMER_ID", "OPENVIKING_PEER_ID"),
    )

    model_config = SettingsConfigDict(
        env_prefix="",
        extra="ignore",
        populate_by_name=True,
    )

    def __init__(self, **data):
        source_api_keys = data.pop("source_api_keys", None)
        source_users = data.pop("source_users", None)
        if source_api_keys is not None:
            data["source_api_keys_raw"] = json.dumps(source_api_keys)
        if source_users is not None:
            data["source_users_raw"] = json.dumps(source_users)
        super().__init__(**data)

    def model_post_init(self, __context):
        key_account, key_user = parse_openviking_key(self.api_key)
        if not self.account:
            self.account = key_account
        if not self.agent_id:
            self.agent_id = key_user

    @property
    def source_api_keys(self) -> list[str]:
        raw = str(self.source_api_keys_raw or "").strip()
        if not raw:
            return []
        try:
            parsed = json.loads(raw)
            values = parsed if isinstance(parsed, list) else [str(parsed)]
        except (TypeError, ValueError):
            values = raw.replace("\n", ",").split(",")
        team_key = str(self.api_key or "").strip()
        return list(
            dict.fromkeys(
                value
                for item in values
                if (value := str(item or "").strip()) and value != team_key
            )
        )

    @property
    def source_users(self) -> list[str]:
        raw = str(self.source_users_raw or "").strip()
        if not raw:
            return []
        try:
            parsed = json.loads(raw)
            values = parsed if isinstance(parsed, list) else [str(parsed)]
        except (TypeError, ValueError):
            values = raw.replace("\n", ",").split(",")
        return list(
            dict.fromkeys(
                value
                for item in values
                if (value := str(item or "").strip()) and value != self.agent_id
            )
        )


def parse_openviking_key(value: str) -> tuple[str, str]:
    """Return ``(account, user)`` encoded in an OpenViking key."""
    parts = str(value or "").strip().split(".")
    if len(parts) < 3:
        return "", ""
    decoded: list[str] = []
    for part in parts[:2]:
        try:
            padding = "=" * (-len(part) % 4)
            decoded.append(base64.b64decode(part + padding).decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return "", ""
    return decoded[0].strip(), decoded[1].strip()


class LLMConfig(BaseSettings):
    """LLM provider settings."""
    base_url: str = Field(default="https://ark.cn-beijing.volces.com/api/v3", alias="DREAMCYCLE_LLM_BASE_URL")
    api_key: str = Field(default="", alias="DREAMCYCLE_LLM_API_KEY")
    model: str = Field(default="ep-20260625144348-h9bt9", alias="DREAMCYCLE_MODEL")
    max_tokens: int = Field(default=4096, alias="DREAMCYCLE_LLM_MAX_TOKENS")
    temperature: float = Field(default=0.3, alias="DREAMCYCLE_TEMPERATURE")
    # Embedding backend for semantic duplication detection. Reuses the LLM
    # base_url/api_key by default; set the model to enable semantic dedup.
    embed_model: str = Field(default="", alias="DREAMCYCLE_EMBED_MODEL")
    embed_base_url: str = Field(default="", alias="DREAMCYCLE_EMBED_BASE_URL")
    embed_api_key: str = Field(default="", alias="DREAMCYCLE_EMBED_API_KEY")
    dedup_merge_threshold: float = Field(default=0.86, alias="DREAMCYCLE_DEDUP_MERGE_THRESHOLD")
    dedup_warn_threshold: float = Field(default=0.72, alias="DREAMCYCLE_DEDUP_WARN_THRESHOLD")

    model_config = SettingsConfigDict(
        env_prefix="",
        extra="ignore",
        populate_by_name=True,
    )

    def model_post_init(self, __context):
        # Fallback to OPENAI_* if DREAMCYCLE_* not set
        if not self.base_url or self.base_url == "https://ark.cn-beijing.volces.com/api/v3":
            self.base_url = os.environ.get("OPENAI_BASE_URL", self.base_url)
        if not self.api_key:
            self.api_key = os.environ.get("OPENAI_API_KEY", "")
        # Embedding endpoint defaults to the chat endpoint/key unless overridden.
        if not self.embed_base_url:
            self.embed_base_url = self.base_url
        if not self.embed_api_key:
            self.embed_api_key = self.api_key


class SchedulerConfig(BaseSettings):
    """Scheduling and execution settings."""
    active_start_hour: int = Field(default=0, alias="DREAMCYCLE_START_HOUR")
    active_end_hour: int = Field(default=6, alias="DREAMCYCLE_END_HOUR")
    rounds_per_window: int = Field(default=3, alias="DREAMCYCLE_ROUNDS")
    round_interval_minutes: int = Field(default=90, alias="DREAMCYCLE_ROUND_INTERVAL")
    max_turns_per_job: int = Field(default=25, alias="DREAMCYCLE_MAX_TURNS")
    max_consecutive_errors: int = Field(default=3, alias="DREAMCYCLE_MAX_ERRORS")
    retry_delay_seconds: int = Field(default=300, alias="DREAMCYCLE_RETRY_DELAY")

    model_config = SettingsConfigDict(
        env_prefix="",
        extra="ignore",
        populate_by_name=True,
    )


def _default_data_dir() -> Path:
    """Default data directory — ~/.dreamcycle with expanduser at call time."""
    return Path(os.environ.get("DREAMCYCLE_DATA_DIR", os.path.expanduser("~/.dreamcycle")))


class LogConfig(BaseSettings):
    """Logging settings."""
    log_dir: Path = Field(default_factory=lambda: _default_data_dir() / "logs", alias="DREAMCYCLE_LOG_DIR")
    report_dir: Path = Field(default_factory=lambda: _default_data_dir() / "reports", alias="DREAMCYCLE_REPORT_DIR")
    state_file: Path = Field(default_factory=lambda: _default_data_dir() / "state.json", alias="DREAMCYCLE_STATE_FILE")
    log_level: str = Field(default="INFO", alias="DREAMCYCLE_LOG_LEVEL")
    max_log_size_mb: int = Field(default=50, alias="DREAMCYCLE_MAX_LOG_SIZE")
    log_backup_count: int = Field(default=10, alias="DREAMCYCLE_LOG_BACKUPS")

    model_config = SettingsConfigDict(
        env_prefix="",
        extra="ignore",
        populate_by_name=True,
    )


class DreamCycleConfig:
    """Composite configuration — loads all sub-configs."""

    def __init__(
        self,
        env_file: Optional[str] = None,
        *,
        viking: Optional[OpenVikingConfig] = None,
        llm: Optional[LLMConfig] = None,
        scheduler: Optional[SchedulerConfig] = None,
        log: Optional[LogConfig] = None,
        job_prompts: Optional[dict[str, str]] = None,
        job_settings: Optional[dict[str, dict]] = None,
        team_name: Optional[str] = None,
    ):
        if env_file and Path(env_file).exists():
            from dotenv import load_dotenv
            load_dotenv(env_file, override=True)
        elif Path(".env").exists():
            from dotenv import load_dotenv
            load_dotenv(".env", override=True)

        self.viking = viking or OpenVikingConfig()
        self.llm = llm or LLMConfig()
        self.scheduler = scheduler or SchedulerConfig()
        self.log = log or LogConfig()
        self.team_name = str(
            team_name
            if team_name is not None
            else (
                os.environ.get("EVOLVE_TEAM_DISPLAY_NAME")
                or os.environ.get("DREAMCYCLE_TEAM_NAME")
                or ""
            )
        ).strip()
        self.job_prompts = dict(job_prompts or {})
        self.job_settings = {
            str(job_id): dict(settings)
            for job_id, settings in (job_settings or {}).items()
            if isinstance(settings, dict)
        }

        # Ensure directories exist
        self.log.log_dir.mkdir(parents=True, exist_ok=True)
        self.log.report_dir.mkdir(parents=True, exist_ok=True)
        self.log.state_file.parent.mkdir(parents=True, exist_ok=True)

    def build_semantic_matcher(self):
        """Construct the embedding-backed duplication matcher.

        Returns a :class:`SemanticMatcher`; it is disabled (verdict ``unknown``)
        when no embedding model is configured, so dedup never falls back to
        lexical token overlap.
        """
        from .semantic import SemanticMatcher, make_openai_embedder

        embed_fn = make_openai_embedder(
            base_url=self.llm.embed_base_url,
            api_key=self.llm.embed_api_key,
            model=self.llm.embed_model,
        )
        return SemanticMatcher(
            embed_fn,
            merge_threshold=self.llm.dedup_merge_threshold,
            warn_threshold=self.llm.dedup_warn_threshold,
        )
