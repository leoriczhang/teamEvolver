"""REST routes for the developer-facing Skills experiment lab."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
from typing import Any

from fastapi import FastAPI, HTTPException, Request

from ..skill_lab import (
    SkillLabError,
    SkillLabStore,
    evolution_datasets,
    parse_dataset_markdown,
    parse_skill_markdown,
    prepare_experiment_job,
    resolve_dataset,
)
from ..dataset_synthesizer import synthesize_evolution_datasets
from .skills_admin import _clear_version_cache, _require_admin_request

logger = logging.getLogger(__name__)


class SkillLabMixin:
    """Dataset CRUD, True Replay execution, and durable trace endpoints."""

    async def _execute_skill_lab_run(
        self,
        *,
        store: SkillLabStore,
        run_id: str,
        job: dict[str, Any],
        timeout_seconds: int,
        max_interactions: int,
    ) -> None:
        try:
            from ..true_replay import evaluate_job

            result = await asyncio.to_thread(
                evaluate_job,
                run_id,
                job=job,
                case_index=0,
                timeout=timeout_seconds,
                max_interactions=max_interactions,
            )
            replay_status = str(result.get("status") or "")
            status = "completed" if replay_status == "evaluated" else (
                "failed" if replay_status in {"failed", "not_found"} else "skipped"
            )
        except Exception as exc:  # noqa: BLE001 - persist failure for debugging.
            logger.exception("[SkillLab] experiment %s failed", run_id)
            result = {
                "status": "failed",
                "mode": "true_replay",
                "job_id": run_id,
                "accepted": False,
                "reason": f"{type(exc).__name__}: {exc}",
                "cases": [],
            }
            status = "failed"
        try:
            store.finish_run(run_id, result=result, status=status)
        except Exception:
            logger.exception("[SkillLab] failed to persist experiment %s", run_id)

    def _register_skill_lab_routes(self, app: FastAPI) -> None:
        owner = self

        def lab_store() -> SkillLabStore:
            return SkillLabStore.from_config(owner.config)

        def merged_datasets(skill_name: str) -> list[dict[str, Any]]:
            store = lab_store()
            manual = store.list_datasets(skill_name=skill_name)
            evolved = evolution_datasets(owner.config, skill_name=skill_name)
            return [*manual, *evolved]

        def latest_skill_session_id(skill_name: str) -> str:
            try:
                from ..session_store import SessionStore

                store = SessionStore.from_config(owner.config)
                for row in store.list_conversations(limit=500):
                    session_id = str(row.get("session_id") or "")
                    session = store.load_session(session_id)
                    if not isinstance(session, dict):
                        continue
                    references = {
                        str(item or "")
                        for item in (
                            list(session.get("used_skills") or [])
                            + list(session.get("injected_skills") or [])
                        )
                    }
                    for turn in session.get("turns") or []:
                        if not isinstance(turn, dict):
                            continue
                        references.update(
                            str(item or "")
                            for item in (
                                list(turn.get("used_skills") or [])
                                + list(turn.get("injected_skills") or [])
                            )
                        )
                    if skill_name in references:
                        return session_id
            except Exception:
                return ""
            return ""

        @app.get("/api/skill-lab/datasets")
        async def api_skill_lab_datasets(skill_name: str):
            try:
                datasets = merged_datasets(skill_name)
            except (SkillLabError, ValueError) as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            return {
                "datasets": datasets,
                "total": len(datasets),
                "manual_count": sum(
                    1
                    for item in datasets
                    if str((item.get("source") or {}).get("kind") or "") != "evolution"
                ),
                "evolution_count": sum(
                    1
                    for item in datasets
                    if str((item.get("source") or {}).get("kind") or "") == "evolution"
                ),
            }

        @app.post("/api/skill-lab/datasets")
        async def api_skill_lab_save_dataset(body: dict[str, Any]):
            payload = dict(body)
            markdown = str(payload.get("dataset_markdown") or "").strip()
            if markdown:
                try:
                    sections = parse_dataset_markdown(markdown)
                except SkillLabError as exc:
                    raise HTTPException(status_code=400, detail=str(exc)) from exc
                for key, value in sections.items():
                    if not str(payload.get(key) or "").strip():
                        payload[key] = value
            files = payload.pop("files", None)
            if files is not None and not isinstance(files, list):
                raise HTTPException(status_code=400, detail="files 必须是数组")
            try:
                dataset = lab_store().save_dataset(payload, files=files)
            except (SkillLabError, ValueError) as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            return dataset

        @app.post("/api/skill-lab/datasets/synthesize")
        async def api_skill_lab_synthesize_datasets(body: dict[str, Any]):
            """Generate editable tests from archived sessions and SOP evidence."""
            skill_name = str(body.get("skill_name") or "").strip()
            if not skill_name:
                raise HTTPException(status_code=400, detail="skill_name 不能为空")
            requested_ids = {
                str(item or "").strip()
                for item in body.get("session_ids") or []
                if str(item or "").strip()
            }
            try:
                from ..evolve.kernel.llm import AsyncLLMClient
                from ..evolve.stages.summarize import (
                    _extract_session_metadata,
                    build_session_trajectory,
                )
                from ..session_store import SessionStore
                from ..skill_lab import parse_skill_markdown
                from ..skills import editor
                from ..validation.store import ValidationStore

                session_store = SessionStore.from_config(owner.config)
                rows = session_store.list_conversations(limit=500)
                sessions: list[dict[str, Any]] = []
                for row in rows:
                    session_id = str(row.get("session_id") or "")
                    if requested_ids and session_id not in requested_ids:
                        continue
                    session = session_store.load_session(session_id)
                    if not isinstance(session, dict):
                        continue
                    references = {
                        str(item or "")
                        for item in (
                            list(session.get("used_skills") or [])
                            + list(session.get("injected_skills") or [])
                        )
                    }
                    for turn in session.get("turns") or []:
                        if not isinstance(turn, dict):
                            continue
                        references.update(
                            str(item or "")
                            for item in (
                                list(turn.get("used_skills") or [])
                                + list(turn.get("injected_skills") or [])
                            )
                        )
                    if not requested_ids and skill_name not in references:
                        continue
                    _extract_session_metadata(session)
                    session["_trajectory"] = (
                        session.get("_trajectory")
                        or build_session_trajectory(session)
                    )
                    sessions.append(session)
                    if len(sessions) >= 30:
                        break
                if not sessions:
                    raise HTTPException(
                        status_code=404,
                        detail="没有找到该 Skill 关联的历史 Session",
                    )

                validation_store = ValidationStore.from_config(owner.config)
                jobs = [
                    job
                    for job in validation_store.list_jobs()
                    if str(
                        (job.get("candidate_skill") or {}).get("name")
                        if isinstance(job.get("candidate_skill"), dict)
                        else job.get("candidate_skill_name") or ""
                    )
                    == skill_name
                ]
                latest = jobs[-1] if jobs else {}
                replay_windows: dict[str, list[dict[str, Any]]] = {
                    "recent": [],
                    "historical": [],
                }
                for case in latest.get("replay_cases") or []:
                    if not isinstance(case, dict):
                        continue
                    window = str(case.get("evidence_window") or "recent")
                    replay_windows[
                        window if window in replay_windows else "recent"
                    ].append(case)

                detail = editor.get_skill(str(owner.config.skills_dir), skill_name)
                candidate_skill = parse_skill_markdown(
                    str(detail.get("skill_md") or "")
                )
                candidate_skill["_evidence_classification"] = (
                    latest.get("evidence_classification")
                    if isinstance(latest.get("evidence_classification"), dict)
                    else {}
                )
                llm = AsyncLLMClient(
                    api_key=str(owner.config.llm_api_key or ""),
                    base_url=str(owner.config.llm_api_base or ""),
                    model=str(
                        owner.config.llm_model_id
                        or owner.config.model_name
                        or ""
                    ),
                    max_tokens=int(owner.config.llm_max_tokens or 100_000),
                    temperature=float(owner.config.llm_temperature),
                )
                generated = await synthesize_evolution_datasets(
                    llm,
                    skill_name=skill_name,
                    sessions=sessions,
                    candidate_skill=candidate_skill,
                    evidence_context=(
                        latest.get("evolution_context")
                        if isinstance(latest.get("evolution_context"), dict)
                        else {}
                    ),
                    replay_windows=replay_windows,
                    case_count=max(
                        1,
                        min(6, int(body.get("case_count") or 2)),
                    ),
                    min_requirements=max(
                        1,
                        int(body.get("min_requirements") or 12),
                    ),
                    max_requirements=max(
                        1,
                        int(body.get("max_requirements") or 24),
                    ),
                    batch_size=max(
                        1,
                        int(body.get("disclosure_batch_size") or 4),
                    ),
                )
                store = lab_store()
                saved = [
                    store.save_dataset(
                        {
                            "skill_name": skill_name,
                            "name": dataset.get("name"),
                            "query": dataset.get("query"),
                            "requirements": dataset.get("requirements"),
                            "trajectory_requirements": dataset.get(
                                "trajectory_requirements"
                            ),
                            "progressive_disclosure": dataset.get(
                                "progressive_disclosure"
                            ),
                            "source": {
                                "kind": "synthesized",
                                "source_session_ids": dataset.get(
                                    "source_session_ids"
                                )
                                or [],
                                "evidence_window": dataset.get(
                                    "evidence_window"
                                ),
                                "candidate_job_id": latest.get("job_id"),
                                "synthesis_mode": dataset.get(
                                    "synthesis_mode"
                                ),
                            },
                        },
                        files=[],
                    )
                    for dataset in generated
                ]
            except HTTPException:
                raise
            except (SkillLabError, ValueError) as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            return {
                "datasets": saved,
                "count": len(saved),
                "source_session_count": len(sessions),
            }

        @app.post("/api/skill-lab/datasets/{dataset_id}/clone")
        async def api_skill_lab_clone_dataset(
            dataset_id: str,
            body: dict[str, Any],
        ):
            skill_name = str(body.get("skill_name") or "").strip()
            store = lab_store()
            try:
                source = resolve_dataset(
                    owner.config,
                    store,
                    skill_name=skill_name,
                    dataset_id=dataset_id,
                )
                if not source:
                    raise HTTPException(status_code=404, detail="数据集不存在")
                cloned = store.save_dataset(
                    {
                        "skill_name": skill_name,
                        "name": str(body.get("name") or f"{source.get('name')} · 副本"),
                        "query": source.get("query"),
                        "requirements": source.get("requirements"),
                        "trajectory_requirements": source.get(
                            "trajectory_requirements"
                        ),
                        "source": {
                            "kind": "cloned",
                            "dataset_id": dataset_id,
                            "origin": source.get("source") or {},
                        },
                    },
                    files=[],
                )
            except (SkillLabError, ValueError) as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            return cloned

        @app.delete("/api/skill-lab/datasets/{dataset_id}")
        async def api_skill_lab_delete_dataset(dataset_id: str):
            try:
                deleted = lab_store().delete_dataset(dataset_id)
            except (SkillLabError, ValueError) as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            if not deleted:
                raise HTTPException(status_code=404, detail="数据集不存在或为只读进化数据集")
            return {"dataset_id": dataset_id, "deleted": True}

        @app.get("/api/skill-lab/runs")
        async def api_skill_lab_runs(skill_name: str = "", limit: int = 100):
            try:
                runs = lab_store().list_runs(skill_name=skill_name, limit=limit)
            except (SkillLabError, ValueError) as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            return {"runs": runs, "total": len(runs)}

        @app.get("/api/skill-lab/runs/{run_id}")
        async def api_skill_lab_run_detail(run_id: str):
            try:
                run = lab_store().load_run(run_id)
            except (SkillLabError, ValueError) as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            if not run:
                raise HTTPException(status_code=404, detail="实验不存在")
            return run

        @app.post("/api/skill-lab/runs")
        async def api_skill_lab_start_run(body: dict[str, Any]):
            skill_name = str(body.get("skill_name") or "").strip()
            dataset_id = str(body.get("dataset_id") or "").strip()
            candidate_skill_md = str(body.get("candidate_skill_md") or "")
            if not skill_name or not dataset_id or not candidate_skill_md.strip():
                raise HTTPException(
                    status_code=400,
                    detail="skill_name、dataset_id 和 candidate_skill_md 均为必填",
                )
            try:
                timeout_default = int(
                    os.environ.get("TEAMEVOLVER_TRUE_REPLAY_TIMEOUT_S", "600")
                )
            except ValueError:
                timeout_default = 600
            timeout_seconds = max(
                10,
                min(3600, int(body.get("timeout_seconds") or timeout_default)),
            )
            store = lab_store()
            try:
                dataset = resolve_dataset(
                    owner.config,
                    store,
                    skill_name=skill_name,
                    dataset_id=dataset_id,
                )
                if not dataset:
                    raise HTTPException(status_code=404, detail="数据集不存在")
                source = (
                    dict(dataset.get("source"))
                    if isinstance(dataset.get("source"), dict)
                    else {}
                )
                if not (
                    source.get("session_id")
                    or source.get("source_session_ids")
                ):
                    fallback_session_id = latest_skill_session_id(skill_name)
                    if fallback_session_id:
                        source["session_id"] = fallback_session_id
                        source["runtime_context_source"] = (
                            "latest_skill_session"
                        )
                        dataset = {**dataset, "source": source}
                requirements = (
                    str(dataset.get("requirements") or "").splitlines()
                )
                trajectory_requirements = (
                    str(
                        dataset.get("trajectory_requirements") or ""
                    ).splitlines()
                )
                checklist_count = len(
                    [
                        line
                        for line in [
                            *requirements,
                            *trajectory_requirements,
                        ]
                        if line.strip()
                    ]
                )
                disclosure = (
                    dataset.get("progressive_disclosure")
                    if isinstance(
                        dataset.get("progressive_disclosure"),
                        dict,
                    )
                    else {}
                )
                batch_size = max(
                    1,
                    int(disclosure.get("batch_size") or 4),
                )
                inferred_interactions = (
                    (checklist_count + batch_size - 1) // batch_size + 1
                )
                max_interactions = max(
                    1,
                    min(
                        20,
                        int(
                            body.get("max_interactions")
                            or inferred_interactions
                            or 4
                        ),
                    ),
                )
                materials = (
                    store.material_payloads(dataset)
                    if not bool(dataset.get("read_only"))
                    else []
                )
                run_id = store.make_run_id()
                job = prepare_experiment_job(
                    skills_dir=str(owner.config.skills_dir),
                    skill_name=skill_name,
                    candidate_skill_md=candidate_skill_md,
                    dataset=dataset,
                    materials=materials,
                    run_id=run_id,
                )
                record = store.create_run(
                    {
                        "run_id": run_id,
                        "skill_name": skill_name,
                        "dataset_id": dataset_id,
                        "dataset_name": dataset.get("name"),
                        "dataset_source": dataset.get("source") or {},
                        "candidate_skill_sha256": hashlib.sha256(
                            candidate_skill_md.encode("utf-8")
                        ).hexdigest(),
                        "candidate_skill_md": candidate_skill_md,
                        "timeout_seconds": timeout_seconds,
                        "max_interactions": max_interactions,
                        "status": "running",
                    }
                )
            except HTTPException:
                raise
            except (SkillLabError, ValueError) as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            owner._safe_create_task(
                owner._execute_skill_lab_run(
                    store=store,
                    run_id=run_id,
                    job=job,
                    timeout_seconds=timeout_seconds,
                    max_interactions=max_interactions,
                )
            )
            return record

        @app.post("/api/skill-lab/skills/{skill_name}/save")
        async def api_skill_lab_save_skill(
            skill_name: str,
            body: dict[str, Any],
            request: Request,
        ):
            """Commit an experimented draft through the existing skill editor."""
            _require_admin_request(request)
            raw = str(body.get("candidate_skill_md") or "")
            try:
                parsed = parse_skill_markdown(raw)
                if parsed["name"] != skill_name:
                    raise SkillLabError("草稿中的 name 与所选 Skill 不一致")
                result = owner._save_skill_lab_draft(skill_name, raw)
            except (SkillLabError, ValueError) as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            return result

    def _save_skill_lab_draft(self, skill_name: str, raw: str) -> dict[str, Any]:
        """Save through the same reload and cloud-sync behavior as skill admin."""
        from ..skills import editor

        parsed = parse_skill_markdown(raw)
        result = editor.save_skill(
            self._skills_dir(),
            name=skill_name,
            description=str(parsed.get("description") or ""),
            category=str(parsed.get("category") or "general"),
            body=str(parsed.get("content") or ""),
            skill_md=raw,
        )
        loaded = self._reload_skill_manager()
        cloud = self._cloud_sync_push(skill_name)
        _clear_version_cache(skill_name)
        return {**result, "loaded_skills": loaded, "cloud": cloud}
