"""Resumable Skill Evolution, executed exclusively by the shared Worker."""
from __future__ import annotations

import asyncio
import copy

from session_ingestion.queries import SessionQueries
from team_skills.candidates.repository import CandidateRepository, CandidateStateError
from team_skills.candidates.static_checks import validate_candidate_bundle
from team_skills.config import SkillsModuleConfig
from team_skills.evolution.candidate_pipeline import load_command, submit_validation
from team_skills.evolution.history import evolution_feedback, skill_payload
from team_skills.evolution.kernel.bundle_changes import materialize_bundle_changes, select_editable_files
from team_skills.evolution.kernel.enums import NO_SKILL_KEY, DecisionAction
from team_skills.evolution.repository import EvolutionRepository, evidence_projection
from team_skills.evolution.session_processing import after_reset, evidence_gap, groups, load_sessions
from team_skills.experiences.patterns import compact_context
from team_skills.experiences.repository import ExperienceRepository
from team_skills.library.bundle import candidate_skill_bundle, encode_bundle_payload
from team_skills.library.repository import SkillLibraryRepository
from teamEvolver.runtime import HandlerResult
from teamEvolver.runtime.leases import LeaseGuard

PLANNER_TIMEOUT_SECONDS = 1_800
CASE_SYNTHESIS_TIMEOUT_SECONDS = 1_800
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
        target_bundle, target_editable, target_version, target_revision = working_bundle, editable, None, 0
        target_reset_at = None
        if not current and proposed.get("name") in names:
            # A create-group planner can select an existing library Skill.
            # Its complete Bundle remains the baseline, including runtime files.
            library = SkillLibraryRepository(services.tx)
            async with services.tx.tenant_tx(task.tenant_id) as conn:
                target_head = await library.get_skill(conn, proposed["name"])
                target_bundle = await library.read_version_bundle(conn, proposed["name"], target_head["version"])
                target_published = dict(target_bundle)
                candidates = CandidateRepository(services.tx)
                target_live = await candidates.live_for_skill(conn, proposed["name"])
                if target_live:
                    if target_live["status"] == "validating":
                        raise CandidateStateError("selected existing Skill Candidate is validating")
                    target_revision = target_live["revision"]
                    target_bundle = await candidates.read_bundle(conn, target_live["candidate_id"])
            target_version = target_head["version"]
            target_reset_at = target_head.get("evolution_reset_at")
            target_current = skill_payload(proposed["name"], target_bundle)
            proposed = {**target_current, **proposed}
            target_editable = select_editable_files(
                target_bundle, extensions=config.get("bundle_text_extensions"),
                max_file_bytes=config.get("bundle_edit_max_bytes"),
                max_prompt_bytes=config.get("bundle_prompt_max_bytes"),
            )
        material, diff = materialize_bundle_changes(
            proposed, current_bundle=target_bundle, file_changes=proposed.get("file_changes") or [],
            editable_paths=target_editable, extensions=config.get("bundle_text_extensions"),
            max_file_bytes=config.get("bundle_edit_max_bytes"), allow_delete=config.get("bundle_allow_delete"),
        )
        if target_version is not None:
            material["_target_base_version"] = target_version
            material["_target_candidate_revision"] = target_revision
            material["_target_reset_at"] = target_reset_at
            if target_head.get("backend") == "managed-agents":
                material["_baseline_bundle"] = encode_bundle_payload(target_published)
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
    reprocess = payload.get("reprocess") is True and bool(ids)
    while len(selected) < limit:
        async with services.tx.tenant_tx(task.tenant_id) as conn:
            page = await SessionQueries(services.tx).evolution_page(
                conn, session_ids=ids, after=cursor, limit=200,
            )
            consumed = await repo.consumed(conn, page)
        if not page:
            break
        for row in page:
            if not reprocess and (row["session_id"], row["content_hash"]) in consumed:
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
    manual_ids = set(p.get("session_ids") or []) if p.get("reprocess") is True else set()
    descriptors = await _inputs(task, services, p, repo)
    sessions = await load_sessions(descriptors, payloads)
    async with services.tx.tenant_tx(task.tenant_id) as conn:
        names = [s["name"] for s in await library.list_skills(conn)]

    async def library_reader(name):
        async with services.tx.tenant_tx(task.tenant_id) as conn:
            _, bundle = await library.read_current_bundle(conn, name)
        return bundle["SKILL.md"].decode()

    async def prepare_group(name, group):
        async with services.tx.tenant_tx(task.tenant_id) as conn:
            if not await guard.heartbeat(conn, handle, lease_seconds=600):
                raise RuntimeError("evolution resource lease lost")
            saved = await repo.branch(conn, task.task_id, name)
            historical = await repo.evidence(conn, name)
            head = await library.get_skill(conn, name)
            published = await library.read_version_bundle(conn, name, head["version"]) if head else {}
            live = await cands.live_for_skill(conn, name)
            if live and head and live["base_version"] != head["version"]:
                await cands.archive_for_skill(
                    conn, tenant_id=task.tenant_id, skill_name=name, actor="ma-cloud",
                    reason="Cloud Skill baseline changed; generate a fresh Candidate from the current cloud Bundle",
                )
                live = None
            working_bundle = await cands.read_bundle(conn, live["candidate_id"]) if live else published
            pattern_context, pattern_refs = compact_context(
                await ExperienceRepository(services.tx).patterns(conn, name),
            )
        boundary = head.get("evolution_reset_at") if head else None
        group = [s for s in group if s["session_id"] in manual_ids or after_reset(s, boundary)]
        fingerprints = getattr(services, "extras", {}).get("ma_automatic_fingerprints")
        if fingerprints:
            from team_replay.fingerprints import ineligibility_reason
            current_execution = await fingerprints(task.tenant_id)
            eligible_group, eligible_history = [], []
            async with services.tx.tenant_tx(task.tenant_id) as conn:
                for source, output in ((group, eligible_group), (historical, eligible_history)):
                    for item in source:
                        if source is group and item["session_id"] in manual_ids:
                            output.append(item)
                            continue
                        reason = ineligibility_reason(item, current_execution)
                        if reason:
                            await repo.archive_evidence(conn, name, item["session_id"], reason)
                        else:
                            output.append(item)
            group, historical = eligible_group, eligible_history
            async with services.tx.tenant_tx(task.tenant_id) as conn:
                pattern_context, pattern_refs = compact_context(
                    await ExperienceRepository(services.tx).patterns(conn, name),
                )
        if not group:
            return {"skill_name": name, "session_ids": [], "action": "skip",
                    "rationale": "Previous Skill Evidence archived after manual update"}
        if saved is not None:
            if boundary and saved.get("evolution_reset_at") != boundary.isoformat():
                return {"skill_name": name, "session_ids": [], "action": "skip",
                        "rationale": "Previous branch archived after manual update"}
            if saved.get("candidate_id"):
                async with services.tx.tenant_tx(task.tenant_id) as conn:
                    previous = await cands.require(conn, saved["candidate_id"])
                if previous["status"] == "archived":
                    return {"skill_name": name, "session_ids": [], "action": "skip",
                            "rationale": "Previous Candidate archived after manual update"}
            return saved
        current = skill_payload(name, working_bundle)
        current_ids = {s["session_id"] for s in group}
        planning = group + [s for s in historical if s["session_id"] not in current_ids]
        context = {
            "skill_name": name, "total_evidence_sessions": len(planning),
            "current_session_count": len(group), "active_candidate_job_id": live["candidate_id"] if live else "",
            "active_candidate_feedback": evolution_feedback(live["evaluation"]) if live else {},
            "experience_patterns": pattern_context,
        }
        manual = {
            "command_id": task.task_id, "actor": p.get("actor", ""),
            "session_ids": [s["session_id"] for s in group if s["session_id"] in manual_ids],
            "evidence": [evidence_projection(s) for s in group if s["session_id"] in manual_ids],
        } if manual_ids else {}
        if manual:
            context["manual_reprocess"] = {
                key: value for key, value in manual.items() if key != "evidence"
            }
        editable = select_editable_files(
            working_bundle, extensions=config.get("bundle_text_extensions"),
            max_file_bytes=config.get("bundle_edit_max_bytes"),
            max_prompt_bytes=config.get("bundle_prompt_max_bytes"),
        )
        if current:
            current["_editable_bundle_files"] = editable
        gap = ("Skill Candidate validation is running; retain Evidence for the next evolution"
               if live and live["status"] == "validating" else evidence_gap(planning, config))
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
            "evolution_reset_at": boundary.isoformat() if boundary else None,
        }
        if manual:
            record["manual_reprocess"] = context["manual_reprocess"]
        from team_skills.library.store import snapshot
        cloud = snapshot(services.tx)
        if cloud and cloud.managed(material["name"] if material else name):
            await cloud.provider.refresh(cloud, material["name"] if material else name)
        async with services.tx.tenant_tx(task.tenant_id) as conn:
            await services.tasks.require_current(conn, task)
            if not await guard.heartbeat(conn, handle, lease_seconds=600):
                raise RuntimeError("evolution resource lease lost")
            target_name = material["name"] if material else name
            latest_head = await library.lock_evolution_head(conn, target_name)
            expected_version = material.get("_target_base_version") if material else None
            if expected_version is None:
                expected_version = head["version"] if head else 0
            expected_reset = material.get("_target_reset_at", boundary) if material else boundary
            if ((latest_head["version"] if latest_head else 0) != expected_version
                    or (latest_head.get("evolution_reset_at") if latest_head else None) != expected_reset
                    or (material and any(s["session_id"] not in manual_ids
                                         and not after_reset(s, expected_reset) for s in planning))):
                record.update(action="skip", rationale="Skill changed while planning; previous proposal archived")
                material = None
            await repo.record_evidence(
                conn, tenant_id=task.tenant_id, name=name,
                sessions=[s for s in group if s["session_id"] not in manual_ids],
            )
            if material:
                if "_target_base_version" in material:
                    head = await library.get_skill(conn, material["name"])
                    if not head or head["version"] != material["_target_base_version"]:
                        raise CandidateStateError("published Skill changed while the planner was running")
                head = latest_head
                # A human edit while the model worked invalidates this proposal.
                latest = await cands.live_for_skill(conn, material["name"])
                expected_candidate = material.get("_target_candidate_revision", live["revision"] if live else 0)
                if (latest["revision"] if latest else 0) != expected_candidate:
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
                        **({"baseline_bundle": material.get("_baseline_bundle") or encode_bundle_payload(published),
                            "baseline_identity": {
                                "backend": "managed-agents", "skill_id": head["skill_id"],
                                "connection_id": head["connection_id"], "version": head["version"],
                            }} if head and head.get("backend") == "managed-agents" else {}),
                        **({"manual_reprocess": manual} if manual else {}),
                    },
                )
                record.update(action="candidate_created", candidate_id=candidate["candidate_id"],
                              candidate_revision=candidate["revision"])
                if config.get("auto_evaluate"):
                    mode = "auto" if config.get("publish_mode") == "automatic" else "evaluate"
                    try:
                        # Acceptance can fail on unavailable historical Replay
                        # inputs. Roll it back without discarding the proposal.
                        async with conn.transaction():
                            await submit_validation(
                                conn, services, tenant_id=task.tenant_id, candidate_id=candidate["candidate_id"],
                                command_id=f"evaluate:{candidate['candidate_id']}:r{candidate['revision']}",
                                config=config, actor="evolution", revision=candidate["revision"],
                                mode=mode,
                            )
                    except ValueError as exc:
                        await cands.record_validation_blocked(
                            conn, tenant_id=task.tenant_id, candidate_id=candidate["candidate_id"],
                            revision=candidate["revision"], reason=str(exc),
                        )
                        record["validation_acceptance"] = {"status": "blocked", "reason": str(exc)}
            await repo.save_branch(
                conn, tenant_id=task.tenant_id, command_id=task.task_id, key=name,
                session_ids=record["session_ids"], result=record,
            )
        return record

    slots = asyncio.Semaphore(min(8, max(1, int(config.get("evolution_parallel_groups")))))

    async def bounded_group(name, group):
        async with slots:
            return await prepare_group(name, group)

    pending = [asyncio.create_task(bounded_group(name, group)) for name, group in groups(sessions).items()]
    try:
        # Keep successful sibling checkpoints when one group fails. Cancellation
        # still stops every planner and synthesis branch before releasing leases.
        outcomes = await asyncio.gather(*pending, return_exceptions=True)
        records = []
        for outcome in outcomes:
            if isinstance(outcome, BaseException):
                raise outcome
            records.append(outcome)
    finally:
        for work in pending:
            if not work.done():
                work.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
    async with services.tx.tenant_tx(task.tenant_id) as conn:
        await services.tasks.require_current(conn, task)
        await repo.consume(conn, tenant_id=task.tenant_id, command_id=task.task_id, sessions=descriptors)
        await repo.finish(conn, task.task_id, {
            "status": "completed", "sessions": len(sessions), "evolutions": records,
            "candidates_queued": sum(r["action"] == "candidate_created" for r in records),
        })
    return HandlerResult.succeed(task.task_id)
