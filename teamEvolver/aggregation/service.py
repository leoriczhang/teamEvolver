"""Orchestration for cross-user memory aggregation.

Pipeline (all steps below use the OpenViking Root/Admin credential resolved
for the current request):

1. Resolve account users via the Admin API.
2. Expand each user + memory category into source URIs and plan compile
   batches under the ov compile source ceiling.
3. For each batch, decide (incrementally) whether it needs recompiling, then
   run ``ov compile`` into ``viking://resources/<prefix>/<kind>`` with the
   user-editable OKF Skill.
4. Persist per-group status for the next incremental run.

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
from .state import AggregationState

logger = logging.getLogger(__name__)


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
    status: str = "pending"  # pending | running | completed | failed
    started_at: float = field(default_factory=time.time)
    finished_at: Optional[float] = None
    groups: list[GroupResult] = field(default_factory=list)
    error: str = ""

    def to_public(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "account_id": self.account_id,
            "endpoint": self.endpoint,
            "auth_mode": self.auth_mode,
            "target_uri": self.target_uri,
            "status": self.status,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "error": self.error,
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
        self._compile_slots = threading.BoundedSemaphore(self._phase1_concurrency())

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
        # The OKF skill is installed into each participating identity's OWN
        # skills space (viking://user/<uid>/skills/<name>) so compile — which
        # runs as that identity — can read it. agent/skills is globally readable
        # but not writable for this flow, so per-identity install is used.
        from .okf_skill import DEFAULT_OKF_SKILL_NAME

        configured = str(getattr(self.config, "aggregation_okf_skill_uri", "") or "")
        # Accept either a bare name or a full URI; derive the trailing name.
        leaf = configured.rstrip("/").rsplit("/", 1)[-1] if configured else ""
        return leaf or DEFAULT_OKF_SKILL_NAME

    def _own_skill_uri(self, user_id: str) -> str:
        return f"viking://user/{user_id}/skills/{self._skill_name()}"

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

    def _work_root(self, target_uri: Optional[str] = None) -> str:
        # Scratch space for per-user staging and tree-reduce intermediates. Kept
        # as a SIBLING of the final knowledge root,
        # never inside it, so the final team-memory root contains only the
        # aggregated knowledge — no _staging/_merge leaking into the workspace
        # view or the root's L0/L1 summaries.
        staging = self._staging_dir().strip("/")
        return f"{self.resolve_target_uri(target_uri)}-{staging}"

    def _staging_uri(self, user_id: str, target_uri: Optional[str] = None) -> str:
        # One staging root per user (unified-knowledge layout). The OKF skill
        # lays out entities/, events/, tools/ ... subdirectories INSIDE this
        # root, so the target itself must not carry a per-kind path segment
        # (that produced the entities/entities double-nesting).
        return f"{self._work_root(target_uri)}/{user_id}"

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

    def _phase1_concurrency(self) -> int:
        return max(1, int(getattr(self.config, "aggregation_phase1_concurrency", 6) or 6))

    def _merge_fan_in(self) -> int:
        return max(2, min(15, int(getattr(self.config, "aggregation_merge_fan_in", 12) or 12)))

    def _runtime_timeout(self) -> float:
        return float(
            getattr(self.config, "aggregation_compile_runtime_timeout_seconds", 3000)
            or 3000
        )

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

        skill_body = self.skill_body()
        skill_name = self._skill_name()
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
        # A skill change forces every user to recompile (rule 3).
        force_all = full or (state.skill_fingerprint != skill_fp)

        # Run the whole async pipeline in one event loop so Phase 1 can fan out.
        staged_roots = asyncio.run(
            self._run_pipeline(
                run=run,
                users=users,
                user_api_keys=credentials.user_api_keys,
                kinds=kinds,
                endpoint=endpoint,
                agent_id=agent_id,
                skill_name=skill_name,
                skill_body=skill_body,
                state=state,
                force_all=force_all,
            )
        )

        # Persist state (fingerprints + per-user status) for incremental reruns.
        state.save(skill_fingerprint=skill_fp)

        if not staged_roots:
            run.groups.append(
                GroupResult(
                    group_key="merge", kind="(all)", target_uri=run.target_uri,
                    source_count=0, status="skipped", detail="no staged sources",
                )
            )
            return

        # Phase 2: tree-reduce merge (bounded fan-in) as the team user.
        team_client = CompileClient(
            endpoint=endpoint,
            account_id=run.account_id,
            user_id=credentials.merge_user_id,
            api_key=credentials.merge_api_key,
            agent_id=agent_id,
            timeout_seconds=self._runtime_timeout(),
        )
        team_install = asyncio.run(
            team_client.install_skill(
                skill_name=skill_name,
                skill_body=skill_body,
                parent_uri=(
                    f"viking://user/{credentials.merge_user_id}/skills"
                ),
            )
        )
        if not team_install.get("ok"):
            run.groups.append(
                GroupResult(
                    group_key="skill:team", kind="(skill)", target_uri="",
                    source_count=0, status="failed",
                    detail=(team_install.get("stderr") or team_install.get("stdout") or "")[:400],
                )
            )
            return
        team_skill = self._own_skill_uri(credentials.merge_user_id)
        asyncio.run(
            self._tree_reduce_merge(
                run=run,
                staged_roots=staged_roots,
                client=team_client,
                skill_uri=team_skill,
            )
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
        users = [
            user_id
            for user_id in by_user
            if user_id != self._team_user()
        ]
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
        kinds: list,
        endpoint: str,
        agent_id: str,
        skill_name: str,
        skill_body: str,
        state: "AggregationState",
        force_all: bool,
    ) -> list:
        """Phase 1: stage every user concurrently; return staging roots (ok/reused)."""
        import asyncio

        sem = asyncio.Semaphore(self._phase1_concurrency())
        results: list = await asyncio.gather(
            *(
                self._stage_one_user(
                    run=run, user_id=uid, kinds=kinds, endpoint=endpoint,
                    api_key=user_api_keys[uid], agent_id=agent_id, skill_name=skill_name,
                    skill_body=skill_body, state=state, force_all=force_all, sem=sem,
                )
                for uid in users
            )
        )
        return [r for r in results if r]

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
        agent_id: str,
        skill_name: str,
        skill_body: str,
        state: "AggregationState",
        force_all: bool,
        sem,
    ):
        """Stage a single user's memory into its staging root. Failure-isolated.

        Returns the staging URI when output exists (fresh or reused), else None.
        """
        async with sem:
            staging_uri = self._staging_uri(user_id, run.target_uri)
            group_key = f"stage:{user_id}"
            # Probe which categories this user actually has, and fingerprint them.
            present, source_fp = await self._probe_user_sources(
                user_id, kinds, endpoint, api_key, run.account_id
            )
            if not present:
                self._append_group(run, GroupResult(
                    group_key=group_key, kind="(all)", target_uri=staging_uri,
                    source_count=0, status="skipped",
                    detail="user has no memory in requested kinds",
                ))
                return None

            # Incremental: skip recompile when unchanged and last run was ok.
            if not state.needs_recompile(
                group_key, source_fp, current_skill_fingerprint=state.skill_fingerprint,
                full=force_all,
            ):
                self._append_group(run, GroupResult(
                    group_key=group_key, kind="(all)", target_uri=staging_uri,
                    source_count=len(present), status="skipped", detail="unchanged (reused staging)",
                ))
                return staging_uri  # reuse prior staging for the merge

            client = CompileClient(
                endpoint=endpoint, account_id=run.account_id, user_id=user_id,
                api_key=api_key, agent_id=agent_id, timeout_seconds=self._runtime_timeout(),
            )
            install = await client.install_skill(
                skill_name=skill_name, skill_body=skill_body,
                parent_uri=f"viking://user/{user_id}/skills",
            )
            if not install.get("ok"):
                state.mark_failed(group_key)
                self._append_group(run, GroupResult(
                    group_key=f"skill:{user_id}", kind="(skill)", target_uri="",
                    source_count=0, status="failed",
                    detail=(install.get("stderr") or install.get("stdout") or "")[:400],
                ))
                return None
            own_skill = self._own_skill_uri(user_id)
            cap = max(1, min(self._max_users_per_batch(), 15))
            sources = [f"viking://user/{user_id}/memories/{k}" for k in present[:cap]]
            result = await self._run_compile(
                client,
                source_uris=sources, target_uri=staging_uri, skill_uri=own_skill,
                reason=f"Stage {user_id}'s memory ({len(sources)} categories) for team aggregation.",
                runtime_timeout_seconds=self._runtime_timeout(),
            )
            if result.get("ok"):
                state.mark_ok(group_key, source_fp)
                self._append_group(run, GroupResult(
                    group_key=group_key, kind="(all)", target_uri=staging_uri,
                    source_count=len(sources), status="ok",
                ))
                return staging_uri
            detail = (result.get("stderr") or result.get("stdout") or "")[:500]
            if "NOT_FOUND" in detail or "not found" in detail:
                self._append_group(run, GroupResult(
                    group_key=group_key, kind="(all)", target_uri=staging_uri,
                    source_count=len(sources), status="skipped", detail=detail,
                ))
                return None
            state.mark_failed(group_key)
            self._append_group(run, GroupResult(
                group_key=group_key, kind="(all)", target_uri=staging_uri,
                source_count=len(sources), status="failed", detail=detail,
            ))
            return None

    def _append_group(self, run: "AggregationRun", group: "GroupResult") -> None:
        with self._lock:
            run.groups.append(group)

    async def _probe_user_sources(
        self, user_id: str, kinds: list, endpoint: str, api_key: str, account_id: str
    ):
        """Return (present_kinds, source_fingerprint) for a user's memory.

        The fingerprint hashes each present category's entry count + latest
        modTime so unchanged memory reuses prior staging (incremental).
        """
        import hashlib

        import httpx

        headers = {
            "X-API-Key": api_key,
            "X-OpenViking-Account": account_id,
            "X-OpenViking-User": user_id,
        }
        present: list = []
        parts: list = []
        async with httpx.AsyncClient(timeout=20.0) as http:
            for kind in kinds:
                uri = f"viking://user/{user_id}/memories/{kind}"
                try:
                    resp = await http.get(
                        f"{endpoint}/api/v1/fs/ls",
                        params={"uri": uri, "recursive": "true", "node_limit": "500"},
                        headers=headers,
                    )
                    if resp.status_code >= 400:
                        continue
                    entries = resp.json().get("result") or []
                except (httpx.HTTPError, ValueError):
                    # Be permissive: treat as present with a volatile marker so it
                    # recompiles rather than being silently dropped.
                    present.append(kind)
                    parts.append(f"{kind}:probe-error")
                    continue
                if not entries:
                    continue
                present.append(kind)
                latest = ""
                for e in entries:
                    mt = str(e.get("modTime") or "")
                    if mt > latest:
                        latest = mt
                parts.append(f"{kind}:{len(entries)}:{latest}")
        fp = "sha256:" + hashlib.sha256("\n".join(sorted(parts)).encode("utf-8")).hexdigest()
        return present, fp

    async def _tree_reduce_merge(
        self,
        *,
        run: "AggregationRun",
        staged_roots: list,
        client: "CompileClient",
        skill_uri: str,
    ) -> None:
        """Merge staging roots into the final root via bounded-fan-in tree reduce.

        Level 0: staging roots. Each round groups <= fan_in sources into one
        intermediate product. The final round writes the single shared-knowledge
        root. This keeps every compile <= 15 sources (16-source ceiling) no
        matter how many users participate.
        """
        fan_in = self._merge_fan_in()
        target_root = run.target_uri
        work_root = self._work_root(run.target_uri)

        current = list(staged_roots)
        level = 0
        # Reduce until a single round can produce the final root.
        while len(current) > fan_in:
            next_level: list = []
            groups = [current[i : i + fan_in] for i in range(0, len(current), fan_in)]
            for gi, group in enumerate(groups):
                inter_uri = f"{work_root}/_merge/L{level}/g{gi}"
                res = await self._run_compile(
                    client,
                    source_uris=group, target_uri=inter_uri, skill_uri=skill_uri,
                    reason=f"Tree-reduce merge L{level} group {gi} ({len(group)} sources).",
                    runtime_timeout_seconds=self._runtime_timeout(),
                )
                if res.get("ok"):
                    next_level.append(inter_uri)
                    self._append_group(run, GroupResult(
                        group_key=f"merge:L{level}:g{gi}", kind="(all)", target_uri=inter_uri,
                        source_count=len(group), status="ok", detail="intermediate merge",
                    ))
                else:
                    detail = (res.get("stderr") or res.get("stdout") or "")[:400]
                    self._append_group(run, GroupResult(
                        group_key=f"merge:L{level}:g{gi}", kind="(all)", target_uri=inter_uri,
                        source_count=len(group), status="failed", detail=detail,
                    ))
            if not next_level:
                self._append_group(run, GroupResult(
                    group_key=f"merge:L{level}", kind="(all)", target_uri=target_root,
                    source_count=0, status="failed", detail="all intermediate merges failed",
                ))
                return
            current = next_level
            level += 1

        # Final round: <= fan_in sources into the team-memory root.
        res = await self._run_compile(
            client,
            source_uris=current, target_uri=target_root, skill_uri=skill_uri,
            reason="Final merge of staged/intermediate memory into the team shared memory.",
            runtime_timeout_seconds=self._runtime_timeout(),
        )
        if res.get("ok"):
            self._append_group(run, GroupResult(
                group_key="merge", kind="(all)", target_uri=target_root,
                source_count=len(current), status="ok",
                detail=f"merged (levels={level + 1})" if level else "merged",
            ))
        else:
            detail = (res.get("stderr") or res.get("stdout") or "")[:500]
            self._append_group(run, GroupResult(
                group_key="merge", kind="(all)", target_uri=target_root,
                source_count=len(current), status="failed", detail=detail,
            ))
