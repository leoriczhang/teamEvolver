"""Resumable Skill Evolution, executed exclusively by the shared Worker."""
from __future__ import annotations

import asyncio
import copy

from session_ingestion.queries import SessionQueries
from team_skills.candidates.repository import CandidateRepository, CandidateStateError
from team_skills.candidates.static_checks import validate_candidate_bundle
from team_skills.config import SkillsModuleConfig
from team_skills.evolution.candidate_pipeline import load_command, submit_validation
from team_skills.evolution.history import skill_payload
from team_skills.evolution.kernel.bundle_changes import materialize_bundle_changes, select_editable_files
from team_skills.evolution.kernel.enums import NO_SKILL_KEY, DecisionAction
from team_skills.evolution.repository import EvolutionRepository
from team_skills.evolution.session_processing import evidence_gap, groups, load_sessions
from team_skills.experiences.patterns import compact_context
from team_skills.experiences.repository import ExperienceRepository
from team_skills.library.bundle import candidate_skill_bundle
from team_skills.library.repository import SkillLibraryRepository
from teamEvolver.runtime import HandlerResult
from teamEvolver.runtime.leases import LeaseGuard

PLANNER_TIMEOUT_SECONDS = 1_800
CASE_SYNTHESIS_TIMEOUT_SECONDS = 300
RESOURCE_HEARTBEAT_SECONDS = 15


async def _prepare_branch(task, services, *, name, planning, current, names, context,
                          working_bundle, editable, config, planner, case_synthesizer, library_reader,
                          guard, handle):
    """Fork from frozen Evidence and join before any Candidate write."""
    async def propose():
        result = await asyncio.wait_for(
            planner(name=name, sessions=planning, current=current, names=names, context=context,
                    config=config, library_reader=library_reader), timeout=PLANNER_TIMEOUT_SECONDS,
        )
        result = result or {"action": DecisionAction.SKIP, "rationale": "planner returned no proposal"}
        if result.get("action") == DecisionAction.SKIP or not result.get("skill"):
            return result, None, {}, {}
        proposed = dict(result["skill"])
        if current:
            proposed = {**current, **proposed, "name": name}
            if result["action"] == DecisionAction.OPTIMIZE_DESC:
                proposed["content"] = current["content"]
        material, diff = materialize_bundle_changes(
            proposed, current_bundle=working_bundle, file_changes=proposed.get("file_changes") or [],
            editable_paths=editable, extensions=config.get("bundle_text_extensions"),
            max_file_bytes=config.get("bundle_edit_max_bytes"), allow_delete=config.get("bundle_allow_delete"),
        )
        static = await asyncio.to_thread(validate_candidate_bundle, material)
        return result, material, diff, static

    # The planner may mutate its working context; synthesis gets the same
    # Evidence snapshot, without this round's proposal or classification.
    dataset_work = asyncio.create_task(asyncio.wait_for(
        case_synthesizer(
            skill_name=name, sessions=copy.deepcopy(planning), candidate_skill={},
            evidence_context=copy.deepcopy(context),
        ), timeout=CASE_SYNTHESIS_TIMEOUT_SECONDS,
    ))
    proposal_work = asyncio.create_task(propose())

    async def wait(work):
        while not work.done():
            watched = {work}
            if work is proposal_work and not dataset_work.done():
                watched.add(dataset_work)
            await asyncio.wait(watched, timeout=RESOURCE_HEARTBEAT_SECONDS, return_when=asyncio.FIRST_COMPLETED)
            if work.done():
                break
            if dataset_work.done() and not dataset_work.cancelled() and dataset_work.exception() is not None:
                raise dataset_work.exception()
            async with services.tx.tenant_tx(task.tenant_id) as conn:
                await services.tasks.require_current(conn, task)
                if not await guard.heartbeat(conn, handle, lease_seconds=600):
                    raise RuntimeError("evolution resource lease lost")
        return work.result()

    try:
        result, material, diff, static = await wait(proposal_work)
        if material is None:
            return result, material, diff, static, []
        candidate_cases = copy.deepcopy(await wait(dataset_work))
        # New Skill names only become available when planning finishes.
        for case in candidate_cases:
            case["skill_ids"] = [material["name"]]
        return result, material, diff, static, candidate_cases
    finally:
        for work in (proposal_work, dataset_work):
            if not work.done():
                work.cancel()
        await asyncio.gather(proposal_work, dataset_work, return_exceptions=True)


async def _inputs(task, services, payload, repo):
    """Freeze the input set once; resume uses exactly the same Session hashes."""
    async with services.tx.tenant_tx(task.tenant_id) as conn:
        saved = await repo.branch(conn, task.task_id, "__inputs__")
    if saved is not None:
        return saved["sessions"]
    selected, cursor = [], None
    limit = min(200, max(1, int(payload["config"]["evolution_max_sessions"])))
    ids = payload.get("session_ids") or None
    while len(selected) < limit:
        async with services.tx.tenant_tx(task.tenant_id) as conn:
            page = await SessionQueries(services.tx).evolution_page(
                conn, session_ids=ids, after=cursor, limit=200,
            )
            consumed = await repo.consumed(conn, page)
        if not page:
            break
        for row in page:
            if (row["session_id"], row["content_hash"]) in consumed:
                continue
            # A trigger only includes Sessions that existed at acceptance.
            if row["ingested_at"].isoformat() > payload["accepted_at"]:
                continue
            selected.append({
                k: v.isoformat() if hasattr(v, "isoformat") else v for k, v in row.items()
            })
            if len(selected) == limit:
                break
        cursor = page[-1]["ingested_at"], page[-1]["session_id"]
        if ids or len(page) < 200:
            break
    async with services.tx.tenant_tx(task.tenant_id) as conn:
        await services.tasks.require_current(conn, task)
        await repo.save_branch(
            conn, tenant_id=task.tenant_id, command_id=task.task_id, key="__inputs__",
            session_ids=[s["session_id"] for s in selected], result={"sessions": selected},
        )
    return selected


class LLMSkillPlanner:
    """Adapter to the existing LLM planner; no server, storage or lifecycle."""
    def __init__(self, llm):
        self.llm = llm

    async def __call__(self, *, name, sessions, current, names, context, config, library_reader):
        from team_skills.evolution.stages.execute import create_skill_from_sessions, evolve_skill_from_sessions

        if name == NO_SKILL_KEY:
            return await create_skill_from_sessions(
                self.llm, sessions, names, evolution_context=context, library_reader=library_reader,
            )
        return await evolve_skill_from_sessions(
            self.llm, name, sessions, current, names, evolution_context=context,
        )


async def run_cycle(task, services) -> HandlerResult:
    command = await load_command(task, services)
    if command["result"] is not None:
        return HandlerResult.succeed(task.task_id)
    p = command["payload"]
    config = SkillsModuleConfig(p["config_revision"], p["config"])
    planner = services.extras.get("skill_planner")
    case_synthesizer = services.extras.get("replay_case_synthesizer")
    payloads = services.extras.get("session_payloads")
    if planner is None or case_synthesizer is None or payloads is None:
        raise RuntimeError("skill_planner, replay_case_synthesizer and session_payloads collaborators are required")
    guard = LeaseGuard(services.leases)
    async with services.tx.tenant_tx(task.tenant_id) as conn:
        handle = await guard.acquire(
            conn, tenant_id=task.tenant_id, resource_key="skill.evolve",
            owner_id=f"{task.task_id}:{task.fence}", lease_seconds=600,
        )
    if handle is None:
        return HandlerResult.retry("evolution_busy")
    try:
        return await _cycle(task, services, p, config, planner, case_synthesizer, payloads, guard, handle)
    finally:
        async with services.tx.tenant_tx(task.tenant_id) as conn:
            await guard.release(conn, handle)


async def _cycle(task, services, p, config, planner, case_synthesizer, payloads, guard, handle):
    repo = EvolutionRepository(services.tx)
    library = SkillLibraryRepository(services.tx)
    cands = CandidateRepository(services.tx)
    descriptors = await _inputs(task, services, p, repo)
    sessions = await load_sessions(descriptors, payloads)
    async with services.tx.tenant_tx(task.tenant_id) as conn:
        names = [s["name"] for s in await library.list_skills(conn)]

    async def library_reader(name):
        async with services.tx.tenant_tx(task.tenant_id) as conn:
            _, bundle = await library.read_current_bundle(conn, name)
        return bundle["SKILL.md"].decode()

    records = []
    for name, group in groups(sessions).items():
        async with services.tx.tenant_tx(task.tenant_id) as conn:
            if not await guard.heartbeat(conn, handle, lease_seconds=600):
                raise RuntimeError("evolution resource lease lost")
            saved = await repo.branch(conn, task.task_id, name)
            historical = await repo.evidence(conn, name)
            head = await library.get_skill(conn, name)
            published = await library.read_version_bundle(conn, name, head["version"]) if head else {}
            live = await cands.live_for_skill(conn, name)
            working_bundle = await cands.read_bundle(conn, live["candidate_id"]) if live else published
            pattern_context, pattern_refs = compact_context(
                await ExperienceRepository(services.tx).patterns(conn, name),
            )
        if saved is not None:
            records.append(saved)
            continue
        current = skill_payload(name, working_bundle)
        current_ids = {s["session_id"] for s in group}
        planning = group + [s for s in historical if s["session_id"] not in current_ids]
        context = {
            "skill_name": name, "total_evidence_sessions": len(planning),
            "current_session_count": len(group), "active_candidate_job_id": live["candidate_id"] if live else "",
            "active_candidate_feedback": live["evaluation"] if live else {},
            "experience_patterns": pattern_context,
        }
        editable = select_editable_files(
            working_bundle, extensions=config.get("bundle_text_extensions"),
            max_file_bytes=config.get("bundle_edit_max_bytes"),
            max_prompt_bytes=config.get("bundle_prompt_max_bytes"),
        )
        if current:
            current["_editable_bundle_files"] = editable
        gap = evidence_gap(planning, config)
        if gap:
            result, material, diff, static, candidate_cases = (
                {"action": DecisionAction.SKIP, "rationale": gap}, None, {}, {}, [],
            )
        else:
            result, material, diff, static, candidate_cases = await _prepare_branch(
                task, services, name=name, planning=planning, current=current, names=names, context=context,
                working_bundle=working_bundle, editable=editable, config=config, planner=planner,
                case_synthesizer=case_synthesizer, library_reader=library_reader, guard=guard, handle=handle,
            )
        record = {
            "skill_name": name, "session_ids": [s["session_id"] for s in group],
            "action": "skip", "rationale": str(result.get("rationale") or ""),
        }
        async with services.tx.tenant_tx(task.tenant_id) as conn:
            await services.tasks.require_current(conn, task)
            if not await guard.heartbeat(conn, handle, lease_seconds=600):
                raise RuntimeError("evolution resource lease lost")
            await repo.record_evidence(conn, tenant_id=task.tenant_id, name=name, sessions=group)
            if material:
                # A human edit while the model worked invalidates this proposal.
                latest = await cands.live_for_skill(conn, material["name"])
                if (latest["revision"] if latest else 0) != (live["revision"] if live else 0):
                    raise CandidateStateError("candidate changed while the planner was running")
                candidate = await cands.create(
                    conn, tenant_id=task.tenant_id, skill_name=material["name"],
                    bundle=candidate_skill_bundle(material), limits=config.bundle_limits,
                    proposed_action="update" if head else "create", base_version=head["version"] if head else 0,
                    detail={
                        "rationale": record["rationale"], "session_ids": record["session_ids"],
                        "pattern_refs": pattern_refs,
                        "evidence_classification": result.get("evidence_classification") or {},
                        "evolution_context": context, "replay_cases": candidate_cases,
                        "edit_summary": material.get("edit_summary") or {},
                        "file_changes": material.get("file_changes") or [], "bundle_diff": diff,
                        "static_validation": static,
                    },
                )
                record.update(action="candidate_created", candidate_id=candidate["candidate_id"],
                              candidate_revision=candidate["revision"])
                if config.get("auto_evaluate"):
                    mode = "auto" if config.get("publish_mode") == "automatic" else "evaluate"
                    await submit_validation(
                        conn, services, tenant_id=task.tenant_id, candidate_id=candidate["candidate_id"],
                        command_id=f"evaluate:{candidate['candidate_id']}:r{candidate['revision']}",
                        config=config, actor="evolution", revision=candidate["revision"],
                        mode=mode,
                    )
            await repo.save_branch(
                conn, tenant_id=task.tenant_id, command_id=task.task_id, key=name,
                session_ids=record["session_ids"], result=record,
            )
        records.append(record)
    async with services.tx.tenant_tx(task.tenant_id) as conn:
        await services.tasks.require_current(conn, task)
        await repo.consume(conn, tenant_id=task.tenant_id, command_id=task.task_id, sessions=descriptors)
        await repo.finish(conn, task.task_id, {
            "status": "completed", "sessions": len(sessions), "evolutions": records,
            "candidates_queued": sum(r["action"] == "candidate_created" for r in records),
        })
    return HandlerResult.succeed(task.task_id)
