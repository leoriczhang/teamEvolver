"""Candidate command acceptance and the fenced central validation/publish flow."""
from __future__ import annotations

import asyncio

from team_replay._util import stable_hash
from team_replay.adapters.repository import ReplayAdapterRepository
from team_replay.config import load_replay_config
from team_replay.datasets.candidate_selection import freeze_candidate_cases
from team_replay.repository import ReplayRepository
from team_skills.candidates.repository import CandidateRepository, CandidateStateError
from team_skills.candidates.static_checks import validate_candidate_bundle
from team_skills.config import SkillsModuleConfig
from team_skills.evolution.history import skill_payload
from team_skills.evolution.repository import EvolutionRepository, digest, lock_key
from team_skills.experiences.repository import ExperienceRepository
from team_skills.library.bundle import decode_bundle_payload, encode_bundle_payload
from team_skills.library.repository import SkillLibraryError, SkillLibraryRepository
from teamEvolver.config_runtime import ConfigRepository
from teamEvolver.runtime import HandlerResult


async def enqueue_command(conn, services, *, tenant_id, command_id, kind, request, payload):
    repo = EvolutionRepository(services.tx)
    command, created = await repo.accept(
        conn, tenant_id=tenant_id, command_id=command_id, kind=kind, request=request, payload=payload,
    )
    if created:
        await services.tasks.enqueue(
            conn, tenant_id=tenant_id, task_id=command_id, kind=kind, subject_type="skill_command",
            subject_id=command_id, schema_version="1", dedupe_key=command_id,
            payload_ref=command_id, payload_hash=command["payload_hash"],
            config_revision=int(command["payload"]["config_revision"]),
        )
    return command, created


async def submit_validation(conn, services, *, tenant_id: str, candidate_id: str,
                            command_id: str, config, actor: str, revision: int | None,
                            mode: str = "evaluate") -> dict:
    if mode not in {"evaluate", "auto", "force"}:
        raise SkillLibraryError("mode must be evaluate, auto or force")
    if revision is not None and (type(revision) is not int or revision < 1):
        raise SkillLibraryError("candidate_revision must be a positive integer")
    repo = CandidateRepository(services.tx)
    item = await repo.require(conn, candidate_id, for_update=True)
    expected = item["revision"] if revision is None else revision
    request = {"candidate_id": candidate_id, "revision": expected, "mode": mode}
    await lock_key(conn, tenant_id, f"command:{command_id}")
    existing = await EvolutionRepository(services.tx).command(conn, command_id)
    if existing:
        if existing["request_hash"] != digest(request):
            raise CandidateStateError("Idempotency-Key was used for a different validation")
        return existing
    repo._check_revision(item, expected)
    if item["status"] != "open":
        raise CandidateStateError("candidate is already validating or processed")
    bundle = await repo.read_bundle(conn, candidate_id)
    baseline = {}
    if item["base_version"]:
        baseline = await SkillLibraryRepository(services.tx).read_version_bundle(
            conn, item["skill_name"], item["base_version"],
        )
    payload = {
        **request, "actor": actor, "config_revision": config.revision, "config": config.config,
        "bundle": encode_bundle_payload(bundle), "baseline": encode_bundle_payload(baseline) if baseline else None,
        "skill_name": item["skill_name"], "base_version": item["base_version"],
        "detail": item["detail"], "tree_sha256": item["tree_sha256"],
    }
    replay_config = await load_replay_config(
        services.extras.get("config_repo") or ConfigRepository(services.tx), conn,
    )
    adapter = await ReplayAdapterRepository(services.tx).binding(conn)
    payload["replay_config"] = {"revision": replay_config.revision, "config": replay_config.config}
    payload["replay_adapter"] = {
        key: adapter[key] for key in ("adapter_id", "revision", "source_hash")
    } if adapter else None
    payload["detail"] = await freeze_candidate_cases(
        conn, services.tx, tenant_id=tenant_id, candidate_id=candidate_id, skill_name=item["skill_name"],
        detail=item["detail"], seed=command_id,
    )
    command, _ = await enqueue_command(
        conn, services, tenant_id=tenant_id, command_id=command_id, kind="skill.validate",
        request=request, payload=payload,
    )
    await repo.transition(
        conn, tenant_id=tenant_id, candidate_id=candidate_id, to_status="validating",
        actor=actor, expected_revision=expected, validate_task_id=command_id,
    )
    return command


async def load_command(task, services) -> dict:
    async with services.tx.tenant_tx(task.tenant_id) as conn:
        command = await EvolutionRepository(services.tx).command(conn, task.payload_ref)
    if not command or command["kind"] != task.kind:
        raise ValueError("skill command not found or wrong kind")
    if digest(command["payload"]) != task.payload_hash or command["payload_hash"] != task.payload_hash:
        raise ValueError("skill command payload hash mismatch")
    return command


async def run_validation(task, services) -> HandlerResult:
    command = await load_command(task, services)
    if command["result"] is not None:
        return HandlerResult.succeed(task.payload_ref)
    p = command["payload"]
    repo = CandidateRepository(services.tx)
    history = EvolutionRepository(services.tx)
    async with services.tx.tenant_tx(task.tenant_id) as conn:
        item = await repo.require(conn, p["candidate_id"])
    if item["revision"] != p["revision"] or item["validate_task_id"] != task.task_id or item["status"] != "validating":
        async with services.tx.tenant_tx(task.tenant_id) as conn:
            await services.tasks.require_current(conn, task)
            await history.finish(conn, task.task_id, {"status": "stale"})
        return HandlerResult.succeed(task.task_id)
    bundle = decode_bundle_payload(p["bundle"])
    baseline = decode_bundle_payload(p["baseline"]) if p["baseline"] else {}
    candidate = skill_payload(p["skill_name"], bundle)
    candidate["file_changes"] = p["detail"].get("file_changes") or []
    static = await asyncio.to_thread(validate_candidate_bundle, candidate)
    replay = services.extras.get("skill_replay")
    job = {
        **p["detail"], "job_id": p["candidate_id"], "candidate_revision": p["revision"],
        "candidate_skill": candidate, "current_skill": skill_payload(p["skill_name"], baseline),
        "baseline_ref": {"revision": p["base_version"]}, "skill_ids": [p["skill_name"]],
        "replay_config": p.get("replay_config"), "replay_adapter": p.get("replay_adapter"),
    }
    replay_history = ReplayRepository(services.tx)
    replay_input = {
        "job": job, "baseline_hash": stable_hash(job["current_skill"]),
        "candidate_hash": p["tree_sha256"], "dataset_hash": stable_hash(job.get("replay_cases", [])),
    }
    async with services.tx.tenant_tx(task.tenant_id) as conn:
        await services.tasks.require_current(conn, task)
        await replay_history.accept(
            conn, tenant_id=task.tenant_id, domain="skill", run_id=task.task_id,
            subject_id=p["skill_name"], request={"validation_command": task.task_id}, payload=replay_input,
        )
    if replay is not None:
        evaluation = await replay(job, SkillsModuleConfig(p["config_revision"], p["config"]))
    else:
        evaluation = await evaluate_fixed_job(job, task, services)
    verdict = str(evaluation.get("verdict") or "inconclusive")
    if verdict not in {"accept", "reject", "inconclusive"}:
        raise ValueError("central validator returned an invalid verdict")
    runtime_ok = (evaluation.get("runtime_gate") or {}).get("status") == "passed"
    if not static["passed"]:
        verdict = "reject"
    elif not runtime_ok and verdict == "accept":
        verdict = "inconclusive"
    evaluation = {
        "status": "evaluated", "candidate_revision": p["revision"],
        "recommended_publish": verdict == "accept",
        "replay": {**evaluation, "verdict": verdict},
        "static_validation": static,
    }
    config = SkillsModuleConfig(p["config_revision"], p["config"])
    async with services.tx.tenant_tx(task.tenant_id) as conn:
        await services.tasks.require_current(conn, task)
        item = await repo.record_verdict(
            conn, tenant_id=task.tenant_id, candidate_id=p["candidate_id"], revision=p["revision"],
            verdict=verdict, evaluation=evaluation, task_id=task.task_id,
        )
        result = {"status": "stale"} if item is None else {"status": "evaluated", "evaluation": evaluation}
        if item is not None and p["mode"] != "evaluate":
            publish = static["passed"] and runtime_ok and (verdict == "accept" or p["mode"] == "force")
            if publish:
                mutation_id = f"candidate:{p['candidate_id']}:r{p['revision']}"
                commit = await SkillLibraryRepository(services.tx).commit_bundle(
                    conn, tenant_id=task.tenant_id, name=p["skill_name"], bundle=bundle,
                    mutation_id=mutation_id, limits=config.bundle_limits, actor=p["actor"],
                    source="candidate-review", base_version=p["base_version"],
                    metadata={"candidate_id": p["candidate_id"], "revision": p["revision"]},
                )
                result = {**commit.to_response(), "status": "published", "accepted": True, "mode": p["mode"]}
                await repo.transition(
                    conn, tenant_id=task.tenant_id, candidate_id=p["candidate_id"], to_status="published",
                    actor=p["actor"], expected_revision=p["revision"], decision=result,
                    published_version=commit.version, mutation_id=mutation_id,
                )
            elif verdict == "reject":
                result = {"status": "rejected", "accepted": False, "mode": p["mode"]}
                await repo.transition(
                    conn, tenant_id=task.tenant_id, candidate_id=p["candidate_id"], to_status="rejected",
                    actor=p["actor"], expected_revision=p["revision"], decision=result,
                )
            else:
                result = {"status": "inconclusive", "evaluation": evaluation}
        if item is not None:
            await ExperienceRepository(services.tx).candidate_outcome(
                conn, tenant_id=task.tenant_id, refs=p["detail"].get("pattern_refs") or [],
                outcome={"job_id": p["candidate_id"], "candidate_revision": p["revision"],
                         "status": result["status"], "version": result.get("version")},
            )
        await history.finish(conn, task.task_id, result)
        await replay_history.finish(conn, "skill", task.task_id, evaluation)
    return HandlerResult.succeed(task.task_id)


async def evaluate_fixed_job(job, task, services):
    """Production validator uses the fixed adapter and common Engine."""
    from team_replay.adapters.guard import GuardedFactory
    from team_replay.contracts import ReplayContractError
    from team_replay.gateway import HostReplayGateway
    from team_replay.integration import host_for

    from .validation import TrueReplayValidator

    slot = None
    owner = f"{task.owner_id}:{task.task_id}:{task.fence}"
    try:
        host = await host_for({
            "config": job["replay_config"]["config"], "adapter": job["replay_adapter"],
        }, services, task.tenant_id)
        factory = host.resolve_replay_factory("", {})
        while slot is None:
            async with services.tx.tenant_tx(task.tenant_id) as conn:
                await services.tasks.require_current(conn, task)
                for index in range(int(job["replay_config"]["config"].get("concurrency", 2))):
                    key = f"replay:slot:{index}"
                    fence = await services.leases.acquire(
                        conn, tenant_id=task.tenant_id, resource_key=key, owner_id=owner, lease_seconds=60,
                    )
                    if fence is not None:
                        slot = (key, fence)
                        break
            if slot is None:
                # Capacity waiting must not exhaust the transient-error retry cap.
                await asyncio.sleep(5)
        guarded = GuardedFactory(factory, task, services, slot, owner)
        class ValidationHost:
            def resolve_replay_factory(self, runtime_type, source_session):
                return guarded
            def judge_harness(self):
                return host.judge_harness()
        validator = TrueReplayValidator(HostReplayGateway(
            ValidationHost(), services.extras.get("replay_judge"), services.extras.get("replay_feedback"),
        ))
        repo = ReplayRepository(services.tx)
        async with services.tx.tenant_tx(task.tenant_id) as conn:
            cached = await repo.cases(conn, "skill", task.task_id)

        async def checkpoint(case, result):
            async with services.tx.tenant_tx(task.tenant_id) as conn:
                await services.tasks.require_current(conn, task)
                await repo.save_case(
                    conn, tenant_id=task.tenant_id, domain="skill", run_id=task.task_id,
                    case=case, result=result,
                )

        work = asyncio.create_task(validator(
            job, job["replay_config"]["config"], cached=cached, on_case=checkpoint,
        ))
        try:
            while not work.done():
                await asyncio.wait({work}, timeout=15)
                if work.done():
                    break
                async with services.tx.tenant_tx(task.tenant_id) as conn:
                    await services.tasks.require_current(conn, task)
                    if not await services.leases.heartbeat(
                        conn, tenant_id=task.tenant_id, resource_key=slot[0],
                        owner_id=owner, fence=slot[1], lease_seconds=60,
                    ):
                        raise RuntimeError("Candidate Replay resource lease lost")
            return work.result()
        finally:
            if not work.done():
                work.cancel()
                await asyncio.gather(work, return_exceptions=True)
    except (ValueError, ReplayContractError):
        return {"verdict": "inconclusive", "accepted": False, "reason": "Replay input or adapter unavailable",
                "runtime_gate": {"status": "inconclusive"}}
    finally:
        if slot is not None:
            async with services.tx.tenant_tx(task.tenant_id) as conn:
                await services.leases.release(
                    conn, tenant_id=task.tenant_id, resource_key=slot[0], owner_id=owner, fence=slot[1],
                )
