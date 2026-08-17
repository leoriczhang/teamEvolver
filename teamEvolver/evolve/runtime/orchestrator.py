"""
Core orchestrator for the current session-level teamEvolver.evolve pipeline.

Active flow:
1. Drain pending sessions from shared storage.
2. Summarize sessions and extract metadata.
3. Optionally backfill a session-level score with session_judge.
4. Aggregate sessions by referenced skill.
5. Evolve existing-skill groups or create new skills from no-skill groups.
6. Upload skills, persist registry state, and ack processed sessions.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import os
import subprocess
import sys
import tempfile
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from ...dataset_store import (
    SkillDatasetStore,
    dataset_material_integrity,
)
from ...dataset_synthesizer import (
    DATASET_FORMAT,
    SynthesizedDatasetStore,
    checklist_items,
    dataset_to_replay_case,
    flatten_requirements,
    synthesize_evolution_datasets,
)
from ...integrations.agent_registry import list_agents
from ...observability import (
    langfuse_observation,
    update_langfuse_observation,
)
from ...progressive_replay import (
    aggregate_case_checklists,
    progressive_replay_decision,
    select_replay_cases,
)
from ...skills.bundle import (
    attach_bundle_payload,
    bundle_entrypoint_text,
    bundle_file_records,
    bundle_tree_sha256,
    candidate_skill_bundle,
)
from ...storage import InMemoryObjectStore, build_object_store, is_not_found_error
from ...validation import ValidationStore
from ...validation.bundle_checks import validate_candidate_bundle
from ...validation.runtime_compatibility import (
    evaluate_runtime_compatibility,
    prepare_runtime_validation,
    runtime_type_for_case,
)
from ..kernel.bundle_changes import (
    BundleChangeError,
    materialize_bundle_changes,
    select_editable_files,
)
from ..kernel.enums import NO_SKILL_KEY, DecisionAction
from ..kernel.helpers import build_skill_md, parse_skill_content
from ..kernel.llm import AsyncLLMClient
from ..kernel.registry import SkillIDRegistry
from ..kernel.settings import EvolveServerConfig
from ..stages.aggregate import aggregate_sessions_by_skill
from ..stages.execute import (
    create_skill_from_sessions,
    evolve_skill_from_sessions,
    execute_merge,
    set_evolve_debug_dir,
)
from ..stages.judge import judge_sessions_parallel
from ..stages.summarize import set_summarizer_debug_dir, summarize_sessions_parallel
from ..store.object_store import (
    build_bundle_record,
    delete_session_keys,
    fetch_skill_bundle,
    fetch_skill_content,
    fetch_version_bundle,
    list_session_keys,
    list_skill_versions,
    load_manifest,
    load_manifest_snapshot,
    publish_skill_bundle_batch,
    read_json_object,
    save_active_bundle,
    save_manifest,
    save_version_bundle,
)
from .evidence import SkillEvidenceStore, is_candidate_audit_session
from .mixins import EvolveEngineMixin

logger = logging.getLogger(__name__)

# Used to launch the built-in true-replay subprocess from a stable cwd.
_REPO_ROOT = Path(__file__).resolve().parents[3]


class EvolveServer(EvolveEngineMixin):
    """Session-level evolve server backed by shared object storage."""

    # Per-branch wall-clock budget for a true replay (each of baseline/candidate
    # runs a full real tool loop). The subprocess is given ~2x + slack overall.
    TRUE_REPLAY_TIMEOUT = 600

    def __init__(
        self,
        config: EvolveServerConfig,
        *,
        mock: bool = False,
        mock_root: str | None = None,
    ) -> None:
        self.config = config
        self._mock = mock
        self._bucket = self._build_bucket(config, mock=mock, mock_root=mock_root)
        self._prefix = ""
        self._skill_bucket = self._build_skill_bucket(config, mock=mock, mock_root=mock_root)
        self._skill_prefix = self._skill_prefix_for_config(config)
        # Session queue is pooled at the team-shared root (no per-peer partition):
        # skill evolution must consume every peer's sessions together. Peer-level
        # distinction is OpenViking's concern (``session commit --peer-id``), not
        # the queue path. Matches the producer in ``ProxyServer._upload_session_data``.
        self._session_prefix = ""
        self._llm = AsyncLLMClient(
            api_key=config.llm_api_key,
            base_url=config.llm_base_url,
            model=config.llm_model,
            max_tokens=config.llm_max_tokens,
            temperature=config.llm_temperature,
        )
        self._validation_store = ValidationStore.from_bucket(
            bucket=self._skill_bucket,
            customer_id=self.config.viking_customer_id,
        )
        self._id_registry = SkillIDRegistry()
        self._running = False
        # Throttle for merging externally-seeded registry entries in ``/status``
        # (monotonic seconds of the last merge; 0 means "never").
        self._status_seed_merge_ts = 0.0
        # Serialize evolution cycles. ``/trigger`` mutates shared instance state
        # in place (``_bucket`` / ``_session_prefix`` / ``_id_registry`` during
        # an identity override) and the manifest is read-modify-written
        # non-atomically inside ``run_once``. Concurrent triggers would race —
        # overlapping cycles drop each other's uploaded skills and corrupt the
        # registry (observed as a manifest that loses freshly-evolved skills).
        # Lazily created on first use to stay bound to the running loop.
        self._run_lock: asyncio.Lock | None = None
        # Background true-replay evaluations kicked off when a candidate is queued.
        # Held so they aren't garbage-collected mid-flight (asyncio only keeps a
        # weak reference to bare tasks).
        self._eval_tasks: set[asyncio.Task[Any]] = set()
        self._eval_jobs: dict[str, asyncio.Task[Any]] = {}
        self._eval_refresh_pending: set[str] = set()

        set_evolve_debug_dir(config.debug_dump_dir)
        set_summarizer_debug_dir(config.debug_dump_dir)
        self._id_registry.load_from_oss(self._skill_bucket, self._skill_prefix)

    @staticmethod
    def _skill_prefix_for_config(_config: EvolveServerConfig) -> str:
        # EvolveServer produces team skills, not per-peer personal skills.
        # Keep skills/manifest/registry at the team resources root so every
        # train/test peer in the group reads the same evolved library.
        return ""

    @staticmethod
    def _build_skill_bucket(
        config: EvolveServerConfig,
        *,
        mock: bool = False,
        mock_root: str | None = None,
    ) -> Any:
        if mock:
            if not mock_root:
                raise ValueError("mock mode requires mock_root")
            return InMemoryObjectStore(mock_root)
        backend_normalized = str(config.storage_backend or "").strip().lower()
        if backend_normalized == "viking":
            return build_object_store(
                backend="viking",
                endpoint=getattr(config, "viking_endpoint", "") or config.storage_endpoint,
                viking_account=getattr(config, "viking_account", "") or "default",
                viking_user=getattr(config, "viking_user", "") or "default",
                viking_agent=getattr(config, "viking_agent", "") or "team-skill-evolver",
                viking_api_key=getattr(config, "viking_api_key", "") or "",
                viking_agent_id=getattr(config, "viking_agent_id", "") or "",
                viking_root_prefix=getattr(config, "viking_root_prefix", "") or "team-skill-evolver",
                viking_group_id=getattr(config, "viking_group_id", "") or "",
                viking_namespace="resources",
            )
        return EvolveEngineMixin._build_bucket(config, mock=mock, mock_root=mock_root)

    def _new_evidence_store(self) -> SkillEvidenceStore:
        return SkillEvidenceStore(
            self._skill_bucket,
            prefix=self._skill_prefix,
            max_entries=self.config.evidence_max_entries,
            recent_limit=self.config.evidence_recent_limit,
            historical_limit=self.config.evidence_historical_limit,
            replay_cases_per_window=self.config.evidence_replay_cases_per_window,
            change_debt_threshold=self.config.evidence_change_debt_threshold,
        )

    async def _prepare_evolution_evidence(
        self,
        evidence_key: str,
        sessions: list[dict[str, Any]],
    ) -> tuple[list[dict[str, Any]], dict[str, Any], dict[str, list[dict[str, Any]]]]:
        if not self.config.evidence_enabled:
            recent = self._build_replay_cases(sessions)
            for case in recent:
                case["evidence_window"] = "recent"
            return list(sessions), {}, {"recent": recent, "historical": []}
        store = self._new_evidence_store()
        await self._call_storage(store.record_sessions, evidence_key, sessions)
        planning_sessions, context = await self._call_storage(
            store.build_context,
            evidence_key,
            sessions,
        )
        replay_windows = await self._call_storage(
            store.build_replay_windows,
            evidence_key,
        )
        active_job_id = str(
            context.get("active_candidate_job_id") or ""
        ).strip()
        if active_job_id:
            context["active_candidate_feedback"] = await self._call_storage(
                self._candidate_validation_feedback,
                active_job_id,
            )
        return planning_sessions, context, replay_windows

    async def _synthesize_candidate_datasets(
        self,
        *,
        skill_name: str,
        sessions: list[dict[str, Any]],
        candidate_skill: dict[str, Any],
        evidence_classification: Optional[dict[str, Any]],
        evolution_context: Optional[dict[str, Any]],
        replay_windows: Optional[dict[str, list[dict[str, Any]]]],
    ) -> list[dict[str, Any]]:
        target_count = max(1, min(6, int(self.config.dataset_test_cases or 2)))
        managed = await self._call_storage(
            self._managed_evolution_datasets,
            skill_name,
            target_count,
        )
        if not self.config.dataset_synthesis_enabled or len(managed) >= target_count:
            return managed
        synthesis_skill = {
            **candidate_skill,
            "_evidence_classification": (
                evidence_classification
                if isinstance(evidence_classification, dict)
                else {}
            ),
        }
        datasets = await synthesize_evolution_datasets(
            self._llm,
            skill_name=skill_name,
            sessions=sessions,
            candidate_skill=synthesis_skill,
            evidence_context=evolution_context or {},
            replay_windows=replay_windows or {},
            case_count=target_count - len(managed),
            min_requirements=self.config.dataset_min_requirements,
            max_requirements=self.config.dataset_max_requirements,
            batch_size=self.config.dataset_disclosure_batch_size,
        )
        merged: list[dict[str, Any]] = []
        seen: set[str] = set()
        for dataset in [*managed, *datasets]:
            dataset_id = str(dataset.get("dataset_id") or "")
            if not dataset_id or dataset_id in seen:
                continue
            seen.add(dataset_id)
            merged.append(dataset)
        logger.info(
            "[DatasetSynthesizer] skill=%s sessions=%d managed=%d generated=%d",
            skill_name,
            len(sessions),
            len(managed),
            len(datasets),
        )
        return merged[:target_count]

    def _managed_evolution_datasets(
        self,
        skill_name: str,
        limit: int,
    ) -> list[dict[str, Any]]:
        """Load explicitly pinned Skill datasets for candidate replay."""
        store = SkillDatasetStore(
            self._skill_bucket,
            prefix=self._skill_prefix,
        )
        selected: list[dict[str, Any]] = []
        for raw in store.list_datasets(skill_name=skill_name):
            if not bool(raw.get("enabled_for_evolution")):
                continue
            available_paths = store.available_material_paths(raw)
            integrity = dataset_material_integrity(
                raw,
                available_paths=available_paths,
            )
            if not integrity["complete"]:
                logger.warning(
                    "[DatasetStore] skip incomplete fixed regression "
                    "skill=%s dataset=%s missing=%s",
                    skill_name,
                    raw.get("dataset_id"),
                    integrity["missing_paths"],
                )
                continue
            requirements = flatten_requirements(raw.get("requirements"))
            trajectory = flatten_requirements(
                raw.get("trajectory_requirements")
            )
            if not requirements and not trajectory:
                continue
            source = (
                raw.get("source")
                if isinstance(raw.get("source"), dict)
                else {}
            )
            source_session_ids = [
                str(item or "")
                for item in source.get("source_session_ids") or []
                if str(item or "")
            ]
            session_id = str(source.get("session_id") or "")
            if session_id and session_id not in source_session_ids:
                source_session_ids.insert(0, session_id)
            materials = []
            for rel_path, data in store.read_materials(raw):
                materials.append(
                    {
                        "path": rel_path,
                        "size": len(data),
                        "content_b64": base64.b64encode(data).decode("ascii"),
                    }
                )
            selected.append(
                {
                    **raw,
                    "dataset_format": str(
                        raw.get("dataset_format") or DATASET_FORMAT
                    ),
                    "requirements": requirements,
                    "trajectory_requirements": trajectory,
                    "checklist": checklist_items(requirements, trajectory),
                    "source_session_ids": source_session_ids,
                    "evidence_window": str(
                        raw.get("evidence_window")
                        or source.get("evidence_window")
                        or "historical"
                    ),
                    "materials": materials,
                }
            )
            if len(selected) >= max(1, int(limit or 1)):
                break
        return selected

    def _candidate_validation_feedback(
        self,
        job_id: str,
    ) -> dict[str, Any]:
        """Compact validator evidence for the next candidate revision."""
        job = self._validation_store.load_job(job_id) or {}
        decision = self._validation_store.load_decision(job_id) or {}
        results = self._validation_store.list_results(job_id)
        compact_results: list[dict[str, Any]] = []
        for result in results[-3:]:
            replay = (
                result.get("replay_summary")
                if isinstance(result.get("replay_summary"), dict)
                else {}
            )
            policy = (
                replay.get("decision_policy")
                if isinstance(replay.get("decision_policy"), dict)
                else {}
            )
            compact_results.append(
                {
                    "validator": str(result.get("user_alias") or ""),
                    "decision": str(
                        result.get("decision") or "inconclusive"
                    ),
                    "metric_changes": policy.get("metric_changes") or {},
                    "improved_metrics": list(
                        policy.get("improved_metrics") or []
                    ),
                    "regressed_metrics": list(
                        policy.get("regressed_metrics") or []
                    ),
                    "reason": str(result.get("reason") or "")[:1_000],
                }
            )
        return {
            "job_id": job_id,
            "candidate_revision": int(
                job.get("candidate_revision") or 1
            ),
            "job_status": str(job.get("status") or ""),
            "decision_status": str(decision.get("status") or ""),
            "candidate_edit_summary": (
                (job.get("candidate_skill") or {}).get("edit_summary")
                if isinstance(job.get("candidate_skill"), dict)
                else {}
            )
            or {},
            "validator_results": compact_results,
            "revision_guidance": (
                "Preserve existing behavior. Revise the candidate where True "
                "Replay shows an increased metric or no objective gain; do not "
                "repeat the same candidate."
            ),
        }

    async def _record_evolution_skip(
        self,
        evidence_key: str,
        sessions: list[dict[str, Any]],
        rationale: str,
    ) -> dict[str, Any]:
        if not self.config.evidence_enabled:
            return {}
        store = self._new_evidence_store()
        return await self._call_storage(
            store.record_skip,
            evidence_key,
            sessions,
            rationale,
        )

    async def _record_evolution_candidate(
        self,
        evidence_key: str,
        sessions: list[dict[str, Any]],
        job_id: str,
    ) -> None:
        if not self.config.evidence_enabled:
            return
        store = self._new_evidence_store()
        await self._call_storage(
            store.record_candidate,
            evidence_key,
            sessions,
            job_id,
        )

    async def _mark_evidence_published(
        self,
        evidence_key: str,
        job_id: str = "",
    ) -> None:
        if not self.config.evidence_enabled or not str(evidence_key or "").strip():
            return
        store = self._new_evidence_store()
        await self._call_storage(store.mark_published, evidence_key, job_id)

    def _load_published_replay_context(
        self,
        skill_name: str,
    ) -> Optional[dict[str, Any]]:
        """Load replay cases from the current published version for a merge."""
        version = int(self._id_registry.get_version(skill_name) or 0)
        if version <= 0:
            return None
        context = self._validation_store.load_skill_version_context(
            skill_name,
            version,
        )
        if not context:
            for job in reversed(self._validation_store.list_jobs()):
                candidate = (
                    job.get("candidate_skill")
                    if isinstance(job.get("candidate_skill"), dict)
                    else {}
                )
                if str(
                    candidate.get("name")
                    or job.get("candidate_skill_name")
                    or ""
                ) != skill_name:
                    continue
                job_id = str(job.get("job_id") or "")
                decision = self._validation_store.load_decision(job_id) or {}
                if (
                    decision.get("status") == "published"
                    and int(decision.get("version") or 0) == version
                ):
                    context = {
                        "job_id": job_id,
                        "job": job,
                        "decision": decision,
                        "evaluation": self._validation_store.load_evaluation(
                            job_id
                        )
                        or {},
                    }
                    break
        job = (
            context.get("job")
            if isinstance(context, dict)
            and isinstance(context.get("job"), dict)
            else {}
        )
        replay_cases = [
            dict(case)
            for case in job.get("replay_cases") or []
            if isinstance(case, dict)
        ]
        if not replay_cases:
            return None
        return {
            "skill_name": skill_name,
            "version": version,
            "job_id": str((context or {}).get("job_id") or job.get("job_id") or ""),
            "replay_cases": replay_cases,
        }

    def _load_remote_skills(self) -> dict[str, dict[str, Any]]:
        """Load the skill manifest from the personal/team skill bucket."""
        return load_manifest(self._skill_bucket, self._skill_prefix)

    def _load_remote_skill_record(self, name: str) -> Optional[dict[str, Any]]:
        rec = self._load_remote_skills().get(name)
        return rec if isinstance(rec, dict) else None

    @staticmethod
    def _overlay_manifest_metadata(
        skill: Optional[dict[str, Any]],
        manifest_record: Optional[dict[str, Any]],
    ) -> Optional[dict[str, Any]]:
        if not skill or not manifest_record:
            return skill
        category = str(manifest_record.get("category", "") or "").strip()
        if category and str(skill.get("category", "general") or "general").strip() == "general":
            skill["category"] = category
        if not str(skill.get("description", "") or "").strip():
            description = str(manifest_record.get("description", "") or "").strip()
            if description:
                skill["description"] = description
        return skill

    def _fetch_skill(self, name: str) -> Optional[str]:
        return fetch_skill_content(self._skill_bucket, self._skill_prefix, name)

    def _fetch_skill_bundle(self, name: str) -> dict[str, bytes]:
        return fetch_skill_bundle(
            self._skill_bucket,
            self._skill_prefix,
            name,
            self._load_remote_skill_record(name),
        )

    @staticmethod
    def _session_bundle_paths(sessions: list[dict[str, Any]]) -> list[str]:
        paths: list[str] = []
        for session in sessions:
            for turn in session.get("turns") or []:
                if not isinstance(turn, dict):
                    continue
                for field in ("read_skills", "modified_skills"):
                    for item in turn.get(field) or []:
                        path = (
                            str(item.get("path") or "")
                            if isinstance(item, dict)
                            else ""
                        ).strip()
                        if path and path not in paths:
                            paths.append(path)
        return paths

    def _prepare_skill_bundle_for_planner(
        self,
        skill: Optional[dict[str, Any]],
        sessions: list[dict[str, Any]],
    ) -> Optional[dict[str, Any]]:
        if not skill:
            return None
        prepared = dict(skill)
        bundle = candidate_skill_bundle(prepared)
        prepared = attach_bundle_payload(prepared, bundle)
        prepared["_editable_bundle_files"] = select_editable_files(
            bundle,
            extensions=self.config.bundle_text_extensions,
            max_file_bytes=self.config.bundle_max_file_bytes,
            max_prompt_bytes=self.config.bundle_max_prompt_bytes,
            priority_paths=self._session_bundle_paths(sessions),
        )
        return prepared

    def _record_committed_skill_mutation(
        self,
        *,
        action: str,
        expected: dict[str, Any],
        metadata: dict[str, Any],
    ) -> dict[str, Any]:
        from ...skills.hub import SkillHub
        from ...skills.mutations import SkillMutationService

        mutation_id = "evolve-" + hashlib.sha256(
            json.dumps(
                {
                    "action": action,
                    "name": expected.get("name"),
                    "version": expected.get("version"),
                    "sha256": expected.get("sha256"),
                    "tree_sha256": expected.get("tree_sha256"),
                },
                sort_keys=True,
            ).encode("utf-8")
        ).hexdigest()[:32]
        service = SkillMutationService.from_hub(
            SkillHub.from_bucket(
                self._skill_bucket,
                user_alias="teamEvolver",
            ),
            config=self.config,
        )
        return service.record_committed(
            action=action,
            mutation_id=mutation_id,
            expected=expected,
            tenant_ids=[],
            metadata=metadata,
        )

    def _upload_skill(self, skill: dict, action: str) -> str:
        name = skill.get("name", "")
        if not name:
            return "skipped_missing_name"

        native_batch = bool(getattr(self._skill_bucket, "native_batch_write", False))
        fixed_preconditions: dict[str, dict[str, str]] = {}
        if native_batch:
            _merged, registry_precondition = (
                self._id_registry.merge_from_oss_snapshot(
                    self._skill_bucket,
                    self._skill_prefix,
                )
            )
            fixed_preconditions[
                f"{self._skill_prefix}evolve_skill_registry.json"
            ] = registry_precondition
        registry_snapshot = self._id_registry.snapshot()
        registry_was_dirty = self._id_registry.dirty
        if native_batch:
            manifest, manifest_precondition = load_manifest_snapshot(
                self._skill_bucket,
                self._skill_prefix,
            )
            fixed_preconditions[f"{self._skill_prefix}manifest.json"] = (
                manifest_precondition
            )
        else:
            manifest = self._load_remote_skills()
        existed = name in manifest

        skill_id = self._id_registry.get_or_create(name)
        bundle = candidate_skill_bundle(skill)
        bundle_record = (
            build_bundle_record(bundle)
            if native_batch
            else save_active_bundle(
                self._skill_bucket,
                self._skill_prefix,
                name,
                bundle,
            )
        )
        md_bytes = bundle["SKILL.md"]
        content_sha = hashlib.sha256(md_bytes).hexdigest()
        tree_sha = str(bundle_record["tree_sha256"])
        version = self._id_registry.record_update(
            name,
            content_sha,
            action=action,
            bundle_record=bundle_record,
        )
        manifest[name] = {
            "name": name,
            "skill_id": skill_id,
            "version": version,
            "sha256": content_sha,
            "tree_sha256": tree_sha,
            "format": "bundle_v1",
            "entrypoint": "SKILL.md",
            "files": bundle_record["files"],
            "uploaded_by": "teamEvolver",
            "uploaded_at": datetime.now(timezone.utc).isoformat(),
            "description": skill.get("description", ""),
            "category": skill.get("category", "general"),
        }
        runtime_policy = (
            skill.get("runtime_policy")
            if isinstance(skill.get("runtime_policy"), dict)
            else {}
        )
        if runtime_policy:
            manifest[name]["runtime_policy"] = dict(runtime_policy)
        try:
            if native_batch:
                publish_skill_bundle_batch(
                    self._skill_bucket,
                    self._skill_prefix,
                    name,
                    version,
                    bundle,
                    manifest=manifest,
                    registry_bytes=self._id_registry.to_bytes(),
                    fixed_preconditions=fixed_preconditions,
                )
                self._id_registry.mark_persisted()
            else:
                save_version_bundle(
                    self._skill_bucket,
                    self._skill_prefix,
                    name,
                    version,
                    bundle,
                )
                save_manifest(self._skill_bucket, self._skill_prefix, manifest)
        except Exception:
            self._id_registry.restore(
                registry_snapshot,
                dirty=registry_was_dirty,
            )
            raise
        logger.info(
            "[EvolveServer] uploaded skill %s (id=%s, v%d) to %s",
            name,
            skill_id,
            version,
            f"{self._skill_prefix}skills/{name}/",
        )
        self._record_committed_skill_mutation(
            action="update" if existed else "publish",
            expected=manifest[name],
            metadata={
                "source": "evolve",
                "proposed_action": action,
            },
        )
        return "uploaded"

    def _list_skill_versions(self, name: str) -> dict[str, Any]:
        """Return archived versions + current pointer for a skill (object store)."""
        entry = self._id_registry.all_entries().get(name) or {}
        return {
            "name": name,
            "current_version": int(entry.get("version", 0) or 0),
            "available_versions": list_skill_versions(self._skill_bucket, self._skill_prefix, name),
            "history": list(entry.get("history") or []),
        }

    def _rollback_skill(self, name: str, target_version: int) -> dict[str, Any]:
        """Restore a skill's content from an archived version (object store).

        Reads the archived bundle for ``target_version`` and republishes it as a
        new (monotonically increasing) version whose content equals the old one,
        then rewrites the current ``SKILL.md`` pointer and manifest record.
        """
        available = list_skill_versions(self._skill_bucket, self._skill_prefix, name)
        if target_version not in available:
            return {
                "status": "error",
                "reason": f"version v{target_version} not found",
                "available_versions": available,
            }

        bundle = fetch_version_bundle(self._skill_bucket, self._skill_prefix, name, target_version)
        md_bytes = bundle.get("SKILL.md")
        if not md_bytes:
            return {
                "status": "error",
                "reason": f"archived bundle for v{target_version} is missing SKILL.md",
                "available_versions": available,
            }

        native_batch = bool(getattr(self._skill_bucket, "native_batch_write", False))
        fixed_preconditions: dict[str, dict[str, str]] = {}
        if native_batch:
            _merged, registry_precondition = (
                self._id_registry.merge_from_oss_snapshot(
                    self._skill_bucket,
                    self._skill_prefix,
                )
            )
            fixed_preconditions[
                f"{self._skill_prefix}evolve_skill_registry.json"
            ] = registry_precondition
            manifest, manifest_precondition = load_manifest_snapshot(
                self._skill_bucket,
                self._skill_prefix,
            )
            fixed_preconditions[f"{self._skill_prefix}manifest.json"] = (
                manifest_precondition
            )
        else:
            manifest = self._load_remote_skills()
        registry_snapshot = self._id_registry.snapshot()
        registry_was_dirty = self._id_registry.dirty
        skill_id = self._id_registry.get_or_create(name)
        from_version = self._id_registry.get_version(name)

        bundle_record = (
            build_bundle_record(bundle)
            if native_batch
            else save_active_bundle(
                self._skill_bucket,
                self._skill_prefix,
                name,
                bundle,
            )
        )
        content_sha = hashlib.sha256(md_bytes).hexdigest()
        tree_sha = str(bundle_record["tree_sha256"])
        new_version = self._id_registry.record_rollback(
            name,
            content_sha,
            target_version,
            bundle_record=bundle_record,
        )
        existing = manifest.get(name, {})
        parsed = parse_skill_content(name, md_bytes.decode("utf-8"))
        manifest[name] = {
            "name": name,
            "skill_id": skill_id,
            "version": new_version,
            "sha256": content_sha,
            "tree_sha256": tree_sha,
            "format": "bundle_v1",
            "entrypoint": "SKILL.md",
            "files": bundle_record["files"],
            "uploaded_by": "teamEvolver",
            "uploaded_at": datetime.now(timezone.utc).isoformat(),
            "description": parsed.get("description") or existing.get("description", ""),
            "category": parsed.get("category") or existing.get("category", "general"),
        }
        if isinstance(existing.get("runtime_policy"), dict):
            manifest[name]["runtime_policy"] = dict(
                existing["runtime_policy"]
            )
        try:
            if native_batch:
                publish_skill_bundle_batch(
                    self._skill_bucket,
                    self._skill_prefix,
                    name,
                    new_version,
                    bundle,
                    manifest=manifest,
                    registry_bytes=self._id_registry.to_bytes(),
                    fixed_preconditions=fixed_preconditions,
                )
                self._id_registry.mark_persisted()
            else:
                save_version_bundle(
                    self._skill_bucket,
                    self._skill_prefix,
                    name,
                    new_version,
                    bundle,
                )
                save_manifest(self._skill_bucket, self._skill_prefix, manifest)
                self._id_registry.save_to_oss(
                    self._skill_bucket,
                    self._skill_prefix,
                )
        except Exception:
            self._id_registry.restore(
                registry_snapshot,
                dirty=registry_was_dirty,
            )
            raise
        logger.info(
            "[EvolveServer] rolled back skill '%s' from v%d to v%d (new v%d)",
            name,
            from_version,
            target_version,
            new_version,
        )
        self._record_committed_skill_mutation(
            action="rollback",
            expected=manifest[name],
            metadata={
                "source": "evolve",
                "rolled_back_to": target_version,
                "from_version": from_version,
            },
        )
        return {
            "status": "rolled_back",
            "name": name,
            "skill_id": skill_id,
            "rolled_back_to": target_version,
            "from_version": from_version,
            "new_version": new_version,
            "content_sha": content_sha,
            "tree_sha256": tree_sha,
        }

    async def list_skill_versions(self, name: str) -> dict[str, Any]:
        """List archived versions for a skill."""
        return await self._call_storage(self._list_skill_versions, self._sanitise_name(name))

    def _get_skill_version(self, name: str, version: int) -> dict[str, Any]:
        """Return one version's SKILL.md content + parsed description/body.

        The current live version is read straight from the active ``SKILL.md``
        pointer (so externally-seeded skills without an archived bundle still
        resolve); any older version is rehydrated from its archived bundle.
        """
        entry = self._id_registry.all_entries().get(name) or {}
        current_version = int(entry.get("version", 0) or 0)
        archived = list_skill_versions(self._skill_bucket, self._skill_prefix, name)
        # The current version may not have an archived bundle (e.g. a seeded
        # skill), so union it into the selectable list.
        versions = sorted({v for v in archived if v > 0} | ({current_version} if current_version > 0 else set()))

        bundle: dict[str, bytes] = {}
        if version == current_version:
            bundle = self._fetch_skill_bundle(name)
        if not bundle:
            bundle = fetch_version_bundle(self._skill_bucket, self._skill_prefix, name, version)
        md_bytes = bundle.get("SKILL.md")
        if not md_bytes:
            return {
                "status": "not_found",
                "name": name,
                "version": version,
                "current_version": current_version,
                "versions": versions,
            }

        raw_md = md_bytes.decode("utf-8")
        parsed = parse_skill_content(name, raw_md)
        return {
            "status": "ok",
            "name": name,
            "version": version,
            "is_current": version == current_version,
            "current_version": current_version,
            "versions": versions,
            "skill_id": str(entry.get("skill_id") or entry.get("name") or name),
            "description": parsed.get("description", ""),
            "category": parsed.get("category", "general"),
            "content": parsed.get("content", ""),
            "raw_md": raw_md,
            "tree_sha256": bundle_tree_sha256(bundle),
            "files": bundle_file_records(bundle),
        }

    async def get_skill_version(self, name: str, version: int) -> dict[str, Any]:
        """Fetch one skill version's content + metadata for the dashboard."""
        return await self._call_storage(
            self._get_skill_version, self._sanitise_name(name), int(version)
        )

    async def rollback_skill(self, name: str, target_version: int) -> dict[str, Any]:
        """Roll a skill back to an archived version."""
        result = await self._call_storage(self._rollback_skill, self._sanitise_name(name), int(target_version))
        if result.get("status") == "rolled_back":
            logger.info("[EvolveServer] rolled back skill %s to v%d", name, target_version)
        return result

    def _detect_conflict(self, name: str, incoming_skill: dict) -> bool:
        record = self._load_remote_skill_record(name) or {}
        existing_tree = str(record.get("tree_sha256") or "")
        if existing_tree:
            return existing_tree != bundle_tree_sha256(
                candidate_skill_bundle(incoming_skill)
            )
        existing_sha = self._id_registry.get_content_sha(name)
        if not existing_sha:
            return False
        incoming_md = build_skill_md(incoming_skill)
        incoming_sha = hashlib.sha256(incoming_md.encode("utf-8")).hexdigest()
        return existing_sha != incoming_sha

    async def _resolve_and_upload(self, skill: dict, action_type: str) -> tuple[str, bool]:
        name = skill.get("name", "")
        if action_type == DecisionAction.MERGE:
            upload_status = await self._call_storage(
                self._upload_skill,
                skill,
                action_type,
            )
            return self._upload_status_to_action(action_type, upload_status)
        has_conflict = await self._call_storage(self._detect_conflict, name, skill)
        if not has_conflict:
            upload_status = await self._call_storage(self._upload_skill, skill, action_type)
            return self._upload_status_to_action(action_type, upload_status)

        logger.info("[EvolveServer] conflict detected for '%s' - merging", name)
        existing_md = await self._call_storage(self._fetch_skill, name)
        if not existing_md:
            upload_status = await self._call_storage(self._upload_skill, skill, action_type)
            return self._upload_status_to_action(action_type, upload_status)

        existing_skill = parse_skill_content(name, existing_md)
        existing_skill = self._overlay_manifest_metadata(
            existing_skill,
            self._load_remote_skill_record(name),
        )
        existing_bundle = await self._call_storage(
            self._fetch_skill_bundle,
            name,
        )
        if existing_skill and existing_bundle:
            existing_skill = attach_bundle_payload(
                existing_skill,
                existing_bundle,
            )
        existing_skill["_version"] = self._id_registry.get_version(name)
        merged = await execute_merge(self._llm, existing_skill, skill)
        if merged and merged.get("name"):
            merged["name"] = name
            incoming_bundle = candidate_skill_bundle(skill)
            changes = [
                item
                for item in (skill.get("file_changes") or [])
                if isinstance(item, dict)
            ]
            if changes:
                merged_bundle = dict(existing_bundle)
                for change in changes:
                    path = str(change.get("path") or "")
                    operation = str(change.get("operation") or "")
                    if operation == "delete":
                        merged_bundle.pop(path, None)
                    elif operation == "upsert" and path in incoming_bundle:
                        merged_bundle[path] = incoming_bundle[path]
            else:
                merged_bundle = incoming_bundle
            merged = attach_bundle_payload(
                merged,
                merged_bundle,
                file_changes=changes,
            )
            upload_status = await self._call_storage(self._upload_skill, merged, "merge")
            return self._upload_status_to_action("merge", upload_status)

        logger.warning("[EvolveServer] merge failed for '%s' - keeping incoming version", name)
        upload_status = await self._call_storage(self._upload_skill, skill, action_type)
        return self._upload_status_to_action(action_type, upload_status)

    @staticmethod
    def _upload_status_to_action(action_type: str, upload_status: str) -> tuple[str, bool]:
        if upload_status == "uploaded":
            return action_type, True
        if upload_status == "uploaded_pending_review":
            return f"{action_type}_pending_review", False
        if upload_status == "uploaded_pending_publish":
            return f"{action_type}_pending_publish", False
        if upload_status == "uploaded_draft":
            return f"{action_type}_draft", False
        return upload_status, False

    def _empty_judge_summary(self) -> dict[str, Any]:
        return {
            "enabled": bool(self.config.use_session_judge),
            "judged_sessions": 0,
            "scored_sessions": 0,
            "mean_score": None,
            "min_score": None,
            "max_score": None,
        }

    async def _run_session_judge(self, sessions: list[dict]) -> dict[str, Any]:
        summary = self._empty_judge_summary()
        if not self.config.use_session_judge or not sessions:
            return summary

        judged = await judge_sessions_parallel(self._llm, sessions)
        scores = [
            float(judge_scores["overall_score"])
            for session in sessions
            for judge_scores in [session.get("_judge_scores")]
            if isinstance(judge_scores, dict) and isinstance(judge_scores.get("overall_score"), (int, float))
        ]
        summary["judged_sessions"] = judged
        summary["scored_sessions"] = len(scores)
        if scores:
            summary["mean_score"] = round(sum(scores) / len(scores), 3)
            summary["min_score"] = round(min(scores), 3)
            summary["max_score"] = round(max(scores), 3)
        logger.info("[EvolveServer] judged %d sessions without benchmark scores", judged)
        return summary

    @staticmethod
    def _collect_session_judge_details(sessions: list[dict]) -> list[dict[str, Any]]:
        """Per-session judge scores for the history record.

        The aggregate ``session_judge`` block only carries mean/min/max, which
        cannot answer "how was *this* session judged?". We surface each
        session's dimension scores + overall + rationale so a per-session
        consumption view can be reconstructed even for skip/no-op cycles.
        """
        details: list[dict[str, Any]] = []
        for session in sessions:
            sid = str(session.get("session_id") or "").strip()
            if not sid:
                continue
            scores = session.get("_judge_scores")
            detail: dict[str, Any] = {"session_id": sid}
            if isinstance(scores, dict):
                for key in (
                    "overall_score",
                    "task_completion",
                    "response_quality",
                    "efficiency",
                    "tool_usage",
                    "rationale",
                ):
                    if scores.get(key) is not None:
                        detail[key] = scores.get(key)
            details.append(detail)
        return details

    def _empty_validation_publish_summary(self) -> dict[str, Any]:
        return {
            "publish_mode": self.config.publish_mode,
            "jobs_scanned": 0,
            "pending": 0,
            "published": 0,
            "rejected": 0,
            "inconclusive": 0,
            "skipped": 0,
            "escalated_to_human": 0,
        }

    @staticmethod
    def _parse_iso(value: Any) -> Optional[datetime]:
        try:
            dt = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
        except (ValueError, TypeError):
            return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt

    def _job_age_seconds(self, job: dict[str, Any]) -> Optional[float]:
        created = self._parse_iso(job.get("created_at"))
        if created is None:
            return None
        return (datetime.now(timezone.utc) - created).total_seconds()

    def _escalate_to_human_review(
        self,
        job: dict[str, Any],
        reason: str,
        stats: dict[str, Any],
    ) -> bool:
        """Record (or refresh) a human-review task for an inconclusive job.

        Non-blocking: the job stays open and is re-evaluated next cycle. Returns
        True if a brand-new task was created, False if it already existed.
        """
        job_id = str(job.get("job_id", "") or "")
        if not job_id:
            return False
        existing = self._validation_store.load_human_review_task(job_id)
        task = {
            "job_id": job_id,
            "status": "needs_human_review",
            "reason": reason,
            "skill_name": str(job.get("candidate_skill_name", "") or job.get("candidate_skill", {}).get("name", "")),
            "proposed_action": str(job.get("proposed_action", "") or ""),
            "rationale": str(job.get("rationale", "") or ""),
            "job_created_at": str(job.get("created_at", "") or ""),
            "validation_stats": stats,
            "review_endpoint": f"POST /api/v1/validation/jobs/{job_id}/review",
        }
        if existing:
            task["created_at"] = str(existing.get("created_at", "") or "")
            task["reminder_count"] = int(existing.get("reminder_count", 0) or 0) + 1
        else:
            task["reminder_count"] = 0
        self._validation_store.save_human_review_task(job_id, task)
        return not existing

    def _collect_human_review_summary(self) -> dict[str, Any]:
        """List outstanding human-review tasks for cycle reminders."""
        summary: dict[str, Any] = {
            "enabled": bool(self.config.human_review_enabled),
            "open_tasks": 0,
            "tasks": [],
        }
        if not self.config.human_review_enabled or self.config.publish_mode != "validated":
            return summary
        for task in self._validation_store.list_human_review_tasks():
            job_id = str(task.get("job_id", "") or "")
            if not job_id or self._validation_store.load_decision(job_id):
                continue
            summary["open_tasks"] += 1
            summary["tasks"].append(
                {
                    "job_id": job_id,
                    "skill_name": str(task.get("skill_name", "")),
                    "reason": str(task.get("reason", "")),
                    "reminder_count": int(task.get("reminder_count", 0) or 0),
                    "review_endpoint": str(task.get("review_endpoint", "")),
                }
            )
        return summary

    def _build_validation_evidence(self, sessions: list[dict[str, Any]]) -> list[dict[str, Any]]:
        evidence: list[dict[str, Any]] = []
        for session in sessions[:8]:
            item: dict[str, Any] = {
                "session_id": str(session.get("session_id", "")),
                "summary": str(session.get("_summary", "")),
                "user_alias": str(session.get("user_alias", "") or ""),
            }
            runtime_context = (
                session.get("runtime_context")
                if isinstance(session.get("runtime_context"), dict)
                else {}
            )
            evaluation_profile = str(
                runtime_context.get("evaluation_profile") or ""
            ).strip()
            if evaluation_profile:
                item["evaluation_profile"] = evaluation_profile
            skills = session.get("_skills_referenced")
            if skills:
                item["skills_referenced"] = sorted(str(s or "") for s in skills if str(s or ""))
            judge_scores = session.get("_judge_scores")
            if isinstance(judge_scores, dict) and isinstance(judge_scores.get("overall_score"), (int, float)):
                item["judge_overall_score"] = float(judge_scores["overall_score"])
            if isinstance(session.get("_avg_prm"), (int, float)):
                item["avg_prm"] = float(session["_avg_prm"])
            evidence.append(item)
        return evidence

    @staticmethod
    def _restore_validation_sessions(job: dict[str, Any]) -> list[dict[str, Any]]:
        """Rebuild verifier inputs from evidence persisted with a validation job."""
        sessions: dict[str, dict[str, Any]] = {}
        for raw in job.get("session_evidence") or []:
            if not isinstance(raw, dict):
                continue
            session_id = str(raw.get("session_id") or "")
            item: dict[str, Any] = {
                "session_id": session_id,
                "_summary": str(raw.get("summary") or ""),
                "turns": [],
            }
            evaluation_profile = str(
                raw.get("evaluation_profile") or ""
            ).strip()
            if evaluation_profile:
                item["runtime_context"] = {
                    "evaluation_profile": evaluation_profile,
                }
            if isinstance(raw.get("judge_overall_score"), (int, float)):
                item["_judge_scores"] = {
                    "overall_score": float(raw["judge_overall_score"])
                }
            if isinstance(raw.get("avg_prm"), (int, float)):
                item["_avg_prm"] = float(raw["avg_prm"])
            sessions[session_id] = item
        for raw in job.get("replay_cases") or []:
            if not isinstance(raw, dict):
                continue
            session_id = str(raw.get("session_id") or "")
            item = sessions.setdefault(
                session_id,
                {"session_id": session_id, "_summary": "", "turns": []},
            )
            item["turns"].append(
                {
                    "turn_num": int(raw.get("turn_num") or 0),
                    "prompt_text": str(raw.get("instruction") or ""),
                    "response_text": str(raw.get("reference_response") or ""),
                }
            )
        return list(sessions.values())

    def _build_replay_cases(self, sessions: list[dict[str, Any]]) -> list[dict[str, Any]]:
        preferred: list[dict[str, Any]] = []
        fallback: list[dict[str, Any]] = []

        for session in sessions[:6]:
            if is_candidate_audit_session(session):
                continue
            session_id = str(session.get("session_id", "") or "")
            runtime_context = (
                session.get("runtime_context")
                if isinstance(session.get("runtime_context"), dict)
                else {}
            )
            evaluation_profile = str(
                runtime_context.get("evaluation_profile") or ""
            ).strip()
            turns = session.get("turns") or []
            if not isinstance(turns, list):
                continue
            for turn in turns:
                if not isinstance(turn, dict):
                    continue
                instruction = str(turn.get("prompt_text", "") or "").strip()
                reference_response = str(turn.get("response_text", "") or "").strip()
                if not instruction or not reference_response:
                    continue
                case = {
                    "session_id": session_id,
                    "turn_num": int(turn.get("turn_num", 0) or 0),
                    "instruction": instruction[:3000],
                    "reference_response": reference_response[:4000],
                    "had_tool_calls": bool(turn.get("tool_calls")),
                    "had_tool_results": bool(turn.get("tool_results") or turn.get("tool_observations")),
                }
                if evaluation_profile:
                    case["evaluation_profile"] = evaluation_profile
                if int(turn.get("turn_num", 0) or 0) <= 1:
                    preferred.append(case)
                else:
                    fallback.append(case)
                if len(preferred) >= 3:
                    return preferred[:3]

        if preferred:
            return preferred[:3]
        return fallback[:3]

    def _queue_validation_job(
        self,
        skill: dict[str, Any],
        action_type: str,
        sessions: list[dict[str, Any]],
        rationale: str,
        source: str,
        *,
        current_skill: Optional[dict[str, Any]] = None,
        evidence_classification: Optional[dict[str, Any]] = None,
        evidence_key: str = "",
        evolution_context: Optional[dict[str, Any]] = None,
        replay_windows: Optional[dict[str, list[dict[str, Any]]]] = None,
        test_datasets: Optional[list[dict[str, Any]]] = None,
    ) -> dict[str, Any]:
        name = str(skill.get("name", "") or "")
        skill_id = self._id_registry.get_or_create(name)
        open_jobs = (
            self._validation_store.list_open_jobs_for_skill(name)
            if self.config.candidate_coalesce_enabled
            else []
        )
        existing = (
            max(
                open_jobs,
                key=lambda item: str(
                    item.get("updated_at") or item.get("created_at") or ""
                ),
            )
            if open_jobs
            else None
        )
        job_id = (
            str(existing.get("job_id") or "")
            if isinstance(existing, dict)
            else self._validation_store.make_job_id(name)
        )
        if not job_id:
            job_id = self._validation_store.make_job_id(name)
            existing = None
        for stale_job in open_jobs:
            stale_job_id = str(stale_job.get("job_id") or "")
            if not stale_job_id or stale_job_id == job_id:
                continue
            self._validation_store.save_decision(
                stale_job_id,
                {
                    "status": "superseded",
                    "reason": "replaced by the latest active candidate for this skill",
                    "superseded_by": job_id,
                },
            )

        def merge_unique(
            previous: list[Any],
            current: list[Any],
            key_builder,
        ) -> list[Any]:
            merged: list[Any] = []
            positions: dict[Any, int] = {}
            for item in list(previous or []) + list(current or []):
                key = key_builder(item)
                if key in positions:
                    merged[positions[key]] = item
                else:
                    positions[key] = len(merged)
                    merged.append(item)
            return merged

        current_session_ids = [
            str(session.get("session_id") or "")
            for session in sessions
            if str(session.get("session_id") or "").strip()
        ]
        source_sessions = {
            str(session.get("session_id") or ""): session
            for session in sessions
            if isinstance(session, dict)
            and str(session.get("session_id") or "").strip()
        }
        current_evidence = self._build_validation_evidence(sessions)
        normalized_windows: dict[str, list[dict[str, Any]]] = {}
        synthesized = [
            dict(item)
            for item in (test_datasets or [])
            if isinstance(item, dict) and item.get("query")
        ]
        if synthesized:
            normalized_windows = {"recent": [], "historical": []}
            for dataset in synthesized:
                case = dataset_to_replay_case(dataset)
                source_session = source_sessions.get(
                    str(case.get("session_id") or "")
                )
                if isinstance(source_session, dict):
                    case["source_runtime"] = (
                        dict(source_session.get("runtime"))
                        if isinstance(source_session.get("runtime"), dict)
                        else {}
                    )
                    runtime_context = (
                        source_session.get("runtime_context")
                        if isinstance(source_session.get("runtime_context"), dict)
                        else {}
                    )
                    case["source_runtime_context"] = {
                        key: runtime_context.get(key)
                        for key in (
                            "tenant_id",
                            "user_id",
                            "username",
                            "external_subject",
                            "agent_id",
                            "environment_id",
                            "source_session_id",
                            "model_config_id",
                            "evaluation_profile",
                            "profile_id",
                        )
                        if runtime_context.get(key) not in (None, "")
                    }
                window = str(case.get("evidence_window") or "recent")
                if window not in normalized_windows:
                    window = "recent"
                    case["evidence_window"] = window
                normalized_windows[window].append(case)
            replay_cases = (
                normalized_windows["recent"]
                + normalized_windows["historical"]
            )
        else:
            windows = replay_windows or {
                "recent": self._build_replay_cases(sessions),
                "historical": [],
            }
            for window in ("recent", "historical"):
                cases: list[dict[str, Any]] = []
                for raw in windows.get(window) or []:
                    if not isinstance(raw, dict):
                        continue
                    case = dict(raw)
                    case["evidence_window"] = window
                    cases.append(case)
                normalized_windows[window] = cases
            replay_cases = (
                normalized_windows["recent"]
                + normalized_windows["historical"]
            )
        if not replay_cases:
            replay_cases = self._build_replay_cases(sessions)
            for case in replay_cases:
                case["evidence_window"] = "recent"
            normalized_windows["recent"] = replay_cases
        runtime_preparation = prepare_runtime_validation(
            skill=skill,
            sessions=sessions,
            replay_cases=replay_cases,
            agents=list_agents(self.config),
        )
        replay_cases = list(runtime_preparation["replay_cases"])
        runtime_policy = dict(runtime_preparation["policy"])
        compatibility_cases = [
            case
            for case in replay_cases
            if str(case.get("evidence_window") or "") == "compatibility"
        ]
        if compatibility_cases:
            normalized_windows["compatibility"] = compatibility_cases
        skill = {**skill, "runtime_policy": runtime_policy}

        previous = existing if isinstance(existing, dict) else {}
        session_ids = merge_unique(
            list(previous.get("session_ids") or []),
            current_session_ids,
            lambda item: str(item or ""),
        )
        session_evidence = merge_unique(
            list(previous.get("session_evidence") or []),
            current_evidence,
            lambda item: str(item.get("session_id") or "")
            if isinstance(item, dict)
            else str(item),
        )
        revision = int(previous.get("candidate_revision") or 0) + 1
        now = datetime.now(timezone.utc).isoformat()
        progressive_max_interactions = max(
            [
                (
                    len(dataset.get("checklist") or [])
                    + max(
                        1,
                        int(
                            (
                                dataset.get("progressive_disclosure")
                                if isinstance(
                                    dataset.get("progressive_disclosure"),
                                    dict,
                                )
                                else {}
                            ).get("batch_size")
                            or self.config.dataset_disclosure_batch_size
                        ),
                    )
                    - 1
                )
                // max(
                    1,
                    int(
                        (
                            dataset.get("progressive_disclosure")
                            if isinstance(
                                dataset.get("progressive_disclosure"),
                                dict,
                            )
                            else {}
                        ).get("batch_size")
                        or self.config.dataset_disclosure_batch_size
                    ),
                )
                + 1
                for dataset in synthesized
            ]
            or [4]
        )
        job = {
            "job_id": job_id,
            "status": "pending_validation",
            "created_at": str(previous.get("created_at") or now),
            "updated_at": now,
            "candidate_revision": revision,
            "candidate_skill_name": name,
            "candidate_skill_id": skill_id,
            "candidate_skill": skill,
            "current_skill": current_skill,
            "proposed_action": action_type,
            "source": source,
            "rationale": rationale,
            "evidence_classification": (
                evidence_classification
                if isinstance(evidence_classification, dict)
                else {}
            ),
            "session_ids": session_ids,
            "session_evidence": session_evidence,
            "test_datasets": synthesized,
            "max_interactions": min(
                20,
                max(1, progressive_max_interactions),
            ),
            "replay_cases": replay_cases,
            "replay_case_windows": normalized_windows,
            "runtime_validation_policy": runtime_policy,
            "evidence_key": str(evidence_key or name),
            "evolution_context": evolution_context or {},
            "coalesced_count": int(previous.get("coalesced_count") or 0)
            + (1 if previous else 0),
            "min_results": self.config.validation_required_results,
            "min_approvals": self.config.validation_required_approvals,
            "max_rejections": self.config.validation_max_rejections,
        }
        candidate_bundle = candidate_skill_bundle(skill)
        if set(candidate_bundle) != {"SKILL.md"}:
            job["candidate_protocol_version"] = 2
            job["candidate_bundle_tree_sha256"] = bundle_tree_sha256(
                candidate_bundle
            )
            job["required_validator_capabilities"] = [
                "bundle_v1",
                "bundle_static_v1",
                "bundle_true_replay_v1",
            ]
        if previous:
            self._validation_store.reset_job_artifacts(job_id)
        self._validation_store.save_job(job)
        if synthesized:
            SynthesizedDatasetStore(
                self._skill_bucket,
                prefix=self._skill_prefix,
            ).save_generation(
                skill_name=name,
                generation_id=job_id,
                datasets=synthesized,
                source_session_ids=session_ids,
                candidate_revision=revision,
            )
        logger.info(
            "[EvolveServer] %s validation job %s for skill '%s' revision=%d",
            "updated" if previous else "queued",
            job_id,
            name,
            revision,
        )
        return {
            "action": (
                "updated_validation_candidate"
                if previous
                else "queued_for_validation"
            ),
            "proposed_action": action_type,
            "skill_name": name,
            "skill_id": skill_id,
            "version": None,
            "session_ids": job["session_ids"],
            "rationale": rationale,
            "evidence_classification": job["evidence_classification"],
            "source": source,
            "edit_summary": skill.get("edit_summary"),
            "file_changes": skill.get("file_changes") or [],
            "uploaded": False,
            "validation_job_id": job_id,
            "candidate_revision": revision,
            "coalesced": bool(previous),
            "test_dataset_count": len(synthesized),
            "test_dataset_ids": [
                str(item.get("dataset_id") or "") for item in synthesized
            ],
        }

    async def _finalize_validation_jobs(self) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        summary = self._empty_validation_publish_summary()
        if self.config.publish_mode != "validated":
            return [], summary

        records: list[dict[str, Any]] = []
        for job in self._validation_store.list_jobs():
            summary["jobs_scanned"] += 1
            job_id = str(job.get("job_id", "") or "")
            if not job_id:
                continue
            if self._validation_store.load_decision(job_id):
                continue

            revision = max(1, int(job.get("candidate_revision") or 1))
            required_capabilities = {
                str(item)
                for item in (job.get("required_validator_capabilities") or [])
                if str(item)
            }
            expected_tree = str(
                job.get("candidate_bundle_tree_sha256") or ""
            )
            results = [
                result
                for result in self._validation_store.list_results(job_id)
                if (
                    int(result.get("candidate_revision") or 1) == revision
                    and (
                        result.get("candidate_revision") is not None
                        or revision == 1
                    )
                    and required_capabilities.issubset(
                        {
                            str(item)
                            for item in (
                                result.get("validator_capabilities") or []
                            )
                        }
                    )
                    and (
                        not expected_tree
                        or str(
                            result.get("candidate_bundle_tree_sha256") or ""
                        )
                        == expected_tree
                    )
                )
            ]
            if not results:
                summary["pending"] += 1
                continue

            accepted = 0
            rejected = 0
            inconclusive = 0
            for result in results:
                result_decision = str(result.get("decision") or "").lower()
                if result.get("accepted") is True or result_decision == "accept":
                    accepted += 1
                elif result_decision == "reject" or result.get("rejected") is True:
                    rejected += 1
                else:
                    inconclusive += 1

            runtime_gate = evaluate_runtime_compatibility(
                (
                    job.get("runtime_validation_policy")
                    if isinstance(job.get("runtime_validation_policy"), dict)
                    else {}
                ),
                results,
            )
            publish_ready = (
                len(results) >= self.config.validation_required_results
                and accepted >= self.config.validation_required_approvals
                and runtime_gate["status"] == "passed"
            )
            reject_ready = (
                rejected >= self.config.validation_max_rejections
                or runtime_gate["status"] == "rejected"
            )

            if publish_ready:
                candidate_skill = job.get("candidate_skill")
                if not isinstance(candidate_skill, dict) or not candidate_skill.get("name"):
                    self._validation_store.save_decision(
                        job_id,
                        {
                            "status": "rejected",
                            "reason": "candidate skill payload missing",
                            "result_count": len(results),
                            "accepted_count": accepted,
                            "rejected_count": rejected,
                            "inconclusive_count": inconclusive,
                        },
                    )
                    summary["rejected"] += 1
                    continue
                action_type = str(job.get("proposed_action", DecisionAction.CREATE) or DecisionAction.CREATE)
                actual_action, uploaded = await self._resolve_and_upload(candidate_skill, action_type)
                skill_name = str(candidate_skill.get("name", ""))
                published_version = self._id_registry.get_version(skill_name)
                best_result = max(
                    results,
                    key=lambda item: str(
                        item.get("created_at")
                        or item.get("evaluated_at")
                        or ""
                    ),
                )
                self._validation_store.save_decision(
                    job_id,
                    {
                        "status": "published" if uploaded else "skipped",
                        "published_action": actual_action,
                        "skill_name": skill_name,
                        "version": published_version if uploaded else None,
                        "result_count": len(results),
                        "accepted_count": accepted,
                        "rejected_count": rejected,
                        "inconclusive_count": inconclusive,
                        "runtime_gate": runtime_gate,
                        "evaluation": best_result,
                    },
                )
                if uploaded:
                    summary["published"] += 1
                    await self._mark_evidence_published(
                        str(job.get("evidence_key") or candidate_skill.get("name") or ""),
                        job_id,
                    )
                else:
                    summary["skipped"] += 1
                records.append(
                    {
                        "action": "published_after_validation" if uploaded else actual_action,
                        "published_action": actual_action,
                        "skill_name": skill_name,
                        "skill_id": self._id_registry.get_or_create(skill_name),
                        "version": published_version,
                        "session_ids": list(job.get("session_ids") or []),
                        "rationale": str(job.get("rationale", "") or ""),
                        "evidence_classification": job.get("evidence_classification") or {},
                        "source": "validation_publish",
                        "uploaded": uploaded,
                        "validation_job_id": job_id,
                        "validation_results": {
                            "result_count": len(results),
                            "accepted_count": accepted,
                            "rejected_count": rejected,
                            "inconclusive_count": inconclusive,
                            "runtime_gate": runtime_gate,
                        },
                    }
                )
                continue

            if reject_ready:
                self._validation_store.save_decision(
                    job_id,
                    {
                        "status": "rejected",
                        "reason": "client validation rejected candidate",
                        "result_count": len(results),
                        "accepted_count": accepted,
                        "rejected_count": rejected,
                        "inconclusive_count": inconclusive,
                        "runtime_gate": runtime_gate,
                    },
                )
                summary["rejected"] += 1
                records.append(
                    {
                        "action": "validation_rejected",
                        "proposed_action": str(job.get("proposed_action", "")),
                        "skill_name": str(job.get("candidate_skill_name", "")),
                        "skill_id": str(job.get("candidate_skill_id", "")),
                        "version": None,
                        "session_ids": list(job.get("session_ids") or []),
                        "rationale": str(job.get("rationale", "") or ""),
                        "evidence_classification": job.get("evidence_classification") or {},
                        "source": "validation_publish",
                        "uploaded": False,
                        "validation_job_id": job_id,
                        "validation_results": {
                            "result_count": len(results),
                            "accepted_count": accepted,
                            "rejected_count": rejected,
                        },
                    }
                )
                continue

            # Gray zone: client replay/AB results are in but cross neither the
            # publish nor the reject bar, OR the job has been pending too long
            # without enough results. Escalate to a human review queue instead
            # of leaving it pending forever. This does NOT block: the job stays
            # open and keeps being re-evaluated next cycle until a human (or
            # more client results) resolves it.
            stats = {
                "result_count": len(results),
                "accepted_count": accepted,
                "rejected_count": rejected,
                "inconclusive_count": inconclusive,
                "required_results": self.config.validation_required_results,
                "required_approvals": self.config.validation_required_approvals,
                "runtime_gate": runtime_gate,
            }
            escalation_reason = ""
            if runtime_gate["status"] in {
                "blocked",
                "pending",
                "inconclusive",
            }:
                escalation_reason = runtime_gate["reason"]
            elif len(results) >= self.config.validation_required_results:
                escalation_reason = (
                    "client validation inconclusive: enough results collected but "
                    "candidate is neither clearly better nor clearly worse than baseline"
                )
            else:
                age = self._job_age_seconds(job)
                if age is not None and age >= self.config.human_review_pending_timeout_seconds:
                    escalation_reason = (
                        f"validation job pending {int(age)}s without enough results "
                        f"(timeout={self.config.human_review_pending_timeout_seconds}s)"
                    )

            if self.config.human_review_enabled and escalation_reason:
                summary["inconclusive"] += inconclusive
                created_new = self._escalate_to_human_review(job, escalation_reason, stats)
                if created_new:
                    summary["escalated_to_human"] += 1
                    records.append(
                        {
                            "action": "escalated_to_human_review",
                            "proposed_action": str(job.get("proposed_action", "")),
                            "skill_name": str(job.get("candidate_skill_name", "")),
                            "skill_id": str(job.get("candidate_skill_id", "")),
                            "version": None,
                            "session_ids": list(job.get("session_ids") or []),
                            "rationale": str(job.get("rationale", "") or ""),
                            "source": "human_review",
                            "uploaded": False,
                            "validation_job_id": job_id,
                            "human_review_reason": escalation_reason,
                            "validation_results": stats,
                        }
                    )

            summary["pending"] += 1

        return records, summary

    # ------------------------------------------------------------------ #
    # Reviewer-triggered replay validation + publish.                     #
    #                                                                     #
    # The scheduled path (``_finalize_validation_jobs``) waits for N      #
    # distributed client votes. StaffDeck instead exposes a "验证并发布"   #
    # button: a human injects the candidate skill, replays the *same*     #
    # sessions the skill was evolved from, and — if the replay passes —   #
    # publishes immediately. One authoritative reviewer decision, no      #
    # quorum wait.                                                        #
    # ------------------------------------------------------------------ #

    def _validation_candidate_payload(
        self,
        job: dict[str, Any],
        *,
        evaluation: Optional[dict[str, Any]] = None,
        decision: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        """Project a validation job, result, and decision for read APIs."""
        job_id = str(job.get("job_id", "") or "")
        candidate = (
            job.get("candidate_skill")
            if isinstance(job.get("candidate_skill"), dict)
            else {}
        )
        decision = (
            self._validation_store.load_decision(job_id) or {}
            if decision is None
            else decision
        )
        should_load_latest_result = evaluation is None
        if should_load_latest_result:
            evaluation = (
                self._validation_store.load_best_evaluation(job_id, job) or {}
            )
        replay = (
            evaluation.get("replay")
            if isinstance(evaluation.get("replay"), dict)
            else evaluation.get("replay_summary")
            if isinstance(evaluation.get("replay_summary"), dict)
            else {}
        )
        raw_efficiency = (
            replay.get("efficiency")
            if isinstance(replay.get("efficiency"), dict)
            else {}
        )
        raw_dimensions = (
            raw_efficiency.get("dimensions")
            if isinstance(raw_efficiency.get("dimensions"), dict)
            else {}
        )
        metric_names = (
            "interaction_turns",
            "tool_call_count",
            "total_tokens",
        )
        dimensions: dict[str, dict[str, Any]] = {}
        baseline_metrics: dict[str, int] = {}
        candidate_metrics: dict[str, int] = {}
        for name in metric_names:
            raw_metric = (
                raw_dimensions.get(name)
                if isinstance(raw_dimensions.get(name), dict)
                else {}
            )
            baseline_value = int(
                raw_metric.get("baseline")
                if raw_metric.get("baseline") is not None
                else (raw_efficiency.get("baseline") or {}).get(name)
                or 0
            )
            candidate_value = int(
                raw_metric.get("candidate")
                if raw_metric.get("candidate") is not None
                else (raw_efficiency.get("candidate") or {}).get(name)
                or 0
            )
            delta = baseline_value - candidate_value
            baseline_metrics[name] = baseline_value
            candidate_metrics[name] = candidate_value
            dimensions[name] = {
                "baseline": baseline_value,
                "candidate": candidate_value,
                "delta": delta,
                "reduction_ratio": round(
                    max(-1.0, min(1.0, delta / max(1, baseline_value))),
                    4,
                ),
                "winner": (
                    "candidate"
                    if delta > 0
                    else "baseline"
                    if delta < 0
                    else "tie"
                ),
            }
        sanitized_efficiency = {
            "baseline": baseline_metrics,
            "candidate": candidate_metrics,
            "dimensions": dimensions,
        }
        normalized_cases: list[dict[str, Any]] = []
        for raw_case in replay.get("cases") or []:
            if not isinstance(raw_case, dict):
                continue
            normalized_case: dict[str, Any] = {}
            for branch_name in ("baseline", "candidate"):
                branch = (
                    raw_case.get(branch_name)
                    if isinstance(raw_case.get(branch_name), dict)
                    else {}
                )
                normalized_case[branch_name] = {
                    "session_id": str(branch.get("session_id") or ""),
                    "turn_num": int(branch.get("turn_num") or 0),
                    "instruction": str(branch.get("instruction") or ""),
                    "response": str(
                        branch.get("response")
                        or branch.get("final_response")
                        or branch.get("response_text")
                        or branch.get("trajectory")
                        or branch.get("rationale")
                        or ""
                    ),
                    "error": str(branch.get("error") or ""),
                    "interaction_turns": int(
                        branch.get("interaction_turns") or 0
                    ),
                    "tool_call_count": int(
                        branch.get("tool_call_count") or 0
                    ),
                    "total_tokens": int(branch.get("total_tokens") or 0),
                    "interactions": branch.get("interactions") or [],
                    "checklist_report": branch.get("checklist_report") or {},
                }
            normalized_cases.append(normalized_case)
        branch_checklists = (
            replay.get("checklist")
            if isinstance(replay.get("checklist"), dict)
            else {
                branch: aggregate_case_checklists(
                    normalized_cases,
                    branch=branch,
                )
                for branch in ("baseline", "candidate")
            }
        )
        metric_policy = progressive_replay_decision(
            efficiency=sanitized_efficiency,
            baseline_checklist=branch_checklists.get("baseline") or {},
            candidate_checklist=branch_checklists.get("candidate") or {},
        )
        sanitized_replay = {
            "status": str(replay.get("status") or "evaluated"),
            "mode": str(replay.get("mode") or "true_replay"),
            "accepted": bool(metric_policy.get("accepted")),
            "verdict": str(metric_policy.get("verdict") or "inconclusive"),
            "no_regression": bool(metric_policy.get("no_regression")),
            "case_count": int(
                replay.get("case_count") or len(normalized_cases)
            ),
            "cases": normalized_cases,
            "efficiency": sanitized_efficiency,
            "checklist": branch_checklists,
            "decision_policy": metric_policy,
            "reason": str(metric_policy.get("verdict") or "inconclusive"),
            "error": replay.get("error"),
        }
        evaluation_payload = {
            "validator_mode": str(
                evaluation.get("validator_mode") or "true_replay"
            ),
            "decision": sanitized_replay["verdict"],
            "accepted": sanitized_replay["accepted"],
            "reason": sanitized_replay["reason"],
            "created_at": str(evaluation.get("created_at") or ""),
            "replay": sanitized_replay,
        } if replay else {}
        review_status = str(decision.get("status") or "").strip()
        if not review_status:
            review_status = sanitized_replay["verdict"] if replay else "open"
        replay_cases = [
            case
            for case in (job.get("replay_cases") or [])
            if isinstance(case, dict)
        ]
        test_datasets = [
            dataset
            for dataset in (job.get("test_datasets") or [])
            if isinstance(dataset, dict)
        ]
        sanitized_decision = {
            key: decision.get(key)
            for key in (
                "status",
                "accepted",
                "reason",
                "decided_at",
                "job_id",
                "skill_name",
                "version",
                "mode",
                "reviewer",
            )
            if decision.get(key) is not None
        }
        payload = {
            **job,
            "job_id": job_id,
            "skill_name": str(
                candidate.get("name")
                or job.get("candidate_skill_name")
                or ""
            ),
            "skill_id": str(
                candidate.get("skill_id")
                or job.get("candidate_skill_id")
                or ""
            ),
            "description": str(candidate.get("description") or ""),
            "proposed_action": str(
                job.get("proposed_action") or DecisionAction.CREATE
            ),
            "rationale": str(job.get("rationale") or ""),
            "session_ids": list(job.get("session_ids") or []),
            "replay_case_count": int(
                replay.get("case_count") or len(replay_cases)
            ),
            "test_dataset_count": len(test_datasets),
            "test_dataset_ids": [
                str(item.get("dataset_id") or "") for item in test_datasets
            ],
            "created_at": str(job.get("created_at") or ""),
            "content_preview": str(candidate.get("content") or "")[:600],
            "review_status": review_status,
            "decision": sanitized_decision,
            "decided_at": str(decision.get("decided_at") or ""),
            "evaluation": evaluation_payload,
            "replay_verdict": sanitized_replay["verdict"] if replay else "",
            "efficiency": sanitized_efficiency if replay else {},
        }
        for key in (
            "min_score",
            "checklist",
            "inherited_checklists",
            "evolution_context",
            "session_evidence",
            "replay_cases",
            "test_datasets",
            "verification",
            "verify_score",
            "replay_score",
            "baseline_score",
            "threshold",
        ):
            payload.pop(key, None)
        return payload

    def _list_validation_candidates(
        self,
        scope: str = "open",
    ) -> list[dict[str, Any]]:
        """Validation jobs with automatic replay evidence projected inline."""
        normalized = str(scope or "open").strip().lower()
        if normalized in {"history", "closed", "decided"}:
            normalized = "processed"
        if normalized not in {"open", "processed", "all"}:
            normalized = "open"
        candidates: list[dict[str, Any]] = []
        seen: set[str] = set()
        if normalized in {"processed", "all"}:
            for record in self._validation_store.list_decision_records(
                reconcile=False,
            ):
                job = (
                    record.get("job")
                    if isinstance(record.get("job"), dict)
                    else {}
                )
                job_id = str(
                    job.get("job_id") or record.get("job_id") or ""
                )
                if not job_id:
                    continue
                job = {**job, "job_id": job_id}
                candidates.append(
                    self._validation_candidate_payload(
                        job,
                        evaluation=(
                            record.get("evaluation")
                            if isinstance(record.get("evaluation"), dict)
                            else {}
                        ),
                        decision=(
                            record.get("decision")
                            if isinstance(record.get("decision"), dict)
                            else {}
                        ),
                    )
                )
                seen.add(job_id)
        if normalized in {"open", "all"}:
            for job in self._validation_store.list_open_jobs():
                job_id = str(job.get("job_id", "") or "")
                if not job_id or job_id in seen:
                    continue
                evaluation = (
                    self._validation_store.load_fresh_evaluation(
                        job_id,
                        job,
                    )
                    or {}
                )
                if not evaluation:
                    revision = max(
                        1,
                        int(job.get("candidate_revision") or 1),
                    )
                    results = [
                        result
                        for result in self._validation_store.list_results(
                            job_id
                        )
                        if max(
                            1,
                            int(result.get("candidate_revision") or 1),
                        )
                        == revision
                    ]
                    evaluation = results[-1] if results else {}
                candidates.append(
                    self._validation_candidate_payload(
                        job,
                        evaluation=evaluation,
                        decision={},
                    )
                )
        candidates.sort(key=lambda item: str(item.get("created_at", "")), reverse=True)
        return candidates

    @staticmethod
    def _aggregate_replay_windows(
        window_results: list[tuple[str, dict[str, Any]]],
        *,
        max_interactions: int,
    ) -> dict[str, Any]:
        if not window_results:
            raise ValueError("true replay produced no window results")
        evaluated = [
            (window, verdict)
            for window, verdict in window_results
            if str(verdict.get("status") or "") in {"", "evaluated"}
        ]
        if not evaluated:
            verdict = dict(window_results[0][1])
            verdict.setdefault("no_regression", False)
            verdict.setdefault("accepted", False)
            verdict["window_results"] = {
                window: result for window, result in window_results
            }
            return verdict

        all_windows_evaluated = len(evaluated) == len(window_results)

        cases: list[dict[str, Any]] = []
        for window, verdict in evaluated:
            for raw_case in verdict.get("cases") or []:
                if not isinstance(raw_case, dict):
                    continue
                case = dict(raw_case)
                case["evidence_window"] = window
                cases.append(case)
        efficiency_inputs = {"baseline": {}, "candidate": {}}
        for _, verdict in evaluated:
            report = (
                verdict.get("efficiency")
                if isinstance(verdict.get("efficiency"), dict)
                else {}
            )
            for branch in ("baseline", "candidate"):
                values = (
                    report.get(branch)
                    if isinstance(report.get(branch), dict)
                    else {}
                )
                for key in (
                    "interaction_turns",
                    "tool_call_count",
                    "total_tokens",
                ):
                    efficiency_inputs[branch][key] = int(
                        efficiency_inputs[branch].get(key) or 0
                    ) + int(values.get(key) or 0)
        from ...true_replay import compare_efficiency

        efficiency = compare_efficiency(
            efficiency_inputs["baseline"],
            efficiency_inputs["candidate"],
        )
        branch_checklists = {
            branch: aggregate_case_checklists(cases, branch=branch)
            for branch in ("baseline", "candidate")
        }
        policy = progressive_replay_decision(
            efficiency=efficiency,
            baseline_checklist=branch_checklists["baseline"],
            candidate_checklist=branch_checklists["candidate"],
        )
        accepted = bool(policy.get("accepted")) and all_windows_evaluated
        verdict = str(policy.get("verdict") or "inconclusive")
        if not all_windows_evaluated and verdict == "accept":
            verdict = "inconclusive"
        no_regression = (
            bool(policy.get("no_regression")) and all_windows_evaluated
        )
        return {
            "status": "evaluated",
            "mode": "true_replay",
            "accepted": accepted,
            "verdict": verdict,
            "no_regression": no_regression,
            "max_interactions": max_interactions,
            "case_count": sum(
                int(verdict.get("case_count") or 0)
                for _, verdict in evaluated
            ),
            "cases": cases,
            "efficiency": efficiency,
            "checklist": branch_checklists,
            "decision_policy": {
                **policy,
                "accepted": accepted,
                "all_windows_evaluated": all_windows_evaluated,
            },
            "window_results": {
                window: verdict for window, verdict in window_results
            },
            "reason": verdict,
        }

    async def _run_candidate_replay(self, job: dict[str, Any]) -> dict[str, Any]:
        """Replay a job's originating case(s) with vs. without the candidate skill.

        This is a **true replay**: it spins up two real Hermes agents in isolated,
        disposable sandboxes (both ``HOME`` and ``HERMES_HOME`` redirected into a
        throwaway temp dir) that differ only in whether the candidate skill is
        injected, and lets each run the full tool loop for real. The verdict
        uses only interaction turns, tool calls, and total tokens.

        The heavy lifting runs in a subprocess because Hermes caches
        ``HERMES_HOME`` at import time, so it must be set before the interpreter
        imports the agent. We shell out to ``teamEvolver.true_replay
        --json`` and parse its framed metric verdict."""
        candidate_skill = job.get("candidate_skill")
        if not isinstance(candidate_skill, dict) or not candidate_skill.get("name"):
            raise ValueError("validation job missing candidate_skill")
        job_id = str(job.get("job_id") or job.get("id") or "").strip()
        if not job_id:
            raise ValueError("validation job missing job_id for true replay")
        replay_cases = [
            case
            for case in (job.get("replay_cases") or [])
            if isinstance(case, dict)
        ]
        if not replay_cases:
            raise ValueError("validation job has no replay_cases to replay")

        max_interactions = max(1, int(job.get("max_interactions") or 4))
        worker_python = (
            os.environ.get("SKILLGENE_REPLAY_PYTHON", "").strip()
            or os.environ.get("CONDA_PYTHON_EXE", "").strip()
            or sys.executable
        )
        base_cmd = [
            worker_python, "-m", "teamEvolver.true_replay",
            "--job-id", job_id, "--json",
            "--timeout", str(self.TRUE_REPLAY_TIMEOUT),
            "--max-interactions", str(max_interactions),
        ]

        def _invoke(case_index: int) -> dict[str, Any]:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                suffix=".json",
                prefix="true_replay_job_",
            ) as job_file:
                json.dump(job, job_file, ensure_ascii=False)
                job_file.flush()
                cmd = [
                    *base_cmd,
                    "--job-file",
                    job_file.name,
                    "--case",
                    str(case_index),
                ]
                proc = subprocess.run(
                    cmd, cwd=str(_REPO_ROOT), capture_output=True, text=True,
                    timeout=self.TRUE_REPLAY_TIMEOUT * 2 + 120,
                )
            out = proc.stdout or ""
            begin, end = out.find("TRUE_REPLAY_JSON_BEGIN"), out.rfind("TRUE_REPLAY_JSON_END")
            if begin < 0 or end < 0:
                raise ValueError(
                    "true replay produced no verdict "
                    f"(rc={proc.returncode}); stderr tail: {(proc.stderr or '')[-500:]}"
                )
            payload = out[begin + len("TRUE_REPLAY_JSON_BEGIN"): end].strip()
            return json.loads(payload)

        selected = select_replay_cases(replay_cases)
        window_results: list[tuple[str, dict[str, Any]]] = []
        results_by_runtime: dict[
            str,
            list[tuple[str, dict[str, Any]]],
        ] = {}
        for window, case_index in selected:
            try:
                verdict = await asyncio.to_thread(_invoke, case_index)
            except subprocess.TimeoutExpired as exc:
                raise ValueError(
                    f"true replay timed out after {exc.timeout}s for {window} window"
                ) from exc
            window_results.append((window, verdict))
            runtime_type = (
                runtime_type_for_case(replay_cases[case_index])
                or "unknown"
            )
            results_by_runtime.setdefault(runtime_type, []).append(
                (window, verdict)
            )
        aggregate = self._aggregate_replay_windows(
            window_results,
            max_interactions=max_interactions,
        )
        runtime_validation = {
            runtime_type: self._aggregate_replay_windows(
                runtime_results,
                max_interactions=max_interactions,
            )
            for runtime_type, runtime_results in results_by_runtime.items()
        }
        runtime_gate = evaluate_runtime_compatibility(
            (
                job.get("runtime_validation_policy")
                if isinstance(job.get("runtime_validation_policy"), dict)
                else {}
            ),
            [{"runtime_validation": runtime_validation}],
        )
        aggregate["runtime_validation"] = runtime_validation
        aggregate["runtime_gate"] = runtime_gate
        if runtime_gate["status"] != "passed":
            aggregate["accepted"] = False
            aggregate["verdict"] = (
                "reject"
                if runtime_gate["status"] == "rejected"
                else "inconclusive"
            )
            aggregate["reason"] = runtime_gate["reason"]
        return aggregate

    async def review_validate_candidate(
        self, job_id: str, *, reviewer: str = "staffdeck-reviewer", mode: str = "auto"
    ) -> dict[str, Any]:
        """Replay-validate one candidate and publish it.

        Serialized behind the cycle lock (publishing mutates the shared
        registry/manifest, like ``/trigger``). Returns a detail payload the
        dashboard renders. Idempotent-ish: an already-decided job is returned
        as-is instead of being re-published.

        ``mode``:
          * ``"auto"`` — interaction turns decide first. When turns tie,
            tool calls and total tokens decide.
          * ``"force"`` — always publish after replay; metrics remain visible.
        """
        mode = "force" if str(mode).lower() == "force" else "auto"
        async with self._get_run_lock():
            job = self._validation_store.load_job(job_id)
            if job is None:
                return {"status": "not_found", "job_id": job_id}
            existing = self._validation_store.load_decision(job_id)
            if existing:
                return {
                    "status": str(existing.get("status") or "decided"),
                    "job_id": job_id,
                    "already_decided": True,
                    "decision": existing,
                }

            try:
                replay = await self._run_candidate_replay(job)
            except ValueError as exc:
                return {"status": "error", "job_id": job_id, "error": str(exc)}

            runtime_gate = (
                replay.get("runtime_gate")
                if isinstance(replay.get("runtime_gate"), dict)
                else evaluate_runtime_compatibility(
                    (
                        job.get("runtime_validation_policy")
                        if isinstance(
                            job.get("runtime_validation_policy"),
                            dict,
                        )
                        else {}
                    ),
                    [],
                )
            )
            publish = (
                True if mode == "force" else bool(replay.get("accepted"))
            ) and runtime_gate.get("status") == "passed"
            candidate_revision = max(1, int(job.get("candidate_revision") or 1))
            policy = (
                replay.get("decision_policy")
                if isinstance(replay.get("decision_policy"), dict)
                else {}
            )
            metric_changes = (
                policy.get("metric_changes")
                if isinstance(policy.get("metric_changes"), dict)
                else {}
            )

            result_record = {
                "validator_mode": f"reviewer_replay:{mode}",
                "candidate_revision": candidate_revision,
                "accepted": publish,
                "decision": "accept" if publish else str(
                    replay.get("verdict") or "inconclusive"
                ),
                "reason": ", ".join(
                    f"{name}: {item.get('baseline', 0)} -> "
                    f"{item.get('candidate', 0)}"
                    for name, item in metric_changes.items()
                    if isinstance(item, dict)
                ),
                "replay_summary": replay,
                "runtime_validation": dict(
                    replay.get("runtime_validation") or {}
                ),
                "runtime_gate": runtime_gate,
            }
            await self._call_storage(
                self._validation_store.save_result, job_id, reviewer, result_record
            )

            if not publish:
                verdict = str(replay.get("verdict") or "inconclusive")
                if verdict != "reject":
                    return {
                        "status": "inconclusive",
                        "job_id": job_id,
                        "mode": mode,
                        "replay": replay,
                    }
                decision = {
                    "status": "rejected",
                    "reason": str(
                        policy.get("decision_basis")
                        or "True Replay metrics rejected the candidate"
                    ),
                    "result_count": 1,
                    "accepted_count": 0,
                    "rejected_count": 1,
                    "reviewed_by": reviewer,
                    "mode": mode,
                    "runtime_gate": runtime_gate,
                    "evaluation": result_record,
                }
                await self._call_storage(self._validation_store.save_decision, job_id, decision)
                return {
                    "status": "rejected",
                    "job_id": job_id,
                    "mode": mode,
                    "replay": replay,
                    "decision": decision,
                }

            candidate_skill = job.get("candidate_skill")
            action_type = str(job.get("proposed_action", DecisionAction.CREATE) or DecisionAction.CREATE)
            actual_action, uploaded = await self._resolve_and_upload(candidate_skill, action_type)
            # Persist the registry immediately so the freshly published version
            # survives independent of the next evolution cycle.
            await self._call_storage(
                self._id_registry.save_to_oss, self._skill_bucket, self._skill_prefix
            )
            skill_name = str(candidate_skill.get("name", ""))
            decision = {
                "status": "published" if uploaded else "skipped",
                "published_action": actual_action,
                "result_count": 1,
                "accepted_count": 1,
                "rejected_count": 0,
                "reviewed_by": reviewer,
                "mode": mode,
                "runtime_gate": runtime_gate,
                "evaluation": result_record,
            }
            await self._call_storage(self._validation_store.save_decision, job_id, decision)
            if uploaded:
                await self._mark_evidence_published(
                    str(job.get("evidence_key") or skill_name),
                    job_id,
                )
            return {
                "status": "published" if uploaded else "skipped",
                "job_id": job_id,
                "mode": mode,
                "skill_name": skill_name,
                "skill_id": self._id_registry.get_or_create(skill_name),
                "version": self._id_registry.get_version(skill_name),
                "published_action": actual_action,
                "uploaded": uploaded,
                "replay": replay,
                "decision": decision,
            }

    # ------------------------------------------------------------------ #
    # Non-binding candidate True Replay evaluation (no publish).          #
    # ------------------------------------------------------------------ #

    def _schedule_candidate_evaluation(self, job_id: str) -> None:
        """Fire-and-forget a true-replay evaluation for a freshly queued candidate.

        Real replays run full tool loops (minutes each), so we do NOT block the
        evolution cycle on them. The task populates the cached evaluation the
        dashboard reads; failures are logged, never raised into the cycle."""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            logger.debug("[EvolveServer] no running loop; skip auto-eval for %s", job_id)
            return

        existing = self._eval_jobs.get(job_id)
        if existing is not None and not existing.done():
            self._eval_refresh_pending.add(job_id)
            logger.info(
                "[EvolveServer] candidate %s changed during evaluation; queued one refresh",
                job_id,
            )
            return

        async def _run() -> None:
            try:
                while True:
                    self._eval_refresh_pending.discard(job_id)
                    logger.info("[EvolveServer] auto true-replay evaluation started for %s", job_id)
                    result = await self.evaluate_candidate(job_id, refresh=True)
                    logger.info(
                        "[EvolveServer] auto true-replay evaluation done for %s: "
                        "status=%s verdict=%s",
                        job_id,
                        result.get("status"),
                        (result.get("replay") or {}).get("verdict"),
                    )
                    if job_id not in self._eval_refresh_pending:
                        break
            except Exception as exc:  # noqa: BLE001 - never surface into the cycle.
                logger.warning("[EvolveServer] auto true-replay evaluation failed for %s: %s", job_id, exc)
            finally:
                self._eval_jobs.pop(job_id, None)
                self._eval_refresh_pending.discard(job_id)

        task = loop.create_task(_run())
        self._eval_jobs[job_id] = task
        self._eval_tasks.add(task)
        task.add_done_callback(self._eval_tasks.discard)

    async def evaluate_candidate(
        self, job_id: str, *, refresh: bool = False
    ) -> dict[str, Any]:
        """Compare a candidate using True Replay execution metrics only.

        Returns the cached evaluation when one exists (unless ``refresh`` is
        set), so the dashboard can poll cheaply. The verdict is advisory only:
        publishing remains an explicit reviewer action via
        :meth:`review_validate_candidate`.
        """
        job = await self._call_storage(self._validation_store.load_job, job_id)
        if job is None:
            return {"status": "not_found", "job_id": job_id}
        candidate_revision = max(1, int(job.get("candidate_revision") or 1))

        if not refresh:
            cached = await self._call_storage(self._validation_store.load_evaluation, job_id)
            if cached and int(cached.get("candidate_revision") or 1) == candidate_revision:
                cached.setdefault("status", "evaluated")
                cached["cached"] = True
                return cached

        candidate_skill = job.get("candidate_skill") if isinstance(job.get("candidate_skill"), dict) else {}
        action_type = str(job.get("proposed_action", DecisionAction.CREATE) or DecisionAction.CREATE)

        # A/B replay (baseline vs. candidate over the originating cases).
        try:
            replay = await self._run_candidate_replay(job)
        except ValueError as exc:
            replay = {"error": str(exc)}

        replay_ok = bool(isinstance(replay, dict) and replay.get("accepted"))
        latest = await self._call_storage(self._validation_store.load_job, job_id)
        latest_revision = (
            max(1, int(latest.get("candidate_revision") or 1))
            if isinstance(latest, dict)
            else 0
        )
        if latest_revision != candidate_revision:
            return {
                "status": "stale",
                "job_id": job_id,
                "candidate_revision": candidate_revision,
                "latest_revision": latest_revision,
            }
        evaluation = {
            "status": "evaluated",
            "job_id": job_id,
            "candidate_revision": candidate_revision,
            "skill_name": str(candidate_skill.get("name") or job.get("candidate_skill_name") or ""),
            "proposed_action": action_type,
            "replay": replay,
            "recommended_publish": replay_ok,
            "cached": False,
        }
        await self._call_storage(self._validation_store.save_evaluation, job_id, evaluation)
        return evaluation

    def _probe_storage_reachable(self) -> bool:
        """Best-effort reachability probe: list one object under the skill root.

        ``FileNotFoundError`` (empty namespace) counts as reachable; only a
        transport/auth error (``RuntimeError`` / connection failure) is treated
        as unreachable.
        """
        try:
            iterator = self._skill_bucket.iter_objects(prefix="")
            for _ in iterator:
                break
            return True
        except Exception as exc:  # noqa: BLE001 - probe never raises to caller.
            if is_not_found_error(exc):
                return True
            logger.debug("[EvolveServer] storage probe failed: %s", exc, exc_info=True)
            return False

    async def storage_status(self) -> dict[str, Any]:
        """Report the object-store connection (backend, endpoint, reachability).

        Powers the dashboard's OpenViking connection indicator. Metadata is read
        from config with no network call; ``reachable`` performs a single cheap
        list against the skill root.
        """
        backend = str(self.config.storage_backend or "").strip().lower() or "unknown"
        endpoint = str(
            getattr(self.config, "viking_endpoint", "") or self.config.storage_endpoint or ""
        )
        root_prefix = str(getattr(self.config, "viking_root_prefix", "") or "")
        group_id = str(getattr(self.config, "viking_group_id", "") or "")
        if backend == "viking":
            namespace = f"viking://resources/{root_prefix}/" + (f"{group_id}/" if group_id else "")
        else:
            namespace = ""
        reachable = await self._call_storage(self._probe_storage_reachable)
        return {
            "backend": backend,
            "endpoint": endpoint,
            "namespace": namespace,
            "api_key_present": bool(getattr(self.config, "viking_api_key", "")),
            "reachable": bool(reachable),
        }

    def _inherit_current_skill(
        self,
        evolved_skill: Optional[dict[str, Any]],
        current_skill: Optional[dict[str, Any]],
        *,
        overwrite_body: bool = False,
    ) -> None:
        if not evolved_skill or not current_skill:
            return
        if overwrite_body:
            evolved_skill["content"] = current_skill.get("content", "")
            evolved_skill["category"] = current_skill.get("category", "general")
        else:
            evolved_skill.setdefault("content", current_skill.get("content", ""))
            evolved_skill.setdefault("category", current_skill.get("category", "general"))
        evolved_skill.setdefault("extra_frontmatter", current_skill.get("extra_frontmatter") or {})
        if isinstance(current_skill.get("bundle"), dict):
            evolved_skill.setdefault("bundle", current_skill["bundle"])
        if isinstance(current_skill.get("_editable_bundle_files"), dict):
            evolved_skill.setdefault(
                "_editable_bundle_files",
                current_skill["_editable_bundle_files"],
            )

    async def _materialize_skill(
        self,
        evolved_skill: Optional[dict],
        action_type: str,
        sessions: list[dict[str, Any]],
        rationale: str,
        source: str,
        *,
        current_skill: Optional[dict[str, Any]] = None,
        evidence_classification: Optional[dict[str, Any]] = None,
        evidence_key: str = "",
        evolution_context: Optional[dict[str, Any]] = None,
        replay_windows: Optional[dict[str, list[dict[str, Any]]]] = None,
    ) -> Optional[dict]:
        if not evolved_skill or not evolved_skill.get("name"):
            return None

        if action_type == DecisionAction.IMPROVE and current_skill and current_skill.get("name"):
            name = current_skill["name"]
        else:
            name = self._sanitise_name(evolved_skill["name"])
        evolved_skill["name"] = name
        candidate_name = name

        session_ids = [session.get("session_id", "") for session in sessions]
        editable_files = (
            evolved_skill.get("_editable_bundle_files")
            if isinstance(evolved_skill.get("_editable_bundle_files"), dict)
            else {}
        )
        raw_changes = evolved_skill.get("file_changes")
        file_changes = raw_changes if isinstance(raw_changes, list) else []
        try:
            base_bundle = (
                candidate_skill_bundle(current_skill)
                if current_skill
                else {}
            )
            evolved_skill, bundle_diff = materialize_bundle_changes(
                evolved_skill,
                current_bundle=base_bundle,
                file_changes=file_changes,
                editable_paths=editable_files,
                extensions=self.config.bundle_text_extensions,
                max_file_bytes=self.config.bundle_max_file_bytes,
                allow_delete=self.config.bundle_allow_delete,
            )
            evolved_skill["bundle_diff"] = bundle_diff
        except BundleChangeError as exc:
            logger.warning(
                "[EvolveServer] rejected bundle changes for '%s': %s",
                name,
                exc,
            )
            return {
                "action": "bundle_change_rejected",
                "proposed_action": action_type,
                "skill_name": name,
                "session_ids": session_ids,
                "rationale": rationale,
                "source": source,
                "uploaded": False,
                "reason": str(exc),
            }
        static_validation = validate_candidate_bundle(
            evolved_skill,
            enabled=self.config.bundle_static_checks_enabled,
        )
        evolved_skill["static_validation"] = static_validation
        if (
            not static_validation.get("passed")
            and self.config.publish_mode != "validated"
        ):
            return {
                "action": "static_validation_rejected",
                "proposed_action": action_type,
                "skill_name": name,
                "session_ids": session_ids,
                "rationale": rationale,
                "source": source,
                "uploaded": False,
                "static_validation": static_validation,
            }

        inherited_replay_contexts: list[dict[str, Any]] = []
        effective_replay_windows = {
            "recent": list((replay_windows or {}).get("recent") or []),
            "historical": list((replay_windows or {}).get("historical") or []),
        }
        if action_type == DecisionAction.MERGE:
            inherited_names = list(
                dict.fromkeys(
                    value
                    for value in (name, candidate_name)
                    if value and value != "candidate_evidence"
                )
            )
            for inherited_name in inherited_names:
                context = await self._call_storage(
                    self._load_published_replay_context,
                    inherited_name,
                )
                if context:
                    inherited_replay_contexts.append(context)
            inherited_cases = [
                {
                    **case,
                    "evidence_window": "historical",
                    "inherited_from_skill": context.get("skill_name"),
                    "inherited_from_version": context.get("version"),
                }
                for context in inherited_replay_contexts
                for case in context.get("replay_cases") or []
                if isinstance(case, dict)
            ]
            effective_replay_windows["historical"] = [
                *inherited_cases,
                *effective_replay_windows["historical"],
            ]
            evolution_context = {
                **(evolution_context or {}),
                "merge_context": {
                    **(
                        (evolution_context or {}).get("merge_context")
                        if isinstance(
                            (evolution_context or {}).get("merge_context"),
                            dict,
                        )
                        else {}
                    ),
                    "inherited_replays": [
                        {
                            "skill_name": context.get("skill_name"),
                            "version": context.get("version"),
                            "job_id": context.get("job_id"),
                            "replay_case_count": len(
                                context.get("replay_cases") or []
                            ),
                        }
                        for context in inherited_replay_contexts
                    ],
                },
            }

        test_datasets = await self._synthesize_candidate_datasets(
            skill_name=name,
            sessions=sessions,
            candidate_skill=evolved_skill,
            evidence_classification=evidence_classification,
            evolution_context=evolution_context,
            replay_windows=effective_replay_windows,
        )
        skill_id = self._id_registry.get_or_create(name)
        evolved_skill["skill_id"] = skill_id
        if self.config.publish_mode == "validated":
            record = self._queue_validation_job(
                evolved_skill,
                action_type,
                sessions,
                rationale,
                source,
                current_skill=current_skill,
                evidence_classification=evidence_classification,
                evidence_key=evidence_key or name,
                evolution_context=evolution_context,
                replay_windows=effective_replay_windows,
                test_datasets=test_datasets,
            )
            job_id = str(record.get("validation_job_id") or "")
            await self._record_evolution_candidate(
                evidence_key or name,
                sessions,
                job_id,
            )
            # "以后就用真实回放": auto-kick a true replay for every freshly queued
            # candidate so the dashboard's evaluation is populated without a human
            # having to press "评估". It runs real tool loops (minutes), so fire it
            # as a background task rather than blocking the cycle.
            if job_id:
                self._schedule_candidate_evaluation(job_id)
            return record

        actual_action, uploaded = await self._resolve_and_upload(evolved_skill, action_type)
        if uploaded:
            await self._mark_evidence_published(evidence_key or name)
        logger.info(
            "[EvolveServer] %s skill '%s' (id=%s, v%d)",
            actual_action,
            name,
            skill_id,
            self._id_registry.get_version(name),
        )
        return {
            "action": actual_action,
            "skill_name": name,
            "skill_id": skill_id,
            "version": self._id_registry.get_version(name),
            "session_ids": session_ids,
            "rationale": rationale,
            "evidence_classification": evidence_classification or {},
            "source": source,
            "edit_summary": evolved_skill.get("edit_summary"),
            "file_changes": evolved_skill.get("file_changes") or [],
            "uploaded": uploaded,
        }

    async def _evolve_skill_group(
        self,
        skill_name: str,
        sessions: list[dict],
        existing_skill_names: list[str],
    ) -> Optional[dict]:
        planning_sessions, evolution_context, replay_windows = (
            await self._prepare_evolution_evidence(skill_name, sessions)
        )
        current_bundle = await self._call_storage(
            self._fetch_skill_bundle,
            skill_name,
        )
        current_md = (
            bundle_entrypoint_text(current_bundle)
            if current_bundle
            else None
        )
        current_skill = parse_skill_content(skill_name, current_md) if current_md else None
        current_skill = self._overlay_manifest_metadata(
            current_skill,
            await self._call_storage(self._load_remote_skill_record, skill_name),
        )
        if current_skill and current_bundle:
            current_skill = attach_bundle_payload(current_skill, current_bundle)
        working_skill = await self._working_skill_for_evolution(
            skill_name,
            current_skill,
        )
        working_skill = self._prepare_skill_bundle_for_planner(
            working_skill,
            planning_sessions,
        )

        result = await evolve_skill_from_sessions(
            self._llm,
            skill_name,
            planning_sessions,
            working_skill,
            existing_skill_names,
            evolution_context=evolution_context,
        )
        if not result or result.get("action") == DecisionAction.SKIP:
            rationale = (
                str(result.get("rationale") or "")
                if isinstance(result, dict)
                else "planner returned no actionable decision"
            )
            evidence_state = await self._record_evolution_skip(
                skill_name,
                sessions,
                rationale,
            )
            logger.info("[EvolveServer] skill '%s': LLM decided to skip", skill_name)
            debt = (
                evidence_state.get("change_debt")
                if isinstance(evidence_state.get("change_debt"), dict)
                else {}
            )
            return {
                "action": "skip",
                "skill_name": skill_name,
                "session_ids": [
                    str(session.get("session_id") or "")
                    for session in sessions
                    if str(session.get("session_id") or "").strip()
                ],
                "rationale": rationale,
                "evidence_classification": (
                    result.get("evidence_classification")
                    if isinstance(result, dict)
                    else {}
                ),
                "source": "skill_group",
                "uploaded": False,
                "change_debt": debt,
            }

        action_type = result.get("action", DecisionAction.IMPROVE)
        evolved_skill = result.get("skill")
        if not evolved_skill:
            return None

        if action_type == DecisionAction.OPTIMIZE_DESC and working_skill:
            self._inherit_current_skill(evolved_skill, working_skill, overwrite_body=True)
        elif working_skill:
            self._inherit_current_skill(evolved_skill, working_skill)

        return await self._materialize_skill(
            evolved_skill,
            action_type,
            planning_sessions,
            result.get("rationale", ""),
            "skill_group",
            current_skill=current_skill,
            evidence_classification=result.get("evidence_classification"),
            evidence_key=skill_name,
            evolution_context=evolution_context,
            replay_windows=replay_windows,
        )

    async def _working_skill_for_evolution(
        self,
        skill_name: str,
        published_skill: Optional[dict[str, Any]],
    ) -> Optional[dict[str, Any]]:
        """Continue editing the latest open candidate while retaining the published A/B baseline."""
        if not self.config.candidate_coalesce_enabled:
            return published_skill
        open_jobs = await self._call_storage(
            self._validation_store.list_open_jobs_for_skill,
            skill_name,
        )
        if not open_jobs:
            return published_skill
        latest = max(
            open_jobs,
            key=lambda item: str(
                item.get("updated_at") or item.get("created_at") or ""
            ),
        )
        candidate = (
            latest.get("candidate_skill")
            if isinstance(latest.get("candidate_skill"), dict)
            else None
        )
        if not candidate or str(candidate.get("name") or "") != skill_name:
            return published_skill
        working = dict(candidate)
        if published_skill:
            for key in ("_version", "_sha256", "_size"):
                if key in published_skill:
                    working.setdefault(key, published_skill[key])
        working["_candidate_job_id"] = str(latest.get("job_id") or "")
        working["_candidate_revision"] = int(
            latest.get("candidate_revision") or 1
        )
        return working

    async def _handle_no_skill_sessions(
        self,
        sessions: list[dict],
        existing_skill_names: list[str],
    ) -> list[dict]:
        planning_sessions, evolution_context, replay_windows = (
            await self._prepare_evolution_evidence(NO_SKILL_KEY, sessions)
        )
        result = await create_skill_from_sessions(
            self._llm,
            planning_sessions,
            existing_skill_names,
            evolution_context=evolution_context,
        )
        if not result or result.get("action") == DecisionAction.SKIP:
            rationale = (
                str(result.get("rationale") or "")
                if isinstance(result, dict)
                else "planner returned no actionable decision"
            )
            evidence_state = await self._record_evolution_skip(
                NO_SKILL_KEY,
                sessions,
                rationale,
            )
            logger.info("[EvolveServer] no-skill sessions: LLM decided to skip")
            debt = (
                evidence_state.get("change_debt")
                if isinstance(evidence_state.get("change_debt"), dict)
                else {}
            )
            return [
                {
                    "action": "skip",
                    "skill_name": "",
                    "session_ids": [
                        str(session.get("session_id") or "")
                        for session in sessions
                        if str(session.get("session_id") or "").strip()
                    ],
                    "rationale": rationale,
                    "evidence_classification": (
                        result.get("evidence_classification")
                        if isinstance(result, dict)
                        else {}
                    ),
                    "source": "no_skill",
                    "uploaded": False,
                    "change_debt": debt,
                }
            ]

        evolved_skill = result.get("skill")
        if not evolved_skill:
            return []

        record = await self._materialize_skill(
            evolved_skill,
            DecisionAction.CREATE,
            planning_sessions,
            result.get("rationale", ""),
            "no_skill",
            evidence_classification=result.get("evidence_classification"),
            evidence_key=NO_SKILL_KEY,
            evolution_context=evolution_context,
            replay_windows=replay_windows,
        )
        return [record] if record else []

    # ------------------------------------------------------------------ #
    # Session ingest (push model).                                        #
    #                                                                     #
    # A remote machine can enqueue a session for evolution WITHOUT any    #
    # OpenViking credentials or knowledge of the queue path by POSTing    #
    # the session JSON to ``/ingest_session``. The server owns the        #
    # storage identity and writes the payload into the team-shared queue, #
    # then (optionally) triggers a cycle. This decouples "who produced    #
    # the session" from evolution.                                       #
    # ------------------------------------------------------------------ #

    @staticmethod
    def _normalize_ingest_session(payload: dict[str, Any]) -> dict[str, Any]:
        """Validate + normalize an incoming session payload into queue schema.

        Mirrors the producer contract in
        ``skillgene.proxy.ProxyServer._upload_session_data``: the queue object is
        ``{session_id, timestamp, user_alias, num_turns, turns}`` where each turn
        carries at least ``prompt_text``/``response_text`` and optional
        ``read_skills``/``modified_skills``/``tool_calls``. Missing optional
        fields are defaulted so the downstream summarizer never KeyErrors.

        ``user_alias`` (the sender's username) is now **required** so the
        dashboard can always attribute a session to who submitted it. Callers
        may pass it as ``user_alias``, ``user``, or ``username``.
        """
        if not isinstance(payload, dict):
            raise ValueError("session payload must be a JSON object")

        session_id = str(payload.get("session_id") or "").strip()
        if not session_id:
            raise ValueError("session payload requires a non-empty 'session_id'")

        user_alias = str(
            payload.get("user_alias")
            or payload.get("user")
            or payload.get("username")
            or ""
        ).strip()
        if not user_alias:
            raise ValueError(
                "session payload requires a non-empty 'user_alias' (the sender's username)"
            )

        raw_turns = payload.get("turns")
        if not isinstance(raw_turns, list) or not raw_turns:
            raise ValueError("session payload requires a non-empty 'turns' list")

        norm_turns: list[dict[str, Any]] = []
        for idx, turn in enumerate(raw_turns, start=1):
            if not isinstance(turn, dict):
                raise ValueError(f"turn #{idx} must be a JSON object")
            prompt_text = str(turn.get("prompt_text") or "").strip()
            response_text = str(turn.get("response_text") or "").strip()
            if not prompt_text and not response_text:
                raise ValueError(
                    f"turn #{idx} needs at least 'prompt_text' or 'response_text'"
                )

            def _skill_list(value: Any) -> list[dict[str, str]]:
                out: list[dict[str, str]] = []
                for item in value or []:
                    if isinstance(item, dict) and item.get("skill_name"):
                        out.append({"skill_name": str(item["skill_name"]).strip()})
                    elif isinstance(item, str) and item.strip():
                        out.append({"skill_name": item.strip()})
                return out

            norm_turns.append(
                {
                    "turn_num": int(turn.get("turn_num", idx) or idx),
                    "prompt_text": prompt_text,
                    "response_text": response_text,
                    "messages": turn.get("messages") or [],
                    "tool_calls": turn.get("tool_calls") or [],
                    "read_skills": _skill_list(turn.get("read_skills")),
                    "used_skills": [
                        str(item or "").strip()
                        for item in (turn.get("used_skills") or [])
                        if str(item or "").strip()
                    ],
                    "injected_skills": [
                        (
                            str(item.get("skill_name") or "").strip()
                            if isinstance(item, dict)
                            else str(item or "").strip()
                        )
                        for item in (turn.get("injected_skills") or [])
                        if (
                            str(item.get("skill_name") or "").strip()
                            if isinstance(item, dict)
                            else str(item or "").strip()
                        )
                    ],
                    "modified_skills": _skill_list(turn.get("modified_skills")),
                    "tool_results": turn.get("tool_results") or [],
                    "tool_observations": turn.get("tool_observations") or [],
                    "tool_errors": turn.get("tool_errors") or [],
                    "metrics": turn.get("metrics") or {},
                    "prm_score": turn.get("prm_score"),
                    "context_usage": (
                        dict(turn.get("context_usage"))
                        if isinstance(turn.get("context_usage"), dict)
                        else {}
                    ),
                }
            )

        timestamp = str(payload.get("timestamp") or "").strip() or (
            datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
        )
        normalized = {
            "schema_version": str(payload.get("schema_version") or ""),
            "protocol_version": str(payload.get("protocol_version") or ""),
            "session_id": session_id,
            "timestamp": timestamp,
            "user_alias": user_alias,
            "num_turns": len(norm_turns),
            "turns": norm_turns,
            "messages": payload.get("messages") or [],
            "system_prompt": str(payload.get("system_prompt") or ""),
            "injected_skills": [
                str(item or "").strip()
                for item in (payload.get("injected_skills") or [])
                if str(item or "").strip()
            ],
            "used_skills": [
                str(item or "").strip()
                for item in (payload.get("used_skills") or [])
                if str(item or "").strip()
            ],
            "metrics": payload.get("metrics") or {},
            "source": str(payload.get("source") or ""),
            "model": str(payload.get("model") or ""),
            "runtime": (
                dict(payload.get("runtime"))
                if isinstance(payload.get("runtime"), dict)
                else {}
            ),
            "runtime_context": (
                dict(payload.get("runtime_context"))
                if isinstance(payload.get("runtime_context"), dict)
                else {}
            ),
            "source_materials": [
                dict(item)
                for item in payload.get("source_materials") or []
                if isinstance(item, dict) and item.get("path")
            ],
        }
        # Preserve a caller-supplied conversation title so the ledger / 会话历史
        # can show it verbatim instead of falling back to the first prompt.
        title = str(payload.get("title") or "").strip()
        if title:
            normalized["title"] = title[:120]
        return normalized

    def _write_ingest_session(self, session: dict[str, Any]) -> str:
        """Write a normalized session into the team-shared session queue."""
        object_key = f"{self._session_prefix}sessions/{session['session_id']}.json"
        content = json.dumps(session, ensure_ascii=False).encode("utf-8")
        self._bucket.put_object(object_key, content)
        logger.info(
            "[EvolveServer] ingested session %s (%d turns) -> %s",
            session["session_id"],
            session["num_turns"],
            object_key,
        )
        return object_key

    async def ingest_session(
        self,
        payload: dict[str, Any],
        *,
        trigger: bool = False,
    ) -> dict[str, Any]:
        """Enqueue a pushed session; optionally run an evolution cycle now.

        When ``trigger`` is false (default) the session only lands in the queue
        and is picked up by the periodic loop / a later ``/trigger`` — the call
        returns immediately. When true, a cycle runs under the shared lock and
        its summary is returned.
        """
        session = self._normalize_ingest_session(payload)
        object_key = await self._call_storage(self._write_ingest_session, session)
        # Record the conversation in the durable ledger (title + sender +
        # consumption state) so it survives being drained out of the queue.
        await self._call_storage(self._upsert_session_ledger, session, "queued")
        result: dict[str, Any] = {
            "status": "ingested",
            "session_id": session["session_id"],
            "num_turns": session["num_turns"],
            "object_key": object_key,
            "triggered": bool(trigger),
        }
        if trigger:
            async with self._get_run_lock():
                result["cycle"] = await self.run_once()
        return result

    def _read_queued_sessions(self) -> list[dict[str, Any]]:
        """Read every queued session and return sender-attributed metadata.

        Returns lightweight rows ``{session_id, user_alias, timestamp,
        num_turns}`` — no turn bodies — so the dashboard can show who submitted
        each session still waiting to be evolved.
        """
        keys = list_session_keys(self._bucket, self._session_prefix)
        rows: list[dict[str, Any]] = []
        for key in keys:
            session = read_json_object(self._bucket, key)
            if not isinstance(session, dict):
                continue
            rows.append(
                {
                    "session_id": str(session.get("session_id") or "").strip()
                    or key.rsplit("/", 1)[-1].removesuffix(".json"),
                    "user_alias": str(session.get("user_alias") or "").strip() or "unknown",
                    "timestamp": str(session.get("timestamp") or ""),
                    "num_turns": int(session.get("num_turns") or len(session.get("turns") or [])),
                }
            )
        rows.sort(key=lambda r: r.get("timestamp") or "", reverse=True)
        return rows

    async def list_queued_sessions(self) -> list[dict[str, Any]]:
        """Async wrapper around :meth:`_read_queued_sessions`."""
        return await self._call_storage(self._read_queued_sessions)

    # ------------------------------------------------------------------ #
    # Session consumption ledger.                                         #
    #                                                                     #
    # The live queue only holds sessions *waiting* to be evolved; once a  #
    # cycle drains them they vanish. The ledger is a durable record of    #
    # every conversation the server has seen — its title, who sent it,    #
    # and whether it has been consumed yet — so the dashboard can show a  #
    # persistent "会话历史" with consumption status instead of losing the  #
    # session the moment it is processed.                                 #
    # ------------------------------------------------------------------ #

    @staticmethod
    def _session_title(session: dict[str, Any]) -> str:
        """Derive a short human title from a session's first prompt."""
        explicit = str(session.get("title") or "").strip()
        if explicit:
            return explicit[:120]
        for turn in session.get("turns") or []:
            if not isinstance(turn, dict):
                continue
            prompt = str(turn.get("prompt_text") or "").strip()
            if prompt:
                title = " ".join(prompt.split())
                return title[:120]
        return "(无标题会话)"

    def _ledger_key(self, session_id: str) -> str:
        return f"{self._session_prefix}session_ledger/{session_id}.json"

    def _upsert_session_ledger(self, session: dict[str, Any], status: str) -> None:
        """Create or update the ledger entry for a session.

        ``status="queued"`` on ingest, ``status="consumed"`` after a cycle
        drains it. Upsert semantics mean sessions pushed straight to the queue
        (e.g. by a trusted batch importer) still get a ledger entry backfilled
        from the session object when they are consumed.
        """
        session_id = str(session.get("session_id") or "").strip()
        if not session_id:
            return
        existing = read_json_object(self._bucket, self._ledger_key(session_id)) or {}
        now = datetime.now(timezone.utc).isoformat()
        entry = {
            "session_id": session_id,
            "title": existing.get("title") or self._session_title(session),
            "user_alias": str(session.get("user_alias") or existing.get("user_alias") or "").strip()
            or "unknown",
            "timestamp": str(session.get("timestamp") or existing.get("timestamp") or ""),
            "num_turns": int(session.get("num_turns") or len(session.get("turns") or [])
                             or existing.get("num_turns") or 0),
            "status": status,
            "ingested_at": existing.get("ingested_at") or now,
            "consumed_at": existing.get("consumed_at") or "",
        }
        if status == "consumed" and not entry["consumed_at"]:
            entry["consumed_at"] = now
        self._bucket.put_object(
            self._ledger_key(session_id),
            json.dumps(entry, ensure_ascii=False).encode("utf-8"),
        )

    def _mark_sessions_consumed(self, sessions: list[dict[str, Any]]) -> None:
        """Flip ledger entries for a drained batch to ``consumed`` (upserting)."""
        for session in sessions:
            if isinstance(session, dict):
                self._upsert_session_ledger(session, status="consumed")

    def _read_session_ledger(self, limit: int = 100) -> list[dict[str, Any]]:
        """Return ledger rows (newest first): title + sender + consumption state."""
        from ..store.object_store import list_object_keys

        prefix = f"{self._session_prefix}session_ledger/"
        rows: list[dict[str, Any]] = []
        for key in list_object_keys(self._bucket, prefix):
            if not key.endswith(".json"):
                continue
            entry = read_json_object(self._bucket, key)
            if isinstance(entry, dict) and entry.get("session_id"):
                rows.append(entry)
        rows.sort(
            key=lambda r: (str(r.get("ingested_at") or ""), str(r.get("timestamp") or "")),
            reverse=True,
        )
        return rows[: max(1, int(limit or 100))]

    async def list_session_ledger(self, limit: int = 100) -> list[dict[str, Any]]:
        """Async wrapper around :meth:`_read_session_ledger`."""
        return await self._call_storage(self._read_session_ledger, int(limit or 100))

    def _archive_key(self, session_id: str) -> str:
        return f"{self._session_prefix}session_archive/{session_id}.json"

    def _archive_sessions(self, sessions: list[dict[str, Any]]) -> None:
        """Persist a durable copy of each session's turns before the live queue
        object is deleted on consumption.

        The queue object ``sessions/{id}.json`` is removed once a cycle drains
        it, which would otherwise make the conversation content unrecoverable.
        Runtime identity, sandbox snapshot references, and embedded source
        materials are retained so later dataset synthesis and True Replay can
        reconstruct uploaded inputs. Archiving is best-effort: a failure here
        must never block the drain.
        """
        for session in sessions:
            if not isinstance(session, dict):
                continue
            sid = str(session.get("session_id") or "").strip()
            if not sid:
                continue
            turns: list[dict[str, Any]] = []
            for turn in session.get("turns") or []:
                if not isinstance(turn, dict):
                    continue
                turns.append({
                    "turn_num": turn.get("turn_num"),
                    "prompt_text": turn.get("prompt_text") or "",
                    "response_text": turn.get("response_text") or "",
                    "messages": turn.get("messages") or [],
                    "tool_calls": turn.get("tool_calls") or [],
                    "tool_results": turn.get("tool_results") or [],
                    "tool_observations": turn.get("tool_observations") or [],
                    "tool_errors": turn.get("tool_errors") or [],
                    "read_skills": turn.get("read_skills") or [],
                    "used_skills": turn.get("used_skills") or [],
                    "injected_skills": turn.get("injected_skills") or [],
                    "modified_skills": turn.get("modified_skills") or [],
                    "metrics": turn.get("metrics") or {},
                    "context_usage": (
                        dict(turn.get("context_usage"))
                        if isinstance(turn.get("context_usage"), dict)
                        else {}
                    ),
                })
            archived = {
                "schema_version": session.get("schema_version") or "",
                "protocol_version": session.get("protocol_version") or "",
                "session_id": sid,
                "timestamp": session.get("timestamp") or "",
                "user_alias": session.get("user_alias") or "",
                "num_turns": session.get("num_turns") or len(turns),
                "title": session.get("title") or "",
                "turns": turns,
                "messages": session.get("messages") or [],
                "system_prompt": session.get("system_prompt") or "",
                "injected_skills": session.get("injected_skills") or [],
                "used_skills": session.get("used_skills") or [],
                "metrics": session.get("metrics") or {},
                "source": session.get("source") or "",
                "model": session.get("model") or "",
                "runtime": (
                    dict(session.get("runtime"))
                    if isinstance(session.get("runtime"), dict)
                    else {}
                ),
                "runtime_context": (
                    dict(session.get("runtime_context"))
                    if isinstance(session.get("runtime_context"), dict)
                    else {}
                ),
                "source_materials": [
                    dict(item)
                    for item in session.get("source_materials") or []
                    if isinstance(item, dict) and item.get("path")
                ],
            }
            try:
                self._bucket.put_object(
                    self._archive_key(sid),
                    json.dumps(archived, ensure_ascii=False).encode("utf-8"),
                )
            except Exception as exc:  # noqa: BLE001 - archival must not block drain
                logger.warning("[EvolveServer] archive session %s failed: %s", sid, exc)

    def _read_session_content(self, session_id: str) -> tuple[dict[str, Any] | None, str]:
        """Return ``(session_dict, source)`` with turns, live queue first then
        archive. ``source`` is ``"queue"``/``"archive"``/``""`` (not found)."""
        live_key = f"{self._session_prefix}sessions/{session_id}.json"
        for key, source in ((live_key, "queue"), (self._archive_key(session_id), "archive")):
            obj = read_json_object(self._bucket, key)
            if isinstance(obj, dict) and obj.get("turns") is not None:
                return obj, source
        return None, ""

    def _get_session_detail(self, session_id: str) -> dict[str, Any]:
        """Ledger metadata + conversation turns for one session.

        Turns come from the live queue if still pending, else the post-consume
        archive. Sessions consumed before archival shipped have no turns — we
        say so via ``turns_available=false`` rather than failing.
        """
        sid = str(session_id or "").strip()
        if not sid:
            return {"status": "not_found"}
        ledger = read_json_object(self._bucket, self._ledger_key(sid)) or {}
        content, source = self._read_session_content(sid)
        if not ledger and content is None:
            return {"status": "not_found", "session_id": sid}
        turns: list[dict[str, Any]] = []
        for turn in (content or {}).get("turns") or []:
            if isinstance(turn, dict):
                turns.append({
                    "turn_num": turn.get("turn_num"),
                    "prompt_text": str(turn.get("prompt_text") or ""),
                    "response_text": str(turn.get("response_text") or ""),
                    "messages": turn.get("messages") or [],
                    "tool_calls": turn.get("tool_calls") or [],
                    "tool_results": turn.get("tool_results") or [],
                    "tool_observations": turn.get("tool_observations") or [],
                    "tool_errors": turn.get("tool_errors") or [],
                    "read_skills": turn.get("read_skills") or [],
                    "used_skills": turn.get("used_skills") or [],
                    "injected_skills": turn.get("injected_skills") or [],
                    "modified_skills": turn.get("modified_skills") or [],
                    "metrics": turn.get("metrics") or {},
                })
        meta = {
            "session_id": sid,
            "title": ledger.get("title") or (content or {}).get("title") or "",
            "user_alias": ledger.get("user_alias") or (content or {}).get("user_alias") or "",
            "status": ledger.get("status") or "",
            "num_turns": ledger.get("num_turns") or (content or {}).get("num_turns") or len(turns),
            "ingested_at": ledger.get("ingested_at") or "",
            "consumed_at": ledger.get("consumed_at") or "",
            "timestamp": ledger.get("timestamp") or (content or {}).get("timestamp") or "",
        }
        return {
            "status": "ok",
            "session_id": sid,
            "meta": meta,
            "turns": turns,
            "messages": (content or {}).get("messages") or [],
            "system_prompt": (content or {}).get("system_prompt") or "",
            "injected_skills": (content or {}).get("injected_skills") or [],
            "used_skills": (content or {}).get("used_skills") or [],
            "metrics": (content or {}).get("metrics") or {},
            "source": (content or {}).get("source") or "",
            "model": (content or {}).get("model") or "",
            "turns_available": bool(turns),
            "turns_source": source,
        }

    async def get_session_detail(self, session_id: str) -> dict[str, Any]:
        """Async wrapper around :meth:`_get_session_detail`."""
        return await self._call_storage(self._get_session_detail, session_id)

    def _get_session_process(self, session_id: str) -> dict[str, Any]:
        """Reconstruct "what happened to this session" from evolution history.

        A session isn't processed in isolation — it's aggregated into per-skill
        groups. So we scan ``evolve_history.jsonl`` for every cycle that
        referenced this ``session_id`` (top-level or via any evolution record)
        and surface, per cycle: this session's judge scores, which skills its
        content contributed to evolving, and the resulting action/candidate.
        Read-only; a missing/corrupt history file yields an empty timeline.
        """
        sid = str(session_id or "").strip()
        if not sid:
            return {"status": "not_found"}
        cycles: list[dict[str, Any]] = []
        try:
            with open(self.config.history_path, "r", encoding="utf-8") as handle:
                for line in handle:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        record = json.loads(line)
                    except Exception:
                        continue
                    if not isinstance(record, dict):
                        continue
                    ids = set(record.get("session_ids") or [])
                    for evo in record.get("evolutions") or []:
                        if isinstance(evo, dict):
                            ids.update(evo.get("session_ids") or [])
                    if sid not in ids:
                        continue
                    judge = None
                    for detail in record.get("session_judge_details") or []:
                        if isinstance(detail, dict) and str(detail.get("session_id")) == sid:
                            judge = detail
                            break
                    evolutions: list[dict[str, Any]] = []
                    for evo in record.get("evolutions") or []:
                        if isinstance(evo, dict) and sid in set(evo.get("session_ids") or []):
                            evolutions.append({
                                "skill_name": evo.get("skill_name"),
                                "action": evo.get("action"),
                                "reason": evo.get("reason"),
                                "rationale": evo.get("rationale"),
                                "evidence_classification": evo.get("evidence_classification") or {},
                                "uploaded": evo.get("uploaded"),
                                "version": evo.get("version"),
                                "job_id": evo.get("job_id"),
                            })
                    cycles.append({
                        "timestamp": record.get("timestamp"),
                        "elapsed_seconds": record.get("elapsed_seconds"),
                        "sessions": record.get("sessions"),
                        "skill_groups": record.get("skill_groups"),
                        "uploaded_skills": record.get("uploaded_skills"),
                        "candidates_queued": record.get("candidates_queued"),
                        "judge": judge,
                        "evolutions": evolutions,
                    })
        except FileNotFoundError:
            cycles = []
        except Exception as exc:  # noqa: BLE001 - process view is best-effort
            logger.warning("[EvolveServer] session process read failed: %s", exc)
            cycles = []
        cycles.reverse()
        return {"status": "ok", "session_id": sid, "cycles": cycles}

    async def get_session_process(self, session_id: str) -> dict[str, Any]:
        """Async wrapper around :meth:`_get_session_process`."""
        return await self._call_storage(self._get_session_process, session_id)

    async def run_once(self) -> dict:
        cycle_id = f"eb-{uuid.uuid4().hex[:12]}"
        with langfuse_observation(
            name="teamEvolver.evolve.cycle",
            as_type="agent",
            input={
                "cycle_id": cycle_id,
                "strategy": self.config.evolve_strategy,
                "publish_mode": self.config.publish_mode,
            },
            metadata={
                "component": "teamEvolver.evolve",
                "operation": "cycle",
                "cycle_id": cycle_id,
            },
            trace_name="teamEvolver.evolve.cycle",
            session_id=cycle_id,
            tags=["evolve", "cycle"],
        ) as observation:
            result = await self._run_once()
            update_langfuse_observation(
                observation,
                output={
                    key: result.get(key)
                    for key in (
                        "sessions",
                        "skill_groups",
                        "actions",
                        "skills_evolved",
                        "candidates_queued",
                        "had_processing_error",
                    )
                },
                metadata={
                    "source_session_ids": result.get("session_ids") or [],
                },
            )
            return result

    async def _run_once(self) -> dict:
        logger.info("[EvolveServer] === starting evolution cycle ===")
        started_at = time.monotonic()

        sessions, session_keys = await self._drain_sessions()
        judge_summary = self._empty_judge_summary()
        skill_group_count = 0
        no_skill_sessions: list[dict] = []
        evolution_records: list[dict] = []
        had_processing_error = False

        if sessions:
            logger.info("[EvolveServer] summarizing %d sessions", len(sessions))
            await summarize_sessions_parallel(self._llm, sessions)
            judge_summary = await self._run_session_judge(sessions)

            grouped_sessions = aggregate_sessions_by_skill(sessions)
            no_skill_sessions = grouped_sessions.pop(NO_SKILL_KEY, [])
            skill_group_count = len(grouped_sessions)

            manifest = await self._call_storage(self._load_remote_skills)
            existing_skill_names = [item.get("name", "") for item in manifest.values()]

            if grouped_sessions:
                logger.info("[EvolveServer] evolving %d skill group(s)", skill_group_count)
            for skill_name, skill_sessions in grouped_sessions.items():
                try:
                    record = await self._evolve_skill_group(skill_name, skill_sessions, existing_skill_names)
                except Exception as exc:
                    logger.error("[EvolveServer] skill '%s' evolve failed: %s", skill_name, exc)
                    had_processing_error = True
                    continue
                if record:
                    evolution_records.append(record)

            if no_skill_sessions:
                logger.info("[EvolveServer] processing %d no-skill sessions", len(no_skill_sessions))
                try:
                    evolution_records.extend(
                        await self._handle_no_skill_sessions(no_skill_sessions, existing_skill_names)
                    )
                except Exception as exc:
                    logger.error("[EvolveServer] no-skill evolve failed: %s", exc)
                    had_processing_error = True
        else:
            logger.info("[EvolveServer] queue empty - checking pending validation publish jobs")

        published_records, validation_publish_summary = await self._finalize_validation_jobs()
        all_records = evolution_records + published_records

        # Adopt any externally-seeded registry entries (e.g. StaffDeck pushing
        # local skills as our initial library) before saving, so this cycle's
        # write-back preserves them instead of clobbering with our in-memory map.
        await self._call_storage(self._id_registry.merge_from_oss, self._skill_bucket, self._skill_prefix)
        await self._call_storage(self._id_registry.save_to_oss, self._skill_bucket, self._skill_prefix)
        if session_keys and not had_processing_error:
            # Snapshot conversation content to the durable archive BEFORE the
            # queue objects are deleted, so 会话历史 can still show the turns of
            # a consumed session (the ledger alone only keeps metadata).
            await self._call_storage(self._archive_sessions, sessions)
            await self._call_storage(delete_session_keys, self._bucket, session_keys)
            # Mark the drained conversations consumed in the durable ledger so
            # the dashboard's 会话历史 reflects that they've been processed even
            # though they're gone from the live queue.
            await self._call_storage(self._mark_sessions_consumed, sessions)
        elif session_keys and had_processing_error:
            logger.warning(
                "[EvolveServer] retaining %d session(s) in queue because this cycle had processing errors",
                len(session_keys),
            )

        elapsed = round(time.monotonic() - started_at, 1)
        uploaded_skills = sum(1 for record in all_records if record.get("uploaded"))
        queued_candidates = sum(
            1
            for record in all_records
            if record.get("action")
            in {"queued_for_validation", "updated_validation_candidate"}
        )
        published_after_validation = sum(
            1 for record in all_records if record.get("action") == "published_after_validation"
        )
        human_review_summary = await self._call_storage(self._collect_human_review_summary)
        summary = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "elapsed_seconds": elapsed,
            "sessions": len(sessions),
            "session_ids": [
                str(session.get("session_id") or "")
                for session in sessions
                if str(session.get("session_id") or "").strip()
            ],
            # Map each consumed session to the username that submitted it, so the
            # dashboard can still attribute a session to its sender after it has
            # been drained out of the live queue.
            "session_senders": {
                str(session.get("session_id") or ""): str(session.get("user_alias") or "")
                for session in sessions
                if str(session.get("session_id") or "").strip()
            },
            "skill_groups": skill_group_count,
            "no_skill_sessions": len(no_skill_sessions),
            "actions": len(all_records),
            "skills_evolved": uploaded_skills,
            "uploaded_skills": uploaded_skills,
            "candidates_queued": queued_candidates,
            "published_after_validation": published_after_validation,
            "evolutions": all_records,
            "session_judge": judge_summary,
            "session_judge_details": self._collect_session_judge_details(sessions),
            "validation_publish": validation_publish_summary,
            "human_review": human_review_summary,
            "had_processing_error": had_processing_error,
        }
        self._append_history(summary)
        logger.info(
            "[EvolveServer] === cycle done: %d sessions, %d skill groups, %d uploaded, %d queued in %.1fs ===",
            len(sessions),
            skill_group_count,
            uploaded_skills,
            queued_candidates,
            elapsed,
        )
        open_reviews = int(human_review_summary.get("open_tasks", 0) or 0)
        if open_reviews > 0:
            logger.warning(
                "[EvolveServer] %d skill candidate(s) awaiting HUMAN REVIEW "
                "(inconclusive validation). Resolve via dashboard review: %s",
                open_reviews,
                ", ".join(
                    f"{t.get('skill_name', '?')}({t.get('job_id', '')})"
                    for t in human_review_summary.get("tasks", [])
                ),
            )
        return summary

    async def run_periodic(self) -> None:
        self._running = True
        logger.info("[EvolveServer] periodic mode: interval=%ds", self.config.interval_seconds)
        while self._running:
            try:
                # Share the cycle lock with /trigger so a manual trigger and the
                # periodic loop never run overlapping read-modify-write cycles.
                async with self._get_run_lock():
                    await self.run_once()
            except Exception as exc:
                logger.error("[EvolveServer] cycle error: %s", exc, exc_info=True)
            await asyncio.sleep(self.config.interval_seconds)

    def stop(self) -> None:
        self._running = False

    def _get_run_lock(self) -> asyncio.Lock:
        """Lazily create the per-loop cycle lock (see ``__init__``)."""
        if self._run_lock is None:
            self._run_lock = asyncio.Lock()
        return self._run_lock

    def create_http_app(self):
        from dataclasses import replace
        from pathlib import Path

        from fastapi import Body, FastAPI, Header
        from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
        from fastapi.staticfiles import StaticFiles

        app = FastAPI(title="teamEvolver Evolve Server")

        _web_dir = Path(__file__).resolve().parent.parent / "web"
        _dist_dir = _web_dir / "dist"
        _dist_index = _dist_dir / "index.html"
        _dist_assets = _dist_dir / "assets"
        _console_path = _web_dir / "console.html"
        _dashboard_path = _web_dir / "dashboard.html"
        # Hashed build assets (JS/CSS/fonts) live under dist/assets. Mount them
        # first so they take precedence over any catch-all routes below.
        if _dist_assets.is_dir():
            app.mount(
                "/assets", StaticFiles(directory=str(_dist_assets)), name="assets"
            )

        @app.get("/", response_class=HTMLResponse)
        @app.get("/console", response_class=HTMLResponse)
        async def console():
            """Serve the unified React console (evolution + skill admin).

            Prefers the built SPA (``web/dist/index.html``); falls back to the
            legacy single-file ``console.html`` when the build is absent.
            """
            if _dist_index.is_file():
                return FileResponse(_dist_index)
            try:
                return HTMLResponse(content=_console_path.read_text(encoding="utf-8"))
            except FileNotFoundError:
                return HTMLResponse(content="<h1>console not built</h1>", status_code=404)

        @app.get("/dashboard", response_class=HTMLResponse)
        async def dashboard():
            """Legacy standalone evolution dashboard (kept for compatibility)."""
            try:
                return HTMLResponse(content=_dashboard_path.read_text(encoding="utf-8"))
            except FileNotFoundError:
                return HTMLResponse(content="<h1>dashboard.html not found</h1>", status_code=404)

        @app.post("/trigger")
        async def trigger_evolve(body: dict[str, Any] = Body(default_factory=dict)):
            # Allow callers to override viking identity / customer so the server
            # can read sessions written by a different account/user/peer namespace.
            if not isinstance(body, dict):
                body = {}
            # Serialize the whole cycle: the override branch swaps shared instance
            # state in place and run_once does a non-atomic manifest read-modify-
            # write. Without this, two concurrent /trigger calls interleave and
            # lose each other's uploaded skills.
            async with self._get_run_lock():
                  eval_run_id = str(body.get("eval_run_id") or "").strip()
                  saved_evobench_run_id = None
                  if eval_run_id:
                      import os

                      saved_evobench_run_id = os.environ.get("EVOBENCH_RUN_ID")
                      os.environ["EVOBENCH_RUN_ID"] = eval_run_id
                  try:
                      if any(
                          [
                              body.get("viking_account"),
                              body.get("viking_user"),
                              body.get("viking_agent_id"),
                              body.get("viking_customer_id"),
                              body.get("group_id"),
                              body.get("viking_group_id"),
                              body.get("viking_root_prefix"),
                          ]
                      ):
                          override_group_id = (
                              str(body.get("group_id") or body.get("viking_group_id") or "")
                              .split(",")[0]
                              .strip()
                              .strip("/")
                          )
                          override_config = replace(
                              self.config,
                              viking_account=str(body.get("viking_account") or self.config.viking_account),
                              viking_user=str(body.get("viking_user") or self.config.viking_user),
                              viking_agent_id=str(body.get("viking_agent_id") or self.config.viking_agent_id),
                              viking_customer_id=str(body.get("viking_customer_id") or self.config.viking_customer_id),
                              viking_group_id=override_group_id or self.config.viking_group_id,
                              viking_root_prefix=str(body.get("viking_root_prefix") or self.config.viking_root_prefix),
                          )
                          saved_bucket = self._bucket
                          saved_skill_bucket = self._skill_bucket
                          saved_skill_prefix = self._skill_prefix
                          saved_session_prefix = self._session_prefix
                          saved_registry = self._id_registry
                          try:
                              self._bucket = self._build_bucket(override_config)
                              self._skill_bucket = self._build_skill_bucket(override_config)
                              self._skill_prefix = self._skill_prefix_for_config(override_config)
                              # Keep the session queue pooled at the team root even when a
                              # caller overrides the peer/customer identity: evolution reads
                              # all peers' sessions together (see __init__).
                              self._session_prefix = ""
                              # Load the registry from the overridden group's namespace so we
                              # never persist the default group's registry into another group.
                              self._id_registry = SkillIDRegistry()
                              self._id_registry.load_from_oss(self._skill_bucket, self._skill_prefix)
                              result = await self.run_once()
                          finally:
                              self._bucket = saved_bucket
                              self._skill_bucket = saved_skill_bucket
                              self._skill_prefix = saved_skill_prefix
                              self._session_prefix = saved_session_prefix
                              self._id_registry = saved_registry
                          return JSONResponse(content=result)
                      return JSONResponse(content=await self.run_once())
                  finally:
                      if eval_run_id:
                          import os

                          if saved_evobench_run_id is None:
                              os.environ.pop("EVOBENCH_RUN_ID", None)
                          else:
                              os.environ["EVOBENCH_RUN_ID"] = saved_evobench_run_id

        @app.get("/status")
        async def status():
            # Surface externally-seeded skills (e.g. StaffDeck's initial library)
            # promptly, without waiting for the next evolution cycle. Throttled so
            # frequent dashboard polling doesn't hammer the store.
            now = time.monotonic()
            if now - self._status_seed_merge_ts >= 5.0:
                self._status_seed_merge_ts = now
                try:
                    await self._call_storage(
                        self._id_registry.merge_from_oss, self._skill_bucket, self._skill_prefix
                    )
                except Exception:  # noqa: BLE001 - status must never 5xx on a soft read.
                    logger.debug("status seed merge failed", exc_info=True)
            entries = self._id_registry.all_entries()
            pending_keys = await self._call_storage(list_session_keys, self._bucket, self._session_prefix)
            return JSONResponse(
                content={
                    "running": self._running,
                    "pending_sessions": len(pending_keys),
                    "registered_skills": len(entries),
                    "skills": {
                        name: {
                            "skill_id": item.get("skill_id") or item.get("name") or name,
                            "version": item.get("version") or (item.get("labels") or {}).get("latest") or 0,
                        }
                        for name, item in entries.items()
                    },
                }
            )

        @app.get("/health")
        async def health():
            return {"status": "ok"}

        @app.get("/history")
        async def history(session_id: str = "", limit: int = 20):
            """Read back recent evolution cycles from ``evolve_history.jsonl``.

            Read-only. Optionally filter to cycles that consumed ``session_id``
            (matched against the cycle's top-level ``session_ids`` or any
            ``evolutions[].session_ids``). ``limit`` caps how many matching
            cycles are returned, newest first. Best-effort: a missing/corrupt
            history file yields an empty list rather than an error.
            """
            wanted = str(session_id or "").strip()
            capped = max(1, min(int(limit or 20), 200))
            path = self.config.history_path
            cycles: list[dict[str, Any]] = []
            try:
                with open(path, "r", encoding="utf-8") as handle:
                    for line in handle:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            record = json.loads(line)
                        except Exception:
                            continue
                        if not isinstance(record, dict):
                            continue
                        if wanted:
                            ids = set(record.get("session_ids") or [])
                            for evo in record.get("evolutions") or []:
                                if isinstance(evo, dict):
                                    ids.update(evo.get("session_ids") or [])
                            if wanted not in ids:
                                continue
                        cycles.append(record)
            except FileNotFoundError:
                cycles = []
            except Exception as exc:
                logger.warning("[EvolveServer] history read failed: %s", exc)
                cycles = []
            cycles.reverse()
            return JSONResponse(content={"reachable": True, "cycles": cycles[:capped]})

        @app.get("/sessions")
        async def sessions():
            """List sessions currently queued for evolution, with their sender.

            Each row is ``{session_id, user_alias, timestamp, num_turns}``.
            ``user_alias`` is the username the submitter attached at ingest time
            (required), so the dashboard can show who fed each session.
            """
            rows = await self.list_queued_sessions()
            return JSONResponse(content={"reachable": True, "sessions": rows})

        @app.get("/conversations")
        async def conversations(limit: int = 100):
            """Durable conversation ledger: title + sender + consumption status.

            Unlike ``/sessions`` (only what's still queued), this survives a
            session being drained: each row is ``{session_id, title,
            user_alias, timestamp, num_turns, status, ingested_at,
            consumed_at}`` where ``status`` is ``queued`` or ``consumed``.
            """
            rows = await self.list_session_ledger(int(limit or 100))
            return JSONResponse(content={"reachable": True, "conversations": rows})

        @app.get("/conversations/{session_id}")
        async def conversation_detail(session_id: str):
            """One conversation's turns + metadata (for the title click-through).

            Turns come from the live queue if still pending, else the
            post-consume archive. Returns 404 if the session is unknown; a
            known-but-pre-archival session returns ``turns_available=false``.
            """
            result = await self.get_session_detail(session_id)
            if result.get("status") == "not_found":
                return JSONResponse(status_code=404, content=result)
            return JSONResponse(content=result)

        @app.get("/conversations/{session_id}/process")
        async def conversation_process(session_id: str):
            """The evolution process this session took part in (status click-through).

            Read-only reconstruction from ``evolve_history.jsonl``: per cycle
            that referenced the session, its judge scores + which skills it
            helped evolve + the resulting action.
            """
            result = await self.get_session_process(session_id)
            if result.get("status") == "not_found":
                return JSONResponse(status_code=404, content=result)
            return JSONResponse(content=result)

        @app.get("/storage/status")
        async def storage_status_route():
            """Object-store connection status (OpenViking reachability)."""
            return JSONResponse(content=await self.storage_status())

        @app.get("/skills/{name}/versions")
        async def skill_versions(name: str):
            return JSONResponse(content=await self.list_skill_versions(name))

        @app.get("/skills/{name}/versions/{version}")
        async def skill_version_detail(name: str, version: int):
            """One version's SKILL.md content + parsed description/body.

            Powers the skill detail panel and lets the dashboard switch between
            versions to compare content. Also attaches the ``evolution`` block
            (optimization items, rationale, skill diff, True Replay evidence) so
            remote consumers (e.g. AgentsHub) get the same context the local
            console shows, without needing an authenticated admin session.
            """
            result = await self.get_skill_version(name, version)
            status_code = 404 if result.get("status") == "not_found" else 200
            if status_code == 200:
                try:
                    from ...proxy.skills_admin import _version_evolution_context

                    versions_payload = await self.list_skill_versions(name)
                    history = versions_payload.get("history") or []
                    result["evolution"] = await asyncio.to_thread(
                        _version_evolution_context,
                        self.config,
                        name=self._sanitise_name(name),
                        version=int(version),
                        history=history,
                        store=self._validation_store,
                    )
                except Exception:  # noqa: BLE001 - version content stays available.
                    logger.warning(
                        "[EvolveServer] failed to attach evolution context for %s v%s",
                        name,
                        version,
                        exc_info=True,
                    )
            return JSONResponse(content=result, status_code=status_code)

        @app.post("/skills/{name}/rollback")
        async def skill_rollback(name: str, target_version: int):
            result = await self.rollback_skill(name, target_version)
            status_code = 200 if result.get("status") == "rolled_back" else 400
            return JSONResponse(content=result, status_code=status_code)

        @app.get("/validation/candidates")
        async def validation_candidates(
            scope: str = "open",
            limit: int = 20,
            offset: int = 0,
            compact: bool = False,
        ):
            """Validation jobs plus automatic replay results."""
            candidates = await self._call_storage(
                self._list_validation_candidates,
                scope,
            )
            safe_limit = min(200, max(1, int(limit or 20)))
            safe_offset = max(0, int(offset or 0))
            page = candidates[safe_offset : safe_offset + safe_limit]
            if compact:
                page = [
                    {
                        key: value
                        for key, value in item.items()
                        if key not in {"candidate_skill", "current_skill"}
                    }
                    for item in page
                ]
            return JSONResponse(
                content={
                    "candidates": page,
                    "total": len(candidates),
                    "limit": safe_limit,
                    "offset": safe_offset,
                    "has_more": safe_offset + len(page) < len(candidates),
                }
            )

        @app.get("/validation/candidates/{job_id}")
        async def validation_candidate_detail(job_id: str):
            job = await self._call_storage(
                self._validation_store.load_job,
                job_id,
            )
            if not job:
                return JSONResponse(
                    content={"status": "not_found", "job_id": job_id},
                    status_code=404,
                )
            return JSONResponse(
                content=await self._call_storage(
                    self._validation_candidate_payload,
                    job,
                )
            )

        @app.post("/validation/candidates/{job_id}/evaluate")
        async def validation_evaluate(job_id: str, refresh: bool = False):
            """Run True Replay metrics for a candidate without publishing.

            Returns the cached evaluation when present unless ``?refresh=true``.
            The dashboard shows interaction turns, tool calls, and tokens.
            """
            result = await self.evaluate_candidate(job_id, refresh=bool(refresh))
            status_code = 404 if result.get("status") == "not_found" else 200
            return JSONResponse(content=result, status_code=status_code)

        @app.post("/validation/candidates/{job_id}/validate")
        async def validation_validate(job_id: str, body: dict[str, Any] = Body(default_factory=dict)):
            """Replay-validate one candidate and publish it.

            ``mode`` in the body selects the gate: ``"auto"`` (default) uses
            turn-first True Replay comparison; ``"force"`` always publishes.
            """
            reviewer = str((body or {}).get("reviewer") or "staffdeck-reviewer").strip() or "staffdeck-reviewer"
            mode = str((body or {}).get("mode") or "auto").strip().lower() or "auto"
            result = await self.review_validate_candidate(job_id, reviewer=reviewer, mode=mode)
            status_map = {"not_found": 404, "error": 400}
            status_code = status_map.get(str(result.get("status")), 200)
            return JSONResponse(content=result, status_code=status_code)

        @app.delete("/validation/candidates/{job_id}")
        async def validation_delete(job_id: str):
            """Discard a pending candidate: remove the job and all its artifacts.

            Used when a reviewer decides a candidate should not be published and
            should stop appearing in the review queue. Best-effort per key."""
            result = await self._call_storage(self._validation_store.delete_job, job_id)
            return JSONResponse(content={"status": "deleted", **result})

        @app.post("/ingest_session")
        async def ingest_session(
            body: dict[str, Any] = Body(default_factory=dict),
            trigger: bool = False,
            authorization: str = Header(default=""),
        ):
            """Push a single session into the evolution queue.

            Lets remote machines feed sessions without any OpenViking
            credentials or knowledge of the queue path — the server owns the
            storage identity. Body is the session payload
            (``{session_id, turns:[{prompt_text, response_text, ...}]}``); set
            ``?trigger=true`` (or ``{"trigger": true}``) to run a cycle now
            instead of waiting for the periodic loop.

            When ``ingest_api_key`` is configured, callers must send
            ``Authorization: Bearer <key>``.
            """
            expected = str(getattr(self.config, "ingest_api_key", "") or "")
            if expected:
                auth = authorization or ""
                token = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
                if token != expected:
                    return JSONResponse(
                        content={"status": "error", "error": "unauthorized"},
                        status_code=401,
                    )
            if not isinstance(body, dict):
                body = {}
            trigger_flag = bool(trigger) or bool(body.get("trigger"))
            payload = {k: v for k, v in body.items() if k != "trigger"}
            try:
                result = await self.ingest_session(payload, trigger=trigger_flag)
            except ValueError as exc:
                return JSONResponse(
                    content={"status": "error", "error": str(exc)},
                    status_code=400,
                )
            return JSONResponse(content=result)

        return app
