"""Orchestration for cross-user memory aggregation.

Pipeline (all steps below use the OpenViking Root/Admin credential resolved
for the current request):

1. Resolve account users via the Admin API.
2. Resolve or bootstrap one account-shared aggregation Skill and pin its
   content-addressed revision for the whole run.
3. Copy changed users into deterministic private snapshots without invoking a
   model or Skill.
4. Apply the pinned Skill while tree-reducing snapshots into the shared
   team-memory target.
5. Persist per-user status for the next incremental run.

The service is transport-agnostic about how it is triggered: the proxy route
runs :meth:`run` inside a background thread and polls :attr:`status`.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import re
import secrets
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional
from urllib.parse import unquote, urlsplit

from .compile_client import CompileClient
from .okf_skill import DEFAULT_OKF_SKILL_BODY, skill_fingerprint
from .sources import (
    DEFAULT_MEMORY_KINDS,
    AccountSourceBuilder,
    AccountUserCredential,
    SourceExpansionError,
)
from .staging import DeterministicStagingClient, StagingError
from .state import AggregationState

logger = logging.getLogger(__name__)

_SNAPSHOT_MERGE_INSTRUCTION = (
    "Inputs may include deterministic per-user snapshot JSONL files. Each JSON "
    "line contains source_uri, relative_path, kind, modified_at, content_sha256, "
    "and the verbatim Memory text in content. Treat content as source material "
    "and preserve source_uri provenance. Inputs from later levels are prior "
    "structured merge outputs."
)


@dataclass
class GroupResult:
    group_key: str
    kind: str
    target_uri: str
    source_count: int
    status: str  # "ok" | "skipped" | "failed"
    detail: str = ""


@dataclass
class AggregationRun:
    task_id: str
    account_id: str
    endpoint: str
    auth_mode: str
    target_uri: str
    work_root: str = ""
    skill_uri: str = ""
    skill_revision: str = ""
    status: str = "pending"  # pending | running | completed | failed
    started_at: float = field(default_factory=time.time)
    finished_at: Optional[float] = None
    groups: list[GroupResult] = field(default_factory=list)
    group_counts: dict[str, int] = field(
        default_factory=lambda: {"ok": 0, "skipped": 0, "failed": 0}
    )
    group_total: int = 0
    groups_truncated: bool = False
    source_user_count: int = 0
    publish_mode: str = "single"
    partition_count: int = 0
    estimated_merge_tasks: int = 0
    error: str = ""

    def to_public(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "account_id": self.account_id,
            "endpoint": self.endpoint,
            "auth_mode": self.auth_mode,
            "target_uri": self.target_uri,
            "work_root": self.work_root,
            "skill_uri": self.skill_uri,
            "skill_revision": self.skill_revision,
            "status": self.status,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "error": self.error,
            "group_counts": dict(self.group_counts),
            "group_total": self.group_total,
            "groups_truncated": self.groups_truncated,
            "source_user_count": self.source_user_count,
            "publish_mode": self.publish_mode,
            "partition_count": self.partition_count,
            "estimated_merge_tasks": self.estimated_merge_tasks,
            "groups": [
                {
                    "group_key": g.group_key,
                    "kind": g.kind,
                    "target_uri": g.target_uri,
                    "source_count": g.source_count,
                    "status": g.status,
                    "detail": g.detail,
                }
                for g in self.groups
            ],
        }


@dataclass(repr=False)
class _ExecutionCredentials:
    users: list[str]
    user_api_keys: dict[str, str]
    merge_user_id: str
    merge_api_key: str


class MemoryAggregationService:
    """Coordinate account-wide memory aggregation via ov compile."""

    def __init__(self, config: Any):
        self.config = config
        self._runs: dict[str, AggregationRun] = {}
        self._lock = threading.Lock()
        self._compile_slots = threading.BoundedSemaphore(self._merge_concurrency())

    # ---- config accessors ------------------------------------------------ #

    def _endpoint(self) -> str:
        return str(getattr(self.config, "sharing_viking_endpoint", "") or "").rstrip("/")

    @staticmethod
    def normalize_endpoint(endpoint: str) -> str:
        """Validate and normalize an OpenViking HTTP endpoint."""
        value = str(endpoint or "").strip().rstrip("/")
        parsed = urlsplit(value)
        try:
            parsed.port
        except ValueError as exc:
            raise ValueError("endpoint must be a valid HTTP(S) URL") from exc
        decoded_path = unquote(parsed.path)
        if (
            len(value) > 2048
            or parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or any(char.isspace() or ord(char) < 32 for char in value)
            or "\\" in decoded_path
            or any(
                segment in {".", ".."}
                or any(char.isspace() or ord(char) < 32 for char in segment)
                for segment in decoded_path.split("/")
            )
        ):
            raise ValueError("endpoint must be a valid HTTP(S) URL")
        return value

    def resolve_endpoint(self, endpoint: Optional[str] = None) -> str:
        """Resolve a run endpoint, falling back to the configured deployment."""
        requested = str(endpoint or "").strip()
        if not requested:
            requested = self._endpoint()
        if not requested:
            raise ValueError("endpoint is required")
        return self.normalize_endpoint(requested)

    def _team_user(self) -> str:
        return str(getattr(self.config, "sharing_viking_user", "") or "team")

    def _prefix(self) -> str:
        return str(
            getattr(self.config, "aggregation_shared_knowledge_prefix", "")
            or "shared-knowledge"
        )

    def _skill_name(self) -> str:
        # The OKF Skill is published once under the account-shared agent scope.
        from .okf_skill import DEFAULT_OKF_SKILL_NAME

        configured = str(getattr(self.config, "aggregation_okf_skill_uri", "") or "")
        # Accept either a bare name or a full URI; derive the trailing name.
        leaf = configured.rstrip("/").rsplit("/", 1)[-1] if configured else ""
        return leaf or DEFAULT_OKF_SKILL_NAME

    def _shared_skill_uri(self) -> str:
        return f"viking://agent/skills/{self._skill_name()}"

    def _staging_dir(self) -> str:
        return str(getattr(self.config, "aggregation_staging_dir", "") or "_staging")

    @staticmethod
    def normalize_target_uri(target_uri: str) -> str:
        """Validate and normalize a final aggregation target URI."""
        value = str(target_uri or "").strip().rstrip("/")
        parsed = urlsplit(value)
        segments = parsed.path.lstrip("/").split("/") if parsed.path else []
        decoded_segments = [unquote(segment) for segment in segments]
        if (
            len(value) > 512
            or parsed.scheme != "viking"
            or parsed.netloc != "resources"
            or parsed.query
            or parsed.fragment
            or not segments
            or any(
                not segment
                or segment in {".", ".."}
                or "/" in segment
                or "\\" in segment
                or any(char.isspace() or ord(char) < 32 for char in segment)
                for segment in decoded_segments
            )
        ):
            raise ValueError(
                "target_uri must be a valid path under "
                "viking://resources/<path>"
            )
        return f"viking://resources/{'/'.join(segments)}"

    def resolve_target_uri(self, target_uri: Optional[str] = None) -> str:
        """Resolve a run target, falling back to the configured default."""
        requested = str(target_uri or "").strip()
        return self.normalize_target_uri(requested or self._target_root())

    @staticmethod
    def _safe_path_segment(value: str, *, field_name: str) -> str:
        segment = str(value or "").strip().strip("/")
        if not re.fullmatch(r"[A-Za-z0-9_.@-]+", segment):
            raise ValueError(f"{field_name} must be a safe OpenViking path segment")
        return segment

    def _work_root(
        self,
        target_uri: Optional[str] = None,
        owner_user_id: Optional[str] = None,
    ) -> str:
        """Return private scratch space owned by the merge identity."""
        owner = self._safe_path_segment(
            owner_user_id or self._team_user(),
            field_name="merge user_id",
        )
        staging = self._safe_path_segment(
            self._staging_dir(),
            field_name="aggregation staging_dir",
        )
        target = self.resolve_target_uri(target_uri)
        target_digest = hashlib.sha256(target.encode("utf-8")).hexdigest()[:20]
        return (
            f"viking://user/{owner}/resources/teamEvolver/"
            f"{staging}/{target_digest}"
        )

    def _staging_uri(
        self,
        user_id: str,
        target_uri: Optional[str] = None,
        *,
        source_fingerprint: str = "",
        work_root: str = "",
    ) -> str:
        source_user = self._safe_path_segment(user_id, field_name="source user_id")
        root = work_root.rstrip("/") or self._work_root(target_uri)
        base = f"{root}/users/{source_user}/snapshots"
        if not source_fingerprint:
            return base
        fingerprint = source_fingerprint.removeprefix("sha256:")
        if not re.fullmatch(r"[0-9a-f]{64}", fingerprint):
            raise ValueError("source fingerprint must be a SHA-256 digest")
        return f"{base}/{fingerprint}"

    def _target_root(self) -> str:
        prefix = self._prefix().strip("/")
        return f"viking://resources/{prefix}"

    def _kinds(self, requested: Optional[list[str]]) -> list[str]:
        if requested:
            return [k.strip() for k in requested if k and k.strip()]
        configured = getattr(self.config, "aggregation_kinds", None)
        if isinstance(configured, (list, tuple)) and configured:
            return [str(k).strip() for k in configured if str(k).strip()]
        return list(DEFAULT_MEMORY_KINDS)

    def _state_path(
        self,
        account_id: str,
        target_uri: Optional[str] = None,
        endpoint: Optional[str] = None,
        auth_mode: str = "trusted",
    ) -> Path:
        base = str(getattr(self.config, "aggregation_state_dir", "") or "").strip()
        root = Path(base).expanduser() if base else Path.home() / ".teamEvolver" / "aggregation"
        configured_account = str(
            getattr(self.config, "sharing_viking_account", "") or "default"
        )
        resolved_target = self.resolve_target_uri(target_uri)
        resolved_endpoint = self.resolve_endpoint(endpoint)
        configured_endpoint = (
            self.normalize_endpoint(self._endpoint()) if self._endpoint() else ""
        )
        legacy_target = "viking://resources/shared-knowledge"
        safe_account = re.sub(r"[^A-Za-z0-9._-]+", "-", account_id).strip("-")
        if (
            account_id == configured_account
            and resolved_target == legacy_target
            and resolved_endpoint == configured_endpoint
            and auth_mode == "trusted"
        ):
            return root / f"state-{safe_account or 'default'}.json"
        identity = (
            f"{resolved_endpoint}\0{account_id}\0{resolved_target}\0{auth_mode}"
        )
        digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:10]
        return root / f"state-{safe_account or 'account'}-{digest}.json"

    def _max_users_per_batch(self) -> int:
        return int(getattr(self.config, "aggregation_max_users_per_batch", 12) or 12)

    def _account_user_limit(self) -> int:
        return max(
            1,
            int(getattr(self.config, "aggregation_account_user_limit", 50_000) or 50_000),
        )

    def _account_user_page_size(self) -> int:
        return max(
            1,
            min(
                1_000,
                int(
                    getattr(self.config, "aggregation_account_user_page_size", 1_000)
                    or 1_000
                ),
            ),
        )

    def _phase1_concurrency(self) -> int:
        return max(1, int(getattr(self.config, "aggregation_phase1_concurrency", 6) or 6))

    def _merge_fan_in(self) -> int:
        return max(2, min(15, int(getattr(self.config, "aggregation_merge_fan_in", 4) or 4)))

    def _merge_concurrency(self) -> int:
        return max(1, int(getattr(self.config, "aggregation_merge_concurrency", 4) or 4))

    def _partition_threshold(self) -> int:
        return max(
            16,
            int(getattr(self.config, "aggregation_partition_threshold", 512) or 512),
        )

    def _partition_count(self) -> int:
        return max(
            16,
            min(
                1_024,
                int(getattr(self.config, "aggregation_partition_count", 256) or 256),
            ),
        )

    def _run_detail_limit(self) -> int:
        return max(
            100,
            int(getattr(self.config, "aggregation_run_detail_limit", 2_000) or 2_000),
        )

    def _runtime_timeout(self) -> float:
        return float(
            getattr(self.config, "aggregation_compile_runtime_timeout_seconds", 3000)
            or 3000
        )

    @staticmethod
    def _merge_input_fingerprint(
        sources: list[tuple[str, str]],
        *,
        skill_revision: str,
    ) -> str:
        digest = hashlib.sha256()
        digest.update(b"team-memory-merge-v1\0")
        digest.update(skill_revision.encode("utf-8"))
        for uri, fingerprint in sources:
            digest.update(b"\0")
            digest.update(uri.encode("utf-8"))
            digest.update(b"\0")
            digest.update(fingerprint.encode("utf-8"))
        return "sha256:" + digest.hexdigest()

    def _partition_staged_roots(
        self,
        staged_roots: list[str],
    ) -> dict[int, list[str]]:
        """Assign staging roots to stable hash partitions."""
        partition_count = self._partition_count()
        partitions: dict[int, list[str]] = {}
        for uri in staged_roots:
            match = re.search(r"/users/([^/]+)/snapshots/", uri)
            partition_key = match.group(1) if match else uri
            digest = hashlib.sha256(partition_key.encode("utf-8")).digest()
            partition = int.from_bytes(digest[:8], "big") % partition_count
            partitions.setdefault(partition, []).append(uri)
        return {
            partition: sorted(uris)
            for partition, uris in sorted(partitions.items())
        }

    def _tree_compile_task_count(self, source_count: int) -> int:
        """Return compile task count for one bounded-fan-in reduction."""
        if source_count <= 0:
            return 0
        fan_in = self._merge_fan_in()
        tasks = 0
        remaining = source_count
        while remaining > fan_in:
            remaining = (remaining + fan_in - 1) // fan_in
            tasks += remaining
        return tasks + 1

    def _staging_source_fingerprints(
        self,
        *,
        run: "AggregationRun",
        state: "AggregationState",
        staged_roots: list[str],
    ) -> dict[str, str]:
        requested = set(staged_roots)
        fingerprints: dict[str, str] = {}
        for group_key, entry in state.groups.items():
            if not group_key.startswith("stage:") or not isinstance(entry, dict):
                continue
            staging_uri = str(entry.get("staging_uri") or "")
            if staging_uri in requested:
                fingerprints[staging_uri] = str(
                    entry.get("source_fingerprint") or ""
                )
        return fingerprints

    # ---- public API ------------------------------------------------------ #
    def _skill_body_path(self) -> Path:
        base = str(getattr(self.config, "aggregation_state_dir", "") or "").strip()
        root = Path(base).expanduser() if base else Path.home() / ".teamEvolver" / "aggregation"
        return root / "okf_skill.md"

    def skill_body(self) -> str:
        """Return the effective OKF skill body (user override on disk, or default)."""
        try:
            text = self._skill_body_path().read_text(encoding="utf-8")
            if text.strip():
                return text
        except OSError:
            pass
        return DEFAULT_OKF_SKILL_BODY

    def save_skill_body(self, body: str) -> None:
        """Persist a user-edited OKF skill body for subsequent runs."""
        if not body.strip():
            raise ValueError("skill body must not be empty")
        path = self._skill_body_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")

    async def _ensure_shared_skill(self, client: CompileClient) -> dict[str, Any]:
        """Return the published shared Skill, creating it from the fallback once."""
        current = await client.get_skill(skill_name=self._skill_name())
        if not current.get("ok") and int(current.get("exit_code") or 0) == 404:
            current = await client.publish_shared_skill(
                skill_name=self._skill_name(),
                skill_body=self.skill_body(),
                version_message="Publish TeamEvolver aggregation Skill",
            )
        if not current.get("ok"):
            detail = current.get("stderr") or current.get("stdout") or "unknown error"
            raise SourceExpansionError(f"shared aggregation Skill is unavailable: {detail}")
        skill = current.get("result") or {}
        body = str(skill.get("content") or "")
        revision = str(skill.get("revision") or "")
        if not body or not revision:
            raise SourceExpansionError(
                "shared aggregation Skill response is missing content or revision"
            )
        return skill

    async def publish_shared_skill(
        self,
        *,
        body: str,
        endpoint: str,
        account_id: str,
        api_key: str,
        user_id: str,
        version_message: str,
    ) -> dict[str, Any]:
        """Publish one shared aggregation Skill revision and persist its fallback."""
        if not body.strip():
            raise ValueError("skill body must not be empty")
        client = CompileClient(
            endpoint=self.resolve_endpoint(endpoint),
            account_id=account_id,
            user_id=user_id,
            api_key=api_key,
            agent_id=str(
                getattr(self.config, "sharing_viking_agent", "")
                or "team-skill-evolver"
            ),
            timeout_seconds=self._runtime_timeout(),
        )
        published = await client.publish_shared_skill(
            skill_name=self._skill_name(),
            skill_body=body,
            version_message=version_message,
        )
        if not published.get("ok"):
            detail = published.get("stderr") or published.get("stdout") or "unknown error"
            raise ValueError(f"failed to publish shared aggregation Skill: {detail}")
        self.save_skill_body(body)
        return published.get("result") or {}

    async def get_shared_skill(
        self,
        *,
        endpoint: str,
        account_id: str,
        api_key: str,
        user_id: str,
    ) -> Optional[dict[str, Any]]:
        client = CompileClient(
            endpoint=self.resolve_endpoint(endpoint),
            account_id=account_id,
            user_id=user_id,
            api_key=api_key,
            agent_id=str(
                getattr(self.config, "sharing_viking_agent", "")
                or "team-skill-evolver"
            ),
            timeout_seconds=self._runtime_timeout(),
        )
        result = await client.get_skill(skill_name=self._skill_name())
        if result.get("ok"):
            return result.get("result") or {}
        if int(result.get("exit_code") or 0) == 404:
            return None
        detail = result.get("stderr") or result.get("stdout") or "unknown error"
        raise ValueError(f"failed to read shared aggregation Skill: {detail}")


    def status(self, task_id: str) -> Optional[dict[str, Any]]:
        with self._lock:
            run = self._runs.get(task_id)
            return run.to_public() if run else None

    def new_run(
        self,
        account_id: str,
        *,
        target_uri: Optional[str] = None,
        endpoint: Optional[str] = None,
        auth_mode: str = "trusted",
    ) -> AggregationRun:
        if auth_mode not in {"trusted", "api_key"}:
            raise ValueError("auth_mode must be trusted or api_key")
        task_id = f"agg_{secrets.token_urlsafe(24)}"
        run = AggregationRun(
            task_id=task_id,
            account_id=account_id,
            endpoint=self.resolve_endpoint(endpoint),
            auth_mode=auth_mode,
            target_uri=self.resolve_target_uri(target_uri),
        )
        with self._lock:
            self._runs[task_id] = run
        return run

    def list_runs(self, limit: int = 20) -> list[dict[str, Any]]:
        """Return recent runs (newest first) for refresh recovery."""
        with self._lock:
            runs = sorted(
                self._runs.values(), key=lambda r: r.started_at, reverse=True
            )[: max(1, limit)]
            return [r.to_public() for r in runs]

    async def list_account_users(
        self,
        account_id: str,
        *,
        api_key: str,
        endpoint: Optional[str] = None,
    ) -> list[str]:
        """List aggregatable users under an account with a request credential."""
        builder = AccountSourceBuilder(
            endpoint=self.resolve_endpoint(endpoint),
            api_key=str(api_key or "").strip(),
            account_id=account_id,
            shared_knowledge_prefix=self._prefix(),
            max_users_per_batch=self._max_users_per_batch(),
            account_user_limit=self._account_user_limit(),
            account_user_page_size=self._account_user_page_size(),
            excluded_user_ids=frozenset({self._team_user()}),
        )
        return await builder.list_account_users()

    def run(
        self,
        run: AggregationRun,
        *,
        kinds: Optional[list[str]] = None,
        full: bool = False,
        user_ids: Optional[list[str]] = None,
        api_key: str,
    ) -> None:
        """Execute the aggregation synchronously (call inside a worker thread)."""
        run.status = "running"
        try:
            credential = str(api_key or "").strip()
            if not credential:
                raise ValueError("OpenViking API key is required")
            self._run_inner(
                run,
                kinds=kinds,
                full=full,
                user_ids=user_ids,
                api_key=credential,
            )
            run.status = "completed"
        except SourceExpansionError as exc:
            run.status = "failed"
            run.error = str(exc)
            logger.warning("[aggregation] source expansion failed: %s", exc)
        except Exception as exc:  # noqa: BLE001 - surface failure into run state
            run.status = "failed"
            run.error = str(exc)
            logger.exception("[aggregation] run failed")
        finally:
            run.finished_at = time.time()

    # ---- internals ------------------------------------------------------- #

    def _run_inner(
        self,
        run: "AggregationRun",
        *,
        kinds,
        full: bool,
        user_ids=None,
        api_key: str,
    ) -> None:
        import asyncio

        endpoint = run.endpoint
        agent_id = str(getattr(self.config, "sharing_viking_agent", "") or "team-skill-evolver")
        builder = AccountSourceBuilder(
            endpoint=endpoint,
            api_key=api_key,
            account_id=run.account_id,
            shared_knowledge_prefix=run.target_uri.removeprefix(
                "viking://resources/"
            ),
            max_users_per_batch=self._max_users_per_batch(),
            account_user_limit=self._account_user_limit(),
            account_user_page_size=self._account_user_page_size(),
            excluded_user_ids=frozenset({self._team_user()}),
        )
        records = asyncio.run(builder.list_account_user_credentials())
        credentials = self._resolve_execution_credentials(
            run=run,
            records=records,
            requested_user_ids=user_ids,
            bootstrap_api_key=api_key,
        )
        users = credentials.users
        run.source_user_count = len(users)
        run.work_root = self._work_root(
            run.target_uri,
            credentials.merge_user_id,
        )

        # Publish at most once, then pin every compile in this run to the same
        # account-shared Skill package revision.
        team_client = CompileClient(
            endpoint=endpoint,
            account_id=run.account_id,
            user_id=credentials.merge_user_id,
            api_key=credentials.merge_api_key,
            agent_id=agent_id,
            timeout_seconds=self._runtime_timeout(),
        )
        shared_skill = asyncio.run(self._ensure_shared_skill(team_client))
        skill_body = str(shared_skill["content"])
        run.skill_uri = self._shared_skill_uri()
        run.skill_revision = str(shared_skill["revision"])
        skill_fp = skill_fingerprint(skill_body)
        kinds = self._kinds(kinds)
        state = AggregationState.load(
            self._state_path(
                run.account_id,
                run.target_uri,
                run.endpoint,
                run.auth_mode,
            ),
            run.account_id,
        )
        skill_changed = state.skill_fingerprint != skill_fp

        # Phase 1 is a deterministic snapshot. It never loads or executes the
        # aggregation Skill, so Skill changes do not invalidate user staging.
        staged_roots = asyncio.run(
            self._run_pipeline(
                run=run,
                users=users,
                user_api_keys=credentials.user_api_keys,
                target_user_id=credentials.merge_user_id,
                target_api_key=credentials.merge_api_key,
                kinds=kinds,
                endpoint=endpoint,
                agent_id=agent_id,
                state=state,
                force_all=full,
            )
        )

        # Persist state (fingerprints + per-user status) for incremental reruns.
        state.save(skill_fingerprint=skill_fp)

        failed_staging = [
            group_key
            for user_id in users
            if (
                isinstance(
                    entry := state.groups.get(group_key := f"stage:{user_id}"),
                    dict,
                )
                and entry.get("status") == "failed"
            )
        ]
        if failed_staging:
            self._append_group(
                run,
                GroupResult(
                    group_key="merge",
                    kind="(all)",
                    target_uri=run.target_uri,
                    source_count=0,
                    status="failed",
                    detail=(
                        "merge not started because staging failed for: "
                        + ", ".join(failed_staging[:10])
                    ),
                ),
            )
            raise SourceExpansionError(
                "staging phase incomplete; rerun will retry failed users"
            )

        if not staged_roots:
            self._append_group(
                run,
                GroupResult(
                    group_key="merge", kind="(all)", target_uri=run.target_uri,
                    source_count=0, status="skipped", detail="no staged sources",
                ),
            )
            return

        # The pinned Skill is applied only while merging staged snapshots.
        merge_completed = asyncio.run(
            self._merge_staged_roots(
                run=run,
                staged_roots=staged_roots,
                client=team_client,
                skill_uri=run.skill_uri,
                skill_revision=run.skill_revision,
                state=state,
                force_all=full or skill_changed,
            )
        )
        if not merge_completed:
            raise SourceExpansionError(
                "merge phase incomplete; rerun will reuse successful groups "
                "and retry failed groups"
            )

    def _resolve_execution_credentials(
        self,
        *,
        run: "AggregationRun",
        records: list[AccountUserCredential],
        requested_user_ids,
        bootstrap_api_key: str,
    ) -> _ExecutionCredentials:
        by_user = {record.user_id: record for record in records if record.user_id}
        users = sorted(
            user_id
            for user_id in by_user
            if user_id != self._team_user()
        )
        if requested_user_ids:
            allow = {
                str(user_id).strip()
                for user_id in requested_user_ids
                if str(user_id).strip()
            }
            users = [user_id for user_id in users if user_id in allow]
        if not users:
            raise SourceExpansionError(
                f"no aggregatable users under account '{run.account_id}'"
            )

        if run.auth_mode == "trusted":
            return _ExecutionCredentials(
                users=users,
                user_api_keys={user_id: bootstrap_api_key for user_id in users},
                merge_user_id=self._team_user(),
                merge_api_key=bootstrap_api_key,
            )

        missing = [
            user_id
            for user_id in users
            if not by_user[user_id].api_key
        ]
        if missing:
            preview = ", ".join(missing[:10])
            suffix = f" (+{len(missing) - 10} more)" if len(missing) > 10 else ""
            raise SourceExpansionError(
                "api_key mode requires plaintext per-user API keys from "
                f"admin list-users; unavailable for: {preview}{suffix}. "
                "API key hashing may be enabled; key rotation was not attempted."
            )

        admin_record = next(
            (
                record
                for record in records
                if record.role == "admin"
                and record.api_key
                and hmac.compare_digest(record.api_key, bootstrap_api_key)
            ),
            None,
        )
        if admin_record is None:
            raise SourceExpansionError(
                "admin_key owner could not be identified from list-users; "
                "plaintext API keys are required and key rotation was not attempted"
            )

        return _ExecutionCredentials(
            users=users,
            user_api_keys={
                user_id: by_user[user_id].api_key
                for user_id in users
            },
            merge_user_id=admin_record.user_id,
            merge_api_key=bootstrap_api_key,
        )

    async def _run_pipeline(
        self,
        *,
        run: "AggregationRun",
        users: list,
        user_api_keys: dict[str, str],
        target_user_id: str,
        target_api_key: str,
        kinds: list,
        endpoint: str,
        agent_id: str,
        state: "AggregationState",
        force_all: bool,
    ) -> list:
        """Phase 1: stage users with a fixed-size worker pool."""

        async def stage(user_id: str) -> Optional[str]:
            return await self._stage_one_user(
                run=run,
                user_id=user_id,
                kinds=kinds,
                endpoint=endpoint,
                api_key=user_api_keys[user_id],
                target_user_id=target_user_id,
                target_api_key=target_api_key,
                agent_id=agent_id,
                state=state,
                force_all=force_all,
            )

        results = await self._bounded_map(
            users,
            concurrency=self._phase1_concurrency(),
            operation=stage,
        )
        return [r for r in results if r]

    @staticmethod
    async def _bounded_map(
        items: list[Any],
        *,
        concurrency: int,
        operation,
    ) -> list[Any]:
        """Map an async operation without creating one Task per input item."""
        if not items:
            return []
        worker_count = min(max(1, concurrency), len(items))
        queue: asyncio.Queue[Any] = asyncio.Queue(maxsize=worker_count * 2)
        sentinel = object()
        results: list[Any] = [None] * len(items)
        errors: list[Exception] = []

        async def worker() -> None:
            while True:
                item = await queue.get()
                try:
                    if item is sentinel:
                        return
                    index, value = item
                    try:
                        results[index] = await operation(value)
                    except Exception as exc:  # noqa: BLE001 - re-raised after draining
                        errors.append(exc)
                finally:
                    queue.task_done()

        workers = [
            asyncio.create_task(worker(), name=f"aggregation-worker-{index}")
            for index in range(worker_count)
        ]
        try:
            for index, item in enumerate(items):
                await queue.put((index, item))
            for _worker in workers:
                await queue.put(sentinel)
            await queue.join()
            await asyncio.gather(*workers)
        finally:
            for worker_task in workers:
                if not worker_task.done():
                    worker_task.cancel()
            await asyncio.gather(*workers, return_exceptions=True)
        if errors:
            raise errors[0]
        return results

    async def _run_compile(self, client: "CompileClient", **kwargs: Any) -> dict[str, Any]:
        while not self._compile_slots.acquire(blocking=False):
            await asyncio.sleep(0.1)
        try:
            return await client.run_batch(**kwargs)
        finally:
            self._compile_slots.release()

    async def _stage_one_user(
        self,
        *,
        run: "AggregationRun",
        user_id: str,
        kinds: list,
        endpoint: str,
        api_key: str,
        target_user_id: str,
        target_api_key: str,
        agent_id: str,
        state: "AggregationState",
        force_all: bool,
    ) -> Optional[str]:
        """Stage a single user's memory into its staging root. Failure-isolated.

        Returns the staging URI when output exists (fresh or reused), else None.
        """
        group_key = f"stage:{user_id}"
        staging_parent = self._staging_uri(
            user_id,
            run.target_uri,
            work_root=run.work_root,
        )
        client = DeterministicStagingClient(
            endpoint=endpoint,
            account_id=run.account_id,
            source_user_id=user_id,
            source_api_key=api_key,
            target_user_id=target_user_id,
            target_api_key=target_api_key,
            agent_id=agent_id,
            timeout_seconds=self._runtime_timeout(),
        )
        try:
            inventory = await client.inspect(kinds)
        except StagingError as exc:
            state.mark_failed(group_key)
            state.checkpoint(
                group_key,
                skill_fingerprint=state.skill_fingerprint,
            )
            self._append_group(run, GroupResult(
                group_key=group_key,
                kind="(all)",
                target_uri=staging_parent,
                source_count=0,
                status="failed",
                detail=str(exc)[:500],
            ))
            return None

        if not inventory.files:
            state.mark_stage_ok(
                group_key,
                inventory.fingerprint,
                staging_uri="",
                source_count=0,
                total_bytes=0,
            )
            state.checkpoint(
                group_key,
                skill_fingerprint=state.skill_fingerprint,
            )
            self._append_group(run, GroupResult(
                group_key=group_key, kind="(all)", target_uri=staging_parent,
                source_count=0, status="skipped",
                detail="user has no memory in requested kinds",
            ))
            return None

        staging_uri = self._staging_uri(
            user_id,
            run.target_uri,
            source_fingerprint=inventory.fingerprint,
            work_root=run.work_root,
        )
        try:
            reusable = (
                not state.needs_restage(
                    group_key,
                    inventory.fingerprint,
                    staging_uri=staging_uri,
                    full=force_all,
                )
                and await client.snapshot_exists(staging_uri)
            )
        except StagingError as exc:
            state.mark_failed(group_key)
            state.checkpoint(
                group_key,
                skill_fingerprint=state.skill_fingerprint,
            )
            self._append_group(run, GroupResult(
                group_key=group_key,
                kind="(all)",
                target_uri=staging_uri,
                source_count=len(inventory.files),
                status="failed",
                detail=str(exc)[:500],
            ))
            return None
        if reusable:
            prior = state.groups.get(group_key) or {}
            source_count = int(prior.get("source_count") or len(inventory.files))
            self._append_group(run, GroupResult(
                group_key=group_key, kind="(all)", target_uri=staging_uri,
                source_count=source_count,
                status="skipped",
                detail="unchanged (reused deterministic snapshot)",
            ))
            return staging_uri

        try:
            snapshot = await client.publish(
                inventory,
                staging_uri=staging_uri,
                run_id=run.task_id,
            )
        except StagingError as exc:
            state.mark_failed(group_key)
            state.checkpoint(
                group_key,
                skill_fingerprint=state.skill_fingerprint,
            )
            self._append_group(run, GroupResult(
                group_key=group_key, kind="(all)", target_uri=staging_uri,
                source_count=len(inventory.files),
                status="failed",
                detail=str(exc)[:500],
            ))
            return None

        state.mark_stage_ok(
            group_key,
            inventory.fingerprint,
            staging_uri=staging_uri,
            source_count=snapshot.source_count,
            total_bytes=snapshot.total_bytes,
        )
        state.checkpoint(
            group_key,
            skill_fingerprint=state.skill_fingerprint,
        )
        status = "skipped" if snapshot.reused else "ok"
        detail = (
            "deterministic snapshot already exists"
            if snapshot.reused
            else (
                f"copied {snapshot.source_count} source files into "
                f"{snapshot.chunk_count} JSONL chunks"
            )
        )
        self._append_group(run, GroupResult(
            group_key=group_key, kind="(all)", target_uri=staging_uri,
            source_count=snapshot.source_count,
            status=status,
            detail=detail,
        ))
        return staging_uri

    def _append_group(self, run: "AggregationRun", group: "GroupResult") -> None:
        with self._lock:
            run.group_total += 1
            run.group_counts[group.status] = run.group_counts.get(group.status, 0) + 1
            if len(run.groups) < self._run_detail_limit():
                run.groups.append(group)
                return
            run.groups_truncated = True
            if group.status == "failed":
                for index, existing in enumerate(run.groups):
                    if existing.status != "failed":
                        run.groups[index] = group
                        break

    async def _merge_staged_roots(
        self,
        *,
        run: "AggregationRun",
        staged_roots: list[str],
        client: "CompileClient",
        skill_uri: str,
        skill_revision: str,
        state: "AggregationState",
        force_all: bool = False,
    ) -> bool:
        """Publish one root for small runs or stable partitions for large runs."""
        if len(staged_roots) <= self._partition_threshold():
            run.publish_mode = "single"
            run.partition_count = 1
            run.estimated_merge_tasks = self._tree_compile_task_count(
                len(staged_roots)
            )
            completed = await self._tree_reduce_merge(
                run=run,
                staged_roots=staged_roots,
                client=client,
                skill_uri=skill_uri,
                skill_revision=skill_revision,
                force_all=force_all,
                state=state,
                compact_state=False,
            )
            if not completed:
                state.save(skill_fingerprint=state.skill_fingerprint)
                return False
            if state.metadata.get("publish_mode") == "partitioned":
                deleted = await client.delete_uri(
                    uri=f"{run.target_uri}/partitions"
                )
                if not deleted.get("ok"):
                    return False
            state.metadata["publish_mode"] = "single"
            state.metadata.pop("active_partitions", None)
            state.metadata.pop("partition_manifest_fingerprint", None)
            state.save(skill_fingerprint=state.skill_fingerprint)
            return True

        partitions = self._partition_staged_roots(staged_roots)
        run.publish_mode = "partitioned"
        run.partition_count = len(partitions)
        run.estimated_merge_tasks = sum(
            self._tree_compile_task_count(len(roots))
            for roots in partitions.values()
        )
        width = max(2, len(f"{self._partition_count() - 1:x}"))
        partition_plans = [
            (f"{partition:0{width}x}", roots)
            for partition, roots in partitions.items()
        ]

        async def publish_partition(
            plan: tuple[str, list[str]],
        ) -> tuple[str, bool]:
            label, roots = plan
            completed = await self._tree_reduce_merge(
                run=run,
                staged_roots=roots,
                client=client,
                skill_uri=skill_uri,
                skill_revision=skill_revision,
                force_all=force_all,
                state=state,
                target_root=f"{run.target_uri}/partitions/{label}",
                merge_work_root=(
                    f"{run.work_root or self._work_root(run.target_uri)}"
                    f"/_merge/partitions/{label}"
                ),
                group_prefix=f"merge:partition:{label}",
                compact_state=False,
            )
            return label, completed

        outcomes = await self._bounded_map(
            partition_plans,
            concurrency=self._merge_concurrency(),
            operation=publish_partition,
        )
        if not all(completed for _label, completed in outcomes):
            state.save(skill_fingerprint=state.skill_fingerprint)
            return False

        previous_mode = str(state.metadata.get("publish_mode") or "")
        if not previous_mode and isinstance(state.groups.get("merge"), dict):
            previous_mode = "single"
        if previous_mode == "single":
            listed = await client.list_children(uri=run.target_uri)
            if not listed.get("ok"):
                return False
            for entry in listed.get("result") or []:
                child_uri = str(entry.get("uri") or "") if isinstance(entry, dict) else ""
                if (
                    not child_uri
                    or child_uri == f"{run.target_uri}/partitions"
                    or child_uri == f"{run.target_uri}/index.md"
                ):
                    continue
                deleted = await client.delete_uri(uri=child_uri)
                if not deleted.get("ok"):
                    return False

        active_labels = [label for label, _completed in outcomes]
        previous_labels = {
            str(label)
            for label in state.metadata.get("active_partitions", [])
        }
        stale_labels = sorted(previous_labels - set(active_labels))
        for label in stale_labels:
            stale_uri = f"{run.target_uri}/partitions/{label}"
            deleted = await client.delete_uri(uri=stale_uri)
            if not deleted.get("ok"):
                detail = (
                    deleted.get("stderr")
                    or deleted.get("stdout")
                    or "partition cleanup failed"
                )[:400]
                self._append_group(run, GroupResult(
                    group_key=f"merge:partition:{label}",
                    kind="(all)",
                    target_uri=stale_uri,
                    source_count=0,
                    status="failed",
                    detail=detail,
                ))
                state.save(skill_fingerprint=state.skill_fingerprint)
                return False

        partition_roots = [
            f"{run.target_uri}/partitions/{label}"
            for label in active_labels
        ]
        manifest_fingerprint = self._merge_input_fingerprint(
            [(uri, label) for uri, label in zip(partition_roots, active_labels)],
            skill_revision=skill_revision,
        )
        if (
            force_all
            or state.metadata.get("partition_manifest_fingerprint")
            != manifest_fingerprint
        ):
            links = "\n".join(
                f"- [Partition {label}]({uri})"
                for label, uri in zip(active_labels, partition_roots)
            )
            manifest = (
                "---\n"
                "type: team-memory-index\n"
                "title: Team Memory\n"
                "---\n\n"
                "# Team Memory\n\n"
                f"Users: {len(staged_roots)}\n\n"
                f"{links}\n"
            )
            published = await client.upsert_text(
                root_uri=run.target_uri,
                uri=f"{run.target_uri}/index.md",
                content=manifest,
            )
            if not published.get("ok"):
                detail = (
                    published.get("stderr")
                    or published.get("stdout")
                    or "partition index publish failed"
                )[:400]
                self._append_group(run, GroupResult(
                    group_key="merge",
                    kind="(all)",
                    target_uri=run.target_uri,
                    source_count=len(active_labels),
                    status="failed",
                    detail=detail,
                ))
                state.save(skill_fingerprint=state.skill_fingerprint)
                return False

        aggregate_fingerprint = self._merge_input_fingerprint(
            [
                (
                    root,
                    str(
                        state.groups.get(
                            f"merge:partition:{label}",
                            {},
                        ).get("source_fingerprint") or ""
                    ),
                )
                for label, root in zip(active_labels, partition_roots)
            ],
            skill_revision=skill_revision,
        )
        state.mark_ok("merge", aggregate_fingerprint)
        state.metadata.update(
            {
                "publish_mode": "partitioned",
                "active_partitions": active_labels,
                "partition_manifest_fingerprint": manifest_fingerprint,
            }
        )
        state.save(skill_fingerprint=state.skill_fingerprint)
        self._append_group(run, GroupResult(
            group_key="merge",
            kind="(all)",
            target_uri=run.target_uri,
            source_count=len(active_labels),
            status="ok",
            detail=(
                f"published {len(active_labels)} stable hash partitions "
                f"for {len(staged_roots)} users"
            ),
        ))
        return True

    async def _tree_reduce_merge(
        self,
        *,
        run: "AggregationRun",
        staged_roots: list,
        client: "CompileClient",
        skill_uri: str,
        skill_revision: str,
        force_all: bool = False,
        state: Optional["AggregationState"] = None,
        target_root: Optional[str] = None,
        merge_work_root: Optional[str] = None,
        group_prefix: str = "merge",
        compact_state: bool = True,
    ) -> bool:
        """Merge staging roots into the final root via bounded-fan-in tree reduce.

        Level 0: staging roots. Each round groups <= fan_in sources into one
        intermediate product. The final round writes the single shared-knowledge
        root. This keeps every compile <= 15 sources (16-source ceiling) no
        matter how many users participate. Successful intermediate products are
        checkpointed and reused when their inputs and Skill revision are unchanged.
        """
        fan_in = self._merge_fan_in()
        target_root = target_root or run.target_uri
        merge_work_root = (
            merge_work_root
            or f"{run.work_root or self._work_root(run.target_uri)}/_merge"
        )
        state = state or AggregationState.load(
            self._state_path(
                run.account_id,
                run.target_uri,
                run.endpoint,
                run.auth_mode,
            ),
            run.account_id,
        )
        staging_fingerprints = self._staging_source_fingerprints(
            run=run,
            state=state,
            staged_roots=staged_roots,
        )
        current = [
            (
                uri,
                staging_fingerprints.get(
                    uri,
                    f"uncached:{secrets.token_hex(16)}",
                ),
            )
            for uri in staged_roots
        ]
        level = 0
        # Reduce until a single round can produce the final root.
        while len(current) > fan_in:
            groups = [current[i : i + fan_in] for i in range(0, len(current), fan_in)]
            next_by_index: dict[int, tuple[str, str]] = {}
            pending: list[tuple[int, list[tuple[str, str]], str, str, str]] = []
            for gi, group in enumerate(groups):
                inter_uri = f"{merge_work_root}/L{level}/g{gi}"
                group_key = f"{group_prefix}:L{level}:g{gi}"
                source_fingerprint = self._merge_input_fingerprint(
                    group,
                    skill_revision=skill_revision,
                )
                if not state.needs_recompile(
                    group_key,
                    source_fingerprint,
                    current_skill_fingerprint=state.skill_fingerprint,
                    full=force_all,
                ):
                    next_by_index[gi] = (inter_uri, source_fingerprint)
                    self._append_group(run, GroupResult(
                        group_key=group_key, kind="(all)", target_uri=inter_uri,
                        source_count=len(group), status="skipped",
                        detail="unchanged (reused intermediate merge)",
                    ))
                    continue
                pending.append(
                    (gi, group, inter_uri, group_key, source_fingerprint)
                )

            async def merge_group(
                plan: tuple[int, list[tuple[str, str]], str, str, str],
            ) -> tuple[int, Optional[tuple[str, str]]]:
                gi, group, inter_uri, group_key, source_fingerprint = plan
                res = await self._run_compile(
                    client,
                    source_uris=[uri for uri, _fingerprint in group],
                    target_uri=inter_uri,
                    skill_uri=skill_uri,
                    skill_revision=skill_revision,
                    reason=(
                        f"Tree-reduce merge L{level} group {gi} "
                        f"({len(group)} sources). {_SNAPSHOT_MERGE_INSTRUCTION}"
                    ),
                    runtime_timeout_seconds=self._runtime_timeout(),
                )
                if res.get("ok"):
                    state.mark_ok(group_key, source_fingerprint)
                    state.checkpoint(
                        group_key,
                        skill_fingerprint=state.skill_fingerprint,
                    )
                    self._append_group(run, GroupResult(
                        group_key=group_key, kind="(all)", target_uri=inter_uri,
                        source_count=len(group), status="ok", detail="intermediate merge",
                    ))
                    return gi, (inter_uri, source_fingerprint)
                else:
                    state.mark_failed(group_key)
                    state.checkpoint(
                        group_key,
                        skill_fingerprint=state.skill_fingerprint,
                    )
                    detail = (res.get("stderr") or res.get("stdout") or "")[:400]
                    self._append_group(run, GroupResult(
                        group_key=group_key, kind="(all)", target_uri=inter_uri,
                        source_count=len(group), status="failed", detail=detail,
                    ))
                    return gi, None

            outcomes = await self._bounded_map(
                pending,
                concurrency=self._merge_concurrency(),
                operation=merge_group,
            )
            for gi, outcome in outcomes:
                if outcome is not None:
                    next_by_index[gi] = outcome
            if len(next_by_index) != len(groups):
                self._append_group(run, GroupResult(
                    group_key=f"{group_prefix}:L{level}",
                    kind="(all)", target_uri=target_root,
                    source_count=0,
                    status="failed",
                    detail="intermediate merge incomplete; final merge not started",
                ))
                return False
            current = [next_by_index[index] for index in range(len(groups))]
            level += 1

        # Final round: <= fan_in sources into the team-memory root.
        final_fingerprint = self._merge_input_fingerprint(
            current,
            skill_revision=skill_revision,
        )
        if not state.needs_recompile(
            group_prefix,
            final_fingerprint,
            current_skill_fingerprint=state.skill_fingerprint,
            full=force_all,
        ):
            self._append_group(run, GroupResult(
                group_key=group_prefix, kind="(all)", target_uri=target_root,
                source_count=len(current), status="skipped",
                detail="unchanged (reused final merge)",
            ))
            return True
        res = await self._run_compile(
            client,
            source_uris=[uri for uri, _fingerprint in current],
            target_uri=target_root,
            skill_uri=skill_uri,
            skill_revision=skill_revision,
            reason=(
                "Final merge of staged/intermediate memory into the team shared "
                f"memory. {_SNAPSHOT_MERGE_INSTRUCTION}"
            ),
            runtime_timeout_seconds=self._runtime_timeout(),
        )
        if res.get("ok"):
            state.mark_ok(group_prefix, final_fingerprint)
            state.checkpoint(group_prefix, skill_fingerprint=state.skill_fingerprint)
            if compact_state:
                state.save(skill_fingerprint=state.skill_fingerprint)
            self._append_group(run, GroupResult(
                group_key=group_prefix, kind="(all)", target_uri=target_root,
                source_count=len(current), status="ok",
                detail=f"merged (levels={level + 1})" if level else "merged",
            ))
            return True
        else:
            state.mark_failed(group_prefix)
            state.checkpoint(group_prefix, skill_fingerprint=state.skill_fingerprint)
            if compact_state:
                state.save(skill_fingerprint=state.skill_fingerprint)
            detail = (res.get("stderr") or res.get("stdout") or "")[:500]
            self._append_group(run, GroupResult(
                group_key=group_prefix, kind="(all)", target_uri=target_root,
                source_count=len(current), status="failed", detail=detail,
            ))
            return False
