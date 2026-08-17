"""Content-level True Replay for an applied DreamCycle Memory Change."""

from __future__ import annotations

import hashlib
import json
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import PurePosixPath
from typing import Any, Callable, Mapping

from ..progressive_replay import (
    normalize_checklist_report,
    progressive_replay_decision,
)
from ..session_store import SessionStore
from ..true_replay import (
    _native_agent_runtime,
    compare_efficiency,
    load_context_snapshot,
    render_trajectory,
    spawn_native_agent_branch,
)
from .memory_changes import MemoryChangeLedger

MEMORY_TRUE_REPLAY_SCHEMA_V1 = "teamevolver.memory-true-replay.v1"
_MAX_QUERY_CHARS = 32_000
_MAX_CHECKLIST_ITEMS = 50


class MemoryTrueReplayRunner:
    """Run before/after Memory as the only branch-level context treatment."""

    def __init__(
        self,
        *,
        ledger: MemoryChangeLedger,
        app_config: Any,
        branch_runner: Callable[..., dict[str, Any]] = spawn_native_agent_branch,
        session_store: SessionStore | None = None,
    ) -> None:
        self._ledger = ledger
        self._app_config = app_config
        self._branch_runner = branch_runner
        self._session_store = session_store

    def run(
        self,
        *,
        change_id: str,
        query: str,
        checklist: list[Any],
        source_session_id: str = "",
        max_interactions: int = 4,
        timeout_seconds: int = 600,
    ) -> dict[str, Any]:
        change = self._ledger.load_change(change_id)
        if change is None:
            raise KeyError(f"Memory Change not found: {change_id}")
        if str(change.get("snapshot_status") or "") not in {
            "complete",
            "partial",
        }:
            raise ValueError("Memory Change does not have replayable Snapshots")

        instruction = str(query or "").strip()
        if not instruction:
            raise ValueError("query is required")
        if len(instruction) > _MAX_QUERY_CHARS:
            raise ValueError("query exceeds 32000 characters")
        normalized_checklist = self._normalize_checklist(checklist)
        if not normalized_checklist:
            raise ValueError("at least one checklist item is required")
        interactions = max(1, min(20, int(max_interactions or 4)))
        timeout = max(30, min(1800, int(timeout_seconds or 600)))

        session = self._select_source_session(source_session_id)
        session_id = str(session.get("session_id") or "")
        turn_num = self._source_turn_num(session)
        runtime_type, endpoint = _native_agent_runtime(session)
        if not endpoint:
            raise ValueError(
                f"Source Session runtime {runtime_type or 'unknown'} "
                "has no replay endpoint"
            )

        replay_id = f"mrp_{uuid.uuid4().hex}"
        before_content, after_content = self._memory_variants(change)
        before_treatment_hash = self._sha256(before_content)
        after_treatment_hash = self._sha256(after_content)
        if before_treatment_hash == after_treatment_hash:
            raise ValueError("before and after Memory content are identical")

        shared_snapshot = self._shared_context_snapshot(
            session,
            change=change,
        )
        shared_context_hash = self._stable_hash(shared_snapshot)
        manifest = {
            "schema_version": "teamevolver.memory-replay-manifest.v1",
            "replay_id": replay_id,
            "change_id": change_id,
            "shared_context_hash": shared_context_hash,
            "query_hash": self._sha256(instruction),
            "source_session_id": session_id,
        }
        common_case = {
            "index": 0,
            "dataset_id": replay_id,
            "session_id": session_id,
            "turn_num": turn_num,
            "instruction": instruction,
            "query": instruction,
            "checklist": normalized_checklist,
            "progressive_disclosure": {
                "enabled": True,
                "initial_visibility": "query_only",
                "batch_size": 4,
                "stop_when": "all_checklist_items_satisfied",
            },
            "execution_manifest": manifest,
        }
        contexts = {
            "baseline": self._with_treatment(
                shared_snapshot,
                replay_id=replay_id,
                branch="baseline",
                content=before_content,
                content_hash=before_treatment_hash,
                title=self._memory_title(change),
            ),
            "candidate": self._with_treatment(
                shared_snapshot,
                replay_id=replay_id,
                branch="candidate",
                content=after_content,
                content_hash=after_treatment_hash,
                title=self._memory_title(change),
            ),
        }
        job = {
            "job_id": replay_id,
            "candidate_revision": str(change.get("after_hash") or change_id),
            "candidate_skill": {},
            "current_skill": None,
            "include_full_trace": False,
        }
        results: dict[str, dict[str, Any]] = {}
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = {
                pool.submit(
                    self._branch_runner,
                    branch,
                    instruction,
                    None,
                    job,
                    {
                        **common_case,
                        "context_snapshot": contexts[branch],
                    },
                    session,
                    timeout,
                    interactions,
                ): branch
                for branch in ("baseline", "candidate")
            }
            for future in as_completed(futures):
                branch = futures[future]
                try:
                    results[branch] = future.result()
                except Exception as exc:  # noqa: BLE001 - persist failed replay.
                    results[branch] = {
                        "branch": branch,
                        "runtime": runtime_type,
                        "ok": False,
                        "error": f"{type(exc).__name__}: {exc}",
                    }

        completed_at = datetime.now(timezone.utc).isoformat()
        failures = [
            f"{branch}: {result.get('error') or 'branch failed'}"
            for branch, result in results.items()
            if not result.get("ok")
        ]
        efficiency = compare_efficiency(
            results["baseline"],
            results["candidate"],
        )
        branch_checklists = {
            branch: normalize_checklist_report(
                results[branch],
                expected_checklist=normalized_checklist,
            )
            for branch in ("baseline", "candidate")
        }
        if failures:
            policy = {
                "accepted": False,
                "verdict": "reject",
                "no_regression": False,
                "decision_basis": "branch_failure",
            }
            status = "failed"
        else:
            policy = progressive_replay_decision(
                efficiency=efficiency,
                baseline_checklist=branch_checklists["baseline"],
                candidate_checklist=branch_checklists["candidate"],
            )
            status = "evaluated"

        replay = {
            "schema_version": MEMORY_TRUE_REPLAY_SCHEMA_V1,
            "replay_id": replay_id,
            "change_id": change_id,
            "mode": "memory_true_replay",
            "status": status,
            "runtime": runtime_type,
            "source_session_id": session_id,
            "query": instruction,
            "checklist": normalized_checklist,
            "max_interactions": interactions,
            "timeout_seconds": timeout,
            "shared_context_hash": shared_context_hash,
            "treatment": {
                "before_oid": str(change.get("before_oid") or ""),
                "after_oid": str(change.get("after_oid") or ""),
                "before_hash": before_treatment_hash,
                "after_hash": after_treatment_hash,
                "action": str(change.get("action") or ""),
            },
            "accepted": bool(policy.get("accepted")),
            "verdict": str(policy.get("verdict") or "reject"),
            "no_regression": bool(policy.get("no_regression")),
            "decision_policy": policy,
            "reason": (
                "; ".join(failures)
                if failures
                else str(policy.get("decision_basis") or policy.get("verdict") or "")
            ),
            "efficiency": efficiency,
            "branch_checklists": branch_checklists,
            "cases": [
                {
                    "baseline": self._branch_result(
                        results["baseline"],
                        checklist=branch_checklists["baseline"],
                    ),
                    "candidate": self._branch_result(
                        results["candidate"],
                        checklist=branch_checklists["candidate"],
                    ),
                }
            ],
            "completed_at": completed_at,
        }
        return self._ledger.save_replay(replay)

    def list_replays(
        self,
        *,
        change_id: str = "",
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        return self._ledger.list_replays(
            change_id=change_id,
            limit=limit,
        )

    def _select_source_session(self, session_id: str) -> dict[str, Any]:
        store = self._session_store or SessionStore.from_config(
            self._app_config
        )
        wanted = str(session_id or "").strip()
        if wanted:
            session = store.load_session(wanted)
            if session is None:
                raise ValueError(f"Source Session not found: {wanted}")
            return session
        for row in store.list_conversations(limit=100):
            candidate = store.load_session(str(row.get("session_id") or ""))
            if not isinstance(candidate, dict):
                continue
            runtime_type, endpoint = _native_agent_runtime(candidate)
            runtime_context = (
                candidate.get("runtime_context")
                if isinstance(candidate.get("runtime_context"), dict)
                else {}
            )
            if endpoint and (
                runtime_type != "agentshub"
                or runtime_context.get("tenant_id")
            ):
                return candidate
        raise ValueError("No replayable Source Session is available")

    def _memory_variants(
        self,
        change: Mapping[str, Any],
    ) -> tuple[str, str]:
        before = ""
        after = ""
        before_oid = str(change.get("before_oid") or "")
        after_oid = str(change.get("after_oid") or "")
        before_path = str(change.get("before_path") or "")
        after_path = str(change.get("after_path") or "")
        if change.get("before_exists") and before_oid and before_path:
            before = self._ledger.read_snapshot_text(
                oid=before_oid,
                path=before_path,
            )
            self._verify_content_hash(
                before,
                expected=str(change.get("before_hash") or ""),
                label="before",
            )
        if (
            str(change.get("action") or "") != "archive"
            and change.get("after_exists")
            and after_oid
            and after_path
        ):
            after = self._ledger.read_snapshot_text(
                oid=after_oid,
                path=after_path,
            )
            self._verify_content_hash(
                after,
                expected=str(change.get("after_hash") or ""),
                label="after",
            )
        return before, after

    def _shared_context_snapshot(
        self,
        session: Mapping[str, Any],
        *,
        change: Mapping[str, Any],
    ) -> dict[str, Any]:
        turn = self._source_turn(session)
        usage = (
            turn.get("context_usage")
            if isinstance(turn.get("context_usage"), dict)
            else {}
        )
        snapshot_id = str(usage.get("context_snapshot_id") or "")
        snapshot = (
            load_context_snapshot(snapshot_id, dict(session))
            if snapshot_id
            else None
        )
        snapshot = dict(snapshot or {})
        treatment_paths = {
            self._memory_relative_path(change.get("before_path")),
            self._memory_relative_path(change.get("after_path")),
        }
        treatment_paths.discard("")
        treatment_hashes = {
            str(change.get("before_hash") or ""),
            str(change.get("after_hash") or ""),
        }
        treatment_hashes.discard("")
        snapshot["items"] = [
            dict(item)
            for item in snapshot.get("items") or []
            if isinstance(item, dict)
            and self._memory_relative_path(item.get("uri"))
            not in treatment_paths
            and str(item.get("content_hash") or "")
            not in treatment_hashes
        ]
        snapshot["snapshot_id"] = (
            str(snapshot.get("snapshot_id") or "")
            or f"memory-replay-shared:{change.get('change_id')}"
        )
        return snapshot

    @staticmethod
    def _with_treatment(
        shared: Mapping[str, Any],
        *,
        replay_id: str,
        branch: str,
        content: str,
        content_hash: str,
        title: str,
    ) -> dict[str, Any]:
        snapshot = {
            **dict(shared),
            "snapshot_id": f"{replay_id}:{branch}",
            "items": [
                dict(item)
                for item in shared.get("items") or []
                if isinstance(item, dict)
            ],
        }
        if content:
            snapshot["items"].append(
                {
                    "scope": "team_memory",
                    "kind": "memory",
                    "title": title,
                    "content_hash": content_hash,
                    "l0": content[:500],
                    "l1": content[:4000],
                    "expanded": {"full": content},
                }
            )
        return snapshot

    @staticmethod
    def _source_turn(session: Mapping[str, Any]) -> dict[str, Any]:
        turns = [
            dict(turn)
            for turn in session.get("turns") or []
            if isinstance(turn, dict)
        ]
        return turns[-1] if turns else {}

    @classmethod
    def _source_turn_num(cls, session: Mapping[str, Any]) -> int:
        turn = cls._source_turn(session)
        return max(1, int(turn.get("turn_num") or len(session.get("turns") or []) or 1))

    @staticmethod
    def _memory_title(change: Mapping[str, Any]) -> str:
        path = str(
            change.get("after_path")
            or change.get("before_path")
            or "team-memory"
        )
        return PurePosixPath(path).name or "team-memory"

    @staticmethod
    def _normalize_checklist(values: list[Any]) -> list[dict[str, Any]]:
        output: list[dict[str, Any]] = []
        for index, raw in enumerate(values[:_MAX_CHECKLIST_ITEMS], start=1):
            if isinstance(raw, Mapping):
                text = str(raw.get("text") or raw.get("requirement") or "").strip()
                item_id = str(raw.get("id") or f"M{index:02d}")
            else:
                text = str(raw or "").strip()
                item_id = f"M{index:02d}"
            if text:
                output.append({"id": item_id, "text": text})
        return output

    @staticmethod
    def _branch_result(
        result: Mapping[str, Any],
        *,
        checklist: Mapping[str, Any],
    ) -> dict[str, Any]:
        return {
            "ok": bool(result.get("ok")),
            "error": str(result.get("error") or ""),
            "final_response": str(result.get("final_response") or "")[:8000],
            "trajectory": render_trajectory(list(result.get("messages") or [])),
            "interaction_turns": int(result.get("interaction_turns") or 0),
            "tool_call_count": int(result.get("tool_call_count") or 0),
            "total_tokens": int(result.get("total_tokens") or 0),
            "input_tokens": int(result.get("input_tokens") or 0),
            "output_tokens": int(result.get("output_tokens") or 0),
            "interactions": list(result.get("interactions") or []),
            "checklist_report": dict(checklist),
            "context_input_hash": str(result.get("context_input_hash") or ""),
            "safety_report": dict(result.get("safety_report") or {}),
        }

    @staticmethod
    def _sha256(value: str) -> str:
        return hashlib.sha256(value.encode("utf-8")).hexdigest()

    @staticmethod
    def _stable_hash(value: Any) -> str:
        return hashlib.sha256(
            json.dumps(
                value,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                default=str,
            ).encode("utf-8")
        ).hexdigest()

    @staticmethod
    def _memory_relative_path(value: Any) -> str:
        uri = str(value or "").split("?", 1)[0].rstrip("/")
        marker = "/memories/"
        if marker in uri:
            return uri.split(marker, 1)[1]
        if uri.endswith("/memories"):
            return ""
        return uri

    @classmethod
    def _verify_content_hash(
        cls,
        content: str,
        *,
        expected: str,
        label: str,
    ) -> None:
        if expected and cls._sha256(content) != expected:
            raise ValueError(
                f"{label} Snapshot content hash does not match Memory Change"
            )
