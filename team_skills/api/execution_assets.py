"""Business execution profiles and Candidate component edits; short transactions only."""
from __future__ import annotations

import asyncio

from fastapi import APIRouter, Request
from starlette.exceptions import HTTPException

from session_ingestion.queries import SessionQueries
from session_ingestion.schemas import session_payload_hash
from teamEvolver.tenants.registry import current_tenant_id

from ..candidates.repository import CandidateRepository, CandidateStateError
from ..config import load_skills_config
from ..evolution.candidate_pipeline import enqueue_command
from ..evolution.history import candidate_payload
from ..execution.candidates import revise_execution_detail
from ..execution.contracts import validate_profile
from ..execution.inputs import has_template_input_contract, shared_template_match_report
from ..execution.registration import agent_integration_template, registration_preview
from ..execution.release_queries import release_details
from ..execution.repository import ExecutionRepository, ExecutionVersionConflict
from ..execution.variable_registration import variable_context_registration
from ..library.repository import SkillLibraryError, SkillLibraryRepository
from .candidates import accepted, actor, command_id, error
from .common import read_json


def create_router(services):
    router = APIRouter(tags=["skill-evolution"])
    tx, configs = services.tx, services.extras["config_repo"]
    repo = ExecutionRepository(tx)

    async def show(conn, profile_id):
        profile, release = await repo.current(conn, profile_id)
        return {**profile, **release["document"], "composition_hash": release["composition_hash"]}

    async def dependencies(conn, value):
        library = SkillLibraryRepository(tx)
        result = []
        for name in value["affected_skill_ids"]:
            head = await library.get_skill(conn, name)
            if not head:
                raise SkillLibraryError(f"affected Skill does not exist: {name}")
            result.append({"name": name, "version": head["version"], "tree_sha256": head["tree_sha256"]})
        return result

    @router.get("/api/skill-evolution/execution-profiles")
    async def listing(request: Request):
        actor(request)
        async with tx.tenant_tx(current_tenant_id()) as conn:
            return {"profiles": [await show(conn, p["profile_id"]) for p in await repo.profiles(conn)]}

    @router.post("/api/skill-evolution/execution-profiles", status_code=201)
    async def register(request: Request):
        reviewer = actor(request)
        raw = await read_json(request)
        try:
            value = validate_profile(raw)
            async with tx.tenant_tx(current_tenant_id()) as conn:
                pinned_skills = await dependencies(conn, value)
                await repo.register(conn, tenant_id=current_tenant_id(), raw=raw,
                                    skill_dependencies=pinned_skills, actor=reviewer)
                return await show(conn, value["profile_id"])
        except SkillLibraryError as exc:
            raise error(exc) from exc

    @router.post("/api/skill-evolution/execution-profiles/preview")
    async def preview_registration(request: Request):
        actor(request)
        raw = await read_json(request)
        try:
            value = validate_profile(raw)
            preview = registration_preview(raw)
            async with tx.tenant_tx(current_tenant_id()) as conn:
                await dependencies(conn, value)
                await repo.check_registration_available(
                    conn, value["profile_id"], [part["component_id"] for part in value["components"]],
                )
            return preview
        except SkillLibraryError as exc:
            raise error(exc) from exc

    async def variable_contexts(profile_id, request, *, create):
        reviewer = actor(request)
        raw = await read_json(request)
        try:
            async with tx.tenant_tx(current_tenant_id()) as conn:
                source = await repo.profile(conn, profile_id, for_update=create)
                if raw.get("source_release_id") != source["current_release_id"]:
                    raise ExecutionVersionConflict("source release changed; refresh and preview again")
                release = await repo.release(conn, source["current_release_id"])
                derived, preview = variable_context_registration(source, release, raw)
                if not create:
                    await repo.check_registration_available(
                        conn, derived["profile_id"], [part["component_id"] for part in preview["components"]],
                    )
                    return preview
                await repo.register(conn, tenant_id=current_tenant_id(), raw=derived,
                                    skill_dependencies=release["document"]["skill_dependencies"], actor=reviewer)
                return {**await show(conn, derived["profile_id"]), "registration_summary": preview["summary"]}
        except SkillLibraryError as exc:
            raise error(exc) from exc

    @router.post("/api/skill-evolution/execution-profiles/{profile_id}/variable-contexts/preview")
    async def preview_variable_contexts(profile_id: str, request: Request):
        return await variable_contexts(profile_id, request, create=False)

    @router.post("/api/skill-evolution/execution-profiles/{profile_id}/variable-contexts", status_code=201)
    async def register_variable_contexts(profile_id: str, request: Request):
        return await variable_contexts(profile_id, request, create=True)

    @router.get("/api/skill-evolution/execution-profiles/{profile_id}/integration")
    async def integration_template(profile_id: str, request: Request):
        actor(request)
        try:
            async with tx.tenant_tx(current_tenant_id()) as conn:
                profile, release = await repo.current(conn, profile_id)
                return agent_integration_template(profile, release)
        except SkillLibraryError as exc:
            raise error(exc) from exc

    @router.get("/api/skill-evolution/execution-profiles/{profile_id}/session-match")
    async def session_match(profile_id: str, request: Request, session_id: str):
        actor(request)
        if not session_id.strip() or len(session_id) > 512:
            raise HTTPException(400, "session_id must be a bounded archived Session ID")
        try:
            async with tx.tenant_tx(current_tenant_id()) as conn:
                _, release = await repo.current(conn, profile_id)
                row = await SessionQueries(tx).get_session(conn, session_id=session_id)
            if row is None:
                raise HTTPException(404, "Session not found")
            payloads = services.extras.get("session_payloads")
            if payloads is None:
                raise HTTPException(503, "Session payload reader is not configured")
            session = await asyncio.to_thread(payloads.get_session, row["content_ref"])
            if session_payload_hash(session) != row["content_hash"]:
                raise HTTPException(409, "Session content differs from its archived hash")
            session = {**session, "session_id": row["session_id"]}
            baseline = release["document"]["execution_plan"]
            from ..evolution.session_processing import evidence_source_key, project_prompt_session
            projection = project_prompt_session(session, baseline)
            target_session = projection if projection is not None else session
            if has_template_input_contract(baseline):
                result = shared_template_match_report(target_session, baseline)
            else:
                from ..execution.evolution import prompt_session_matches
                matched = prompt_session_matches(target_session, baseline)
                result = {"matched": matched, "reason": "matched" if matched else "observed_prompt_not_matched",
                          "targets": []}
            return {"profile_id": profile_id, "session_id": session_id, **result, "decision": row["decision"],
                    "release_id": release["release_id"], "composition_hash": release["composition_hash"],
                    "eligible_for_evolution": bool(result["matched"] and row["decision"] == "valuable"
                                                   and evidence_source_key(target_session)),
                    "note": "Matching checks source attribution only; "
                            "evolution still applies consumption and group gates."}
        except SkillLibraryError as exc:
            raise error(exc) from exc

    @router.get("/api/skill-evolution/execution-profiles/{profile_id}")
    async def detail(profile_id: str, request: Request):
        actor(request)
        try:
            async with tx.tenant_tx(current_tenant_id()) as conn:
                value = await show(conn, profile_id)
                value["releases"] = await repo.releases(conn, profile_id)
                rows = await conn.fetch(
                    f"SELECT document,created_at FROM {tx.pool.schema}.execution_receipts "
                    "WHERE profile_id=$1 ORDER BY created_at DESC LIMIT 20", profile_id,
                )
                from ..evolution.repository import unpack
                value["receipts"] = [{**unpack(r["document"]), "at": r["created_at"].isoformat()} for r in rows]
                return value
        except SkillLibraryError as exc:
            raise error(exc) from exc

    @router.get("/api/skill-evolution/execution-profiles/{profile_id}/releases/{release_id}")
    async def release_detail(profile_id: str, release_id: str, request: Request):
        actor(request)
        try:
            async with tx.tenant_tx(current_tenant_id()) as conn:
                return await release_details(conn, tx, profile_id=profile_id, release_id=release_id)
        except SkillLibraryError as exc:
            raise error(exc) from exc

    @router.post("/api/skill-evolution/execution-profiles/{profile_id}/rollback")
    async def rollback(profile_id: str, request: Request):
        reviewer = actor(request)
        raw = await read_json(request)
        if set(raw) != {"release_id", "expected_release_id"}:
            raise HTTPException(400, "rollback requires release_id and expected_release_id")
        async with tx.tenant_tx(current_tenant_id()) as conn:
            config = await load_skills_config(configs, conn)
            command, _ = await enqueue_command(
                conn, services, tenant_id=current_tenant_id(), command_id=command_id(request, "execution-rollback"),
                kind="skill.evolve", request={"profile_id": profile_id, **raw}, payload={
                    "operation": "execution_rollback", "profile_id": profile_id, **raw, "actor": reviewer,
                    "config": config.config, "config_revision": config.revision,
                },
            )
        return accepted(command)

    @router.put("/api/skill-candidates/{candidate_id}/execution-content")
    async def edit(candidate_id: str, request: Request):
        reviewer = actor(request)
        raw = await read_json(request)
        if set(raw) != {"component_changes", "candidate_revision"}:
            raise HTTPException(400, "expected component_changes and candidate_revision")
        try:
            async with tx.tenant_tx(current_tenant_id()) as conn:
                candidates = CandidateRepository(tx)
                item = await candidates.require(conn, candidate_id, for_update=True)
                candidates._check_revision(item, raw["candidate_revision"])
                if item["status"] != "open":
                    raise CandidateStateError("business components may only be edited on an open candidate")
                detail = revise_execution_detail(item["detail"], raw["component_changes"])
                changed = await candidates.revise_detail(
                    conn, tenant_id=current_tenant_id(), candidate_id=candidate_id,
                    detail=detail, actor=reviewer, expected_revision=raw["candidate_revision"],
                )
                return await candidate_payload(conn, tx, changed)
        except SkillLibraryError as exc:
            raise error(exc) from exc

    @router.post("/api/skill-candidates/{candidate_id}/independent-validation")
    async def independent_validation(candidate_id: str, request: Request):
        reviewer = actor(request)
        raw = await read_json(request)
        if set(raw) != {"candidate_revision", "status", "method", "evidence_ref", "case_ids", "notes"}:
            raise HTTPException(400, "expected revision, status, method, evidence_ref, case_ids and notes")
        if not isinstance(raw["status"], str) or raw["status"] not in {"passed", "failed", "inconclusive"}:
            raise HTTPException(400, "invalid independent verification status")
        if not isinstance(raw["method"], str) or raw["method"] not in {"programmatic", "domain_review"}:
            raise HTTPException(400, "independent verification requires code or domain review")
        if (not isinstance(raw["evidence_ref"], str) or not 1 <= len(raw["evidence_ref"]) <= 2000
                or not isinstance(raw["notes"], str) or len(raw["notes"]) > 8000
                or not isinstance(raw["case_ids"], list) or not raw["case_ids"]
                or any(not isinstance(cid, str) for cid in raw["case_ids"])):
            raise HTTPException(400, "independent verification requires bounded evidence and case references")
        try:
            async with tx.tenant_tx(current_tenant_id()) as conn:
                candidates = CandidateRepository(tx)
                item = await candidates.require(conn, candidate_id, for_update=True)
                candidates._check_revision(item, raw["candidate_revision"])
                if item["status"] != "open":
                    raise CandidateStateError("verification can only attach to an open candidate")
                detail = dict(item["detail"])
                from ..execution.reviewed_publication import REVIEW_CONTRACT, completed_validation
                validation = await completed_validation(conn, tx, item)
                frozen = validation["payload"]["detail"]
                available = {c["case_id"] for c in frozen.get("replay_cases", [])}
                if not available or not set(raw["case_ids"]) <= available:
                    raise SkillLibraryError("verification case is not part of the candidate Dataset")
                if not available <= set(raw["case_ids"]):
                    raise SkillLibraryError("independent verification must cover every current candidate Case")
                detail["independent_validation"] = {**raw, "reviewer": reviewer,
                    "schema_version": REVIEW_CONTRACT, "evidence_level": "reviewer_attested",
                    **{key: validation[key] for key in (
                        "validation_run_id", "validation_payload_hash", "validation_result_hash",
                    )},
                    "validation_input_hash": (frozen.get("validation_plan") or {}).get("input_hash"),
                }
                from ..evolution.repository import canonical
                await conn.execute(
                    f"UPDATE {tx.pool.schema}.skill_candidates SET detail=$2::jsonb,updated_at=now() "
                    "WHERE candidate_id=$1", candidate_id, canonical(detail),
                )
                await candidates._event(
                    conn, tenant_id=current_tenant_id(), candidate_id=candidate_id,
                    kind="independent_validation", from_status=item["status"], to_status=item["status"],
                    revision=item["revision"], actor=reviewer, detail=detail["independent_validation"],
                )
                return await candidate_payload(conn, tx, await candidates.require(conn, candidate_id))
        except SkillLibraryError as exc:
            raise error(exc) from exc

    @router.post("/api/skill-candidates/{candidate_id}/resume-after-rollback")
    async def resume_after_rollback(candidate_id: str, request: Request):
        reviewer = actor(request)
        raw = await read_json(request)
        if set(raw) != {"candidate_revision", "expected_release_id", "component_changes", "reason"}:
            raise HTTPException(400, "expected candidate_revision, expected_release_id, component_changes and reason")
        try:
            async with tx.tenant_tx(current_tenant_id()) as conn:
                changed = await CandidateRepository(tx).resume_after_rollback(
                    conn, tenant_id=current_tenant_id(), candidate_id=candidate_id,
                    expected_revision=raw["candidate_revision"], expected_release_id=raw["expected_release_id"],
                    changes=raw["component_changes"], reason=raw["reason"], actor=reviewer,
                )
                return await candidate_payload(conn, tx, changed)
        except SkillLibraryError as exc:
            raise error(exc) from exc

    @router.post("/api/skill-candidates/{candidate_id}/validation-budget")
    async def validation_budget(candidate_id: str, request: Request):
        reviewer = actor(request)
        raw = await read_json(request)
        if set(raw) != {"candidate_revision", "max_total_tokens", "reason"}:
            raise HTTPException(400, "expected candidate_revision, max_total_tokens and authorization reason")
        try:
            async with tx.tenant_tx(current_tenant_id()) as conn:
                changed = await CandidateRepository(tx).increase_validation_budget(
                    conn, tenant_id=current_tenant_id(), candidate_id=candidate_id,
                    expected_revision=raw["candidate_revision"], maximum=raw["max_total_tokens"],
                    reason=raw["reason"], actor=reviewer,
                )
                return await candidate_payload(conn, tx, changed)
        except SkillLibraryError as exc:
            raise error(exc) from exc

    return router
