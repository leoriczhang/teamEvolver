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
from team_skills.config import SkillsModuleConfig, replay_case_counts
from team_skills.evolution.history import skill_payload
from team_skills.evolution.repository import EvolutionRepository, digest, lock_key
from team_skills.execution.candidates import publication_gate, reconcile_scope
from team_skills.execution.cost_history import historical_cost_output
from team_skills.execution.costs import budget_gate
from team_skills.execution.holdout import enhanced_validation
from team_skills.execution.probes import run_probes
from team_skills.execution.publication import publish_candidate
from team_skills.execution.reconciliation import historical_zero_receipts
from team_skills.execution.reviewed_publication import VALIDATION_CONTRACT, candidate_snapshot, publication_payload
from team_skills.execution.validation import apply_peer_treatments, execution_gate, freeze_execution_validation
from team_skills.experiences.repository import ExperienceRepository
from team_skills.library.bundle import bundle_tree_sha256, decode_bundle_payload, encode_bundle_payload
from team_skills.library.repository import SkillLibraryError, SkillLibraryRepository
from teamEvolver.config_runtime import ConfigRepository
from teamEvolver.runtime import HandlerResult


async def enqueue_command(conn, services, *, tenant_id, command_id, kind, request, payload):
    try:
        replay_case_counts(payload.get("config", {}))
    except ValueError as exc:
        raise SkillLibraryError(str(exc)) from exc
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
    try:
        counts = replay_case_counts(config.config)
    except ValueError as exc:
        raise SkillLibraryError(str(exc)) from exc
    repo._check_revision(item, expected)
    if item["status"] != "open":
        raise CandidateStateError("candidate is already validating or processed")
    if mode != "evaluate" and item["detail"].get("candidate_execution_plan"):
        payload = await publication_payload(
            conn, services.tx, tenant_id=tenant_id, item=item, request=request, actor=actor,
        )
        command, _ = await enqueue_command(
            conn, services, tenant_id=tenant_id, command_id=command_id, kind="skill.evolve",
            request=request, payload=payload,
        )
        return command
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
        "candidate_kind": item.get("candidate_kind", "skill"),
        "detail": reconcile_scope(item["detail"], skill_changed=bool(item["skill_name"]) and (
            not baseline or bundle_tree_sha256(bundle) != bundle_tree_sha256(baseline))),
        "tree_sha256": item["tree_sha256"],
    }
    if payload["detail"].get("candidate_execution_plan"):
        payload["execution_candidate_hash"] = candidate_snapshot(item)
        # A new stochastic run cannot inherit the preceding run's independent decision.
        payload["detail"].pop("independent_validation", None)
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
        detail=payload["detail"], seed=f"{candidate_id}:r{expected}",
        historical_count=counts["historical"],
    )
    payload["detail"] = await freeze_execution_validation(
        conn, services.tx, payload["detail"], tenant_id=tenant_id,
        candidate_id=candidate_id, candidate_revision=expected,
    )
    if payload["detail"].get("candidate_execution_plan"):
        payload["detail"]["validation_plan"]["input_hash"] = digest({
            "validation_strategy": payload["detail"]["validation_plan"]["validation_strategy"],
            "cases": payload["detail"]["replay_cases"],
            "baseline": payload["detail"]["baseline_execution_plan"],
            "candidate": payload["detail"]["candidate_execution_plan"],
            "skill_dependencies": payload["detail"]["skill_dependencies"], "skill_tree": payload["tree_sha256"],
            "replay_adapter": payload["replay_adapter"], "replay_config": payload["replay_config"],
        })
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
    if candidate is not None and p["detail"].get("file_changes"):
        candidate["file_changes"] = p["detail"]["file_changes"]
    static = ({"passed": True, "scope": "execution", "checks": []} if p.get("candidate_kind") == "execution"
              else await asyncio.to_thread(validate_candidate_bundle, candidate))
    replay = services.extras.get("skill_replay")
    job = {
        **p["detail"], "job_id": p["candidate_id"], "candidate_revision": p["revision"],
        "candidate_skill": candidate, "current_skill": skill_payload(p["skill_name"], baseline),
        "baseline_ref": {"revision": p["base_version"]},
        "skill_ids": [p["skill_name"]] if p["skill_name"] else [],
        "replay_config": p.get("replay_config"), "replay_adapter": p.get("replay_adapter"),
    }
    job = apply_peer_treatments(job, p["detail"], p["skill_name"])
    replay_history = ReplayRepository(services.tx)
    replay_input = {
        "job": job, "baseline_hash": stable_hash(job["current_skill"]),
        "candidate_hash": p["tree_sha256"], "dataset_hash": stable_hash(job.get("replay_cases", [])),
    }
    async with services.tx.tenant_tx(task.tenant_id) as conn:
        await services.tasks.require_current(conn, task)
        await replay_history.accept(
            conn, tenant_id=task.tenant_id, domain="skill", run_id=task.task_id,
            subject_id=p["skill_name"] or p["detail"]["execution_profile_id"],
            request={"validation_command": task.task_id}, payload=replay_input,
        )
    if replay is not None:
        evaluation = await replay(job, SkillsModuleConfig(p["config_revision"], p["config"]))
    else:
        evaluation = await evaluate_fixed_job(job, task, services)
    if p["detail"].get("candidate_execution_plan"):
        if enhanced_validation(p["detail"]):
            async def evaluate_probe(variant):
                variant["diagnostic_run_id"] = task.task_id + ":" + variant["diagnostic_kind"]
                if replay is not None:
                    return await replay(variant, SkillsModuleConfig(p["config_revision"], p["config"]))
                return await evaluate_fixed_job(variant, task, services)
            evaluation["probe_gate"] = await run_probes(job, evaluation, evaluate_probe)
        else:
            evaluation["probe_gate"] = {"status": "not_required"}
        past, zero_receipts = [], []
        async with services.tx.tenant_tx(task.tenant_id) as conn:
            runs = await history.validation_cost_runs(conn, p["candidate_id"], excluding=task.task_id)
            for previous in runs[:100]:
                from team_replay.datasets.repository import DatasetNotFound
                try:
                    saved = await replay_history.get(conn, "skill", previous["command_id"])
                    output = await historical_cost_output(conn, replay_history, saved)
                    past.append({**output.get("replay", output),
                                 "generation_cost": previous["payload"]["detail"].get("generation_cost"),
                                 "_cost_context": {"tenant_id": task.tenant_id, "candidate_id": p["candidate_id"],
                                                   "run_id": saved["run_id"], "input_hash": saved["input_hash"],
                                                   "result_hash": stable_hash(saved["result"] or {})}})
                    zero_receipts.extend(await historical_zero_receipts(
                        conn, services.tx, candidate_id=p["candidate_id"], run_id=saved["run_id"],
                    ))
                except DatasetNotFound:
                    past.append({})
            if len(runs) > 100:
                past.append({})  # A bounded scan must not silently discard cost.
        evaluation["cost_gate"] = budget_gate(
            p["detail"], evaluation, worker_attempt=task.attempt, prior_evaluations=past,
            zero_dispatch_receipts=zero_receipts,
        )
    verdict = str(evaluation.get("verdict") or "inconclusive")
    validation_verdict = verdict
    if verdict not in {"accept", "reject", "inconclusive"}:
        raise ValueError("central validator returned an invalid verdict")
    runtime_ok = (evaluation.get("runtime_gate") or {}).get("status") == "passed"
    observed_gate = execution_gate(evaluation, p["detail"])
    evaluation["execution_gate"] = observed_gate
    additional_gate = publication_gate(p["detail"], evaluation, candidate_revision=p["revision"])
    if not static["passed"]:
        verdict = "reject"
    elif not runtime_ok and verdict == "accept":
        verdict = "inconclusive"
    elif not additional_gate["passed"] and verdict == "accept":
        verdict = "inconclusive"
    evaluation = {
        "status": "evaluated", "candidate_revision": p["revision"],
        "recommended_publish": verdict == "accept",
        "replay": {**evaluation, "verdict": verdict},
        "static_validation": static,
        "execution_gate": observed_gate, "publication_gate": additional_gate,
        **({"cost_gate": evaluation["cost_gate"]} if "cost_gate" in evaluation else {}),
        **({"validation_contract": VALIDATION_CONTRACT, "validation_run_id": task.task_id,
            "validation_verdict": validation_verdict} if p["detail"].get("candidate_execution_plan") else {}),
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
            legacy_force = p["mode"] == "force" and not p["detail"].get("candidate_execution_plan")
            publish = (static["passed"] and runtime_ok and additional_gate["passed"]
                       and (verdict == "accept" or legacy_force))
            if publish:
                mutation_id = f"candidate:{p['candidate_id']}:r{p['revision']}"
                if p["detail"].get("candidate_execution_plan"):
                    published = await publish_candidate(
                        conn, services.tx, tenant_id=task.tenant_id, payload={**p, "command_id": task.task_id},
                        bundle=bundle, baseline=baseline, config=config,
                    )
                    published_version = published.get("version")
                else:
                    commit = await SkillLibraryRepository(services.tx).commit_bundle(
                        conn, tenant_id=task.tenant_id, name=p["skill_name"], bundle=bundle,
                        mutation_id=mutation_id, limits=config.bundle_limits, actor=p["actor"],
                        source="candidate-review", base_version=p["base_version"],
                        metadata={"candidate_id": p["candidate_id"], "revision": p["revision"]},
                    )
                    published, published_version = commit.to_response(), commit.version
                result = {**published, "status": "published", "accepted": True, "mode": p["mode"]}
                await repo.transition(
                    conn, tenant_id=task.tenant_id, candidate_id=p["candidate_id"], to_status="published",
                    actor=p["actor"], expected_revision=p["revision"], decision=result,
                    published_version=published_version, mutation_id=mutation_id,
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
    run_id = job.get("diagnostic_run_id") or task.task_id
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
            def evaluate_external(self, request):
                return host.evaluate_external(request)
        validator = TrueReplayValidator(HostReplayGateway(
            ValidationHost(), services.extras.get("replay_judge"), services.extras.get("replay_feedback"),
        ))
        repo = ReplayRepository(services.tx)
        async with services.tx.tenant_tx(task.tenant_id) as conn:
            if run_id != task.task_id:
                await repo.accept(
                    conn, tenant_id=task.tenant_id, domain="skill", run_id=run_id,
                    subject_id=str(job.get("skill_ids") or []), request={"diagnostic": run_id},
                    payload={"job": job},
                )
            cached = await repo.cases(conn, "skill", run_id)

        async def checkpoint(case, result):
            async with services.tx.tenant_tx(task.tenant_id) as conn:
                await services.tasks.require_current(conn, task)
                await repo.save_case(
                    conn, tenant_id=task.tenant_id, domain="skill", run_id=run_id,
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
            result = work.result()
            if run_id != task.task_id:
                async with services.tx.tenant_tx(task.tenant_id) as conn:
                    await services.tasks.require_current(conn, task)
                    await repo.finish(conn, "skill", run_id, result)
            return result
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
