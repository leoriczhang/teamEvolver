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
from team_skills.evolution.assessment import METHODS, assess, assessment_gate
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
                            mode: str = "evaluate", validation_method: str = "auto") -> dict:
    if mode not in {"evaluate", "auto", "force"}:
        raise SkillLibraryError("mode must be evaluate, auto or force")
    if validation_method not in METHODS:
        raise SkillLibraryError("Unknown validation_method")
    if revision is not None and (type(revision) is not int or revision < 1):
        raise SkillLibraryError("candidate_revision must be a positive integer")
    repo = CandidateRepository(services.tx)
    preview = await repo.require(conn, candidate_id)
    await SkillLibraryRepository(services.tx).lock_evolution_head(conn, preview["skill_name"])
    item = await repo.require(conn, candidate_id, for_update=True)
    expected = item["revision"] if revision is None else revision
    requested_method = validation_method
    if mode != "evaluate" and validation_method == "auto":
        validation_method = (item.get("evaluation") or {}).get("replay", {}).get("validation_method", "auto")
    request = {"candidate_id": candidate_id, "revision": expected, "mode": mode}
    if requested_method != "auto":
        request["validation_method"] = requested_method
    await lock_key(conn, tenant_id, f"command:{command_id}")
    existing = await EvolutionRepository(services.tx).command(conn, command_id)
    if existing:
        if existing["request_hash"] != digest(request):
            raise CandidateStateError("Idempotency-Key was used for a different validation")
        return existing
    from team_skills.library.store import snapshot
    cloud = snapshot(services.tx)
    if cloud is not None and cloud.managed(item["skill_name"]):
        head = cloud.head(item["skill_name"])
        origin = item["detail"].get("baseline_identity") or {}
        if ((not head and (item["base_version"] or origin))
                or (head and (head["version"] != item["base_version"]
                              or origin.get("skill_id") != head["skill_id"]
                              or origin.get("connection_id") != head["connection_id"]))):
            raise CandidateStateError("Cloud Skill baseline changed; regenerate the proposal from its latest version")
    repo._check_revision(item, expected)
    if item["status"] != "open":
        raise CandidateStateError("candidate is already validating or processed")
    bundle = await repo.read_bundle(conn, candidate_id)
    baseline = {}
    if item["base_version"]:
        baseline = (decode_bundle_payload(item["detail"]["baseline_bundle"])
                    if item["detail"].get("baseline_bundle")
                    else await SkillLibraryRepository(services.tx).read_version_bundle(
                        conn, item["skill_name"], item["base_version"], local=True,
                    ))
    payload = {
        **request, "actor": actor, "config_revision": config.revision, "config": config.config,
        "bundle": encode_bundle_payload(bundle), "baseline": encode_bundle_payload(baseline) if baseline else None,
        "skill_name": item["skill_name"], "base_version": item["base_version"],
        "detail": item["detail"], "tree_sha256": item["tree_sha256"],
        "validation_method": validation_method,
        "connection_id": cloud.selected if cloud is not None else "",
        "evidence": await EvolutionRepository(services.tx).evidence(conn, item["skill_name"], limit=8),
    }
    manual = item["detail"].get("manual_reprocess") or {}
    if manual:
        source = await EvolutionRepository(services.tx).command(conn, manual.get("command_id", ""))
        if not source or source["kind"] != "skill.evolve" or source["payload"].get("reprocess") is not True:
            raise CandidateStateError("manual reprocess provenance is unavailable")
        allowed = set(source["payload"]["session_ids"]) & set(manual.get("session_ids") or [])
        explicit = [e for e in manual.get("evidence") or [] if e.get("session_id") in allowed]
        payload["evidence"] = explicit + [
            e for e in payload["evidence"] if e["session_id"] not in allowed
        ]
    replay_config = await load_replay_config(
        services.extras.get("config_repo") or ConfigRepository(services.tx), conn,
    )
    adapter = await ReplayAdapterRepository(services.tx).binding(conn)
    payload["replay_config"] = {"revision": replay_config.revision, "config": replay_config.config}
    payload["replay_adapter"] = {
        key: adapter[key] for key in ("adapter_id", "revision", "source_hash")
    } if adapter else None
    active = services.extras.get("ma_active_connections")
    active_connections = await active(tenant_id) if active else None
    fingerprints = services.extras.get("ma_automatic_fingerprints")
    current_execution = await fingerprints(tenant_id) if fingerprints else None
    payload["detail"] = await freeze_candidate_cases(
        conn, services.tx, tenant_id=tenant_id, candidate_id=candidate_id, skill_name=item["skill_name"],
        detail=item["detail"], seed=command_id, active_ma_connections=active_connections,
        execution_fingerprints=current_execution,
    )
    freeze_ma = services.extras.get("ma_freeze_replay")
    if freeze_ma:
        payload["ma_records"], payload["ma_errors"] = {}, {}
        for case in payload["detail"].get("replay_cases") or []:
            try:
                payload["ma_records"].update(await freeze_ma(tenant_id, [case], item["skill_name"]))
            except ValueError as exc:
                payload["ma_errors"][case["case_id"]] = str(exc)
        required_query = services.extras.get("ma_required_targets")
        required = await required_query(tenant_id, item["skill_name"]) if required_query else []
        payload["required_targets"] = required
        policy = dict(payload["detail"].get("runtime_validation_policy") or {})
        policy["required_targets"] = required
        payload["detail"]["runtime_validation_policy"] = policy
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
        "ma_records": p.get("ma_records") or {}, "required_targets": p.get("required_targets") or [],
        "ma_errors": p.get("ma_errors") or {},
    }
    # The injected validator is retained for embedders. Production strategy work
    # goes through the shared LLM client and is checkpointed before any execution.
    assessment = None
    if replay is None:
        async with services.tx.tenant_tx(task.tenant_id) as conn:
            assessment = await history.branch(conn, task.task_id, "assessment")
        if assessment is None:
            if p.get("validation_method") == "full_ab":
                assessment = {
                    "method": "full_ab", "verdict": "inconclusive", "reason": "用户显式选择完整 A/B",
                    "scope": "完整任务与 A/B 效率", "evidence_refs": [], "issues": [], "source": "requested",
                }
            else:
                from .model import model_for
                client = await model_for(services, task.tenant_id, p["config"].get("model", {}))
                assessment = await assess(
                    job, evidence=p.get("evidence") or [], requested_method=p.get("validation_method", "auto"),
                    client=client,
                )
            async with services.tx.tenant_tx(task.tenant_id) as conn:
                await services.tasks.require_current(conn, task)
                await history.save_branch(conn, tenant_id=task.tenant_id, command_id=task.task_id,
                                          key="assessment", session_ids=[], result=assessment)
        job.update(validation_method=assessment["method"], validation_scope=assessment["scope"],
                   assessment=assessment)
        if assessment["method"] == "candidate_execution":
            cases = [c for c in job["replay_cases"] if c["case_id"] == assessment["case_id"]]
            selected = dict(cases[0])
            selected["checks"] = [r for r in selected["checks"] if r["id"] in assessment["check_ids"]]
            job["validation_scope_kind"] = (
                "complete_task" if len(selected["checks"]) == len(cases[0]["checks"]) else "stage"
            )
            if job["validation_scope_kind"] == "stage":
                job["execution_timeout_seconds"] = 300
            job["replay_cases"] = [selected]
            job["required_targets"] = []
            job["runtime_validation_policy"] = {}
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
    if assessment and (assessment["method"] == "experience_review" or assessment["verdict"] == "reject"):
        evaluation = {
            "verdict": assessment["verdict"], "validation_method": assessment["method"],
            "validation_scope": assessment["scope"], "assessment": assessment, "reason": assessment["reason"],
            "runtime_gate": {"status": "not_required", "reason": "Evidence review does not assert Runtime execution"},
            "basic_validation": {"status": "passed" if assessment["verdict"] == "accept" else assessment["verdict"]},
            "ab_verdict": "not_run", "candidate_completed": 0,
            "validation_targets": p.get("required_targets") or [],
        }
    elif replay is not None:
        evaluation = await replay(job, SkillsModuleConfig(p["config_revision"], p["config"]))
    else:
        evaluation = await evaluate_fixed_job(job, task, services)
    verdict = str(evaluation.get("verdict") or "inconclusive")
    if verdict not in {"accept", "reject", "inconclusive"}:
        raise ValueError("central validator returned an invalid verdict")
    runtime_ok = assessment_gate(evaluation)
    if not static["passed"]:
        verdict = "reject"
    elif not runtime_ok and verdict == "accept":
        verdict = "inconclusive"
    evaluation = {
        "status": "evaluated", "candidate_revision": p["revision"],
        "recommended_publish": verdict == "accept",
        "replay": {**evaluation, "verdict": verdict},
        "static_validation": static,
        "validation_method": evaluation.get("validation_method", "full_ab"),
    }
    config = SkillsModuleConfig(p["config_revision"], p["config"])
    async with services.tx.tenant_tx(task.tenant_id) as conn:
        await services.tasks.require_current(conn, task)
        await SkillLibraryRepository(services.tx).lock_evolution_head(conn, p["skill_name"])
        item = await repo.record_verdict(
            conn, tenant_id=task.tenant_id, candidate_id=p["candidate_id"], revision=p["revision"],
            verdict=verdict, evaluation=evaluation, task_id=task.task_id,
        )
        result = {"status": "stale"} if item is None else {"status": "evaluated", "evaluation": evaluation}
        if item is not None and p["mode"] != "evaluate":
            publish = static["passed"] and runtime_ok and (verdict == "accept" or p["mode"] == "force")
            if publish:
                mutation_id = f"candidate:{p['candidate_id']}:r{p['revision']}:{task.task_id}"
                commit = await SkillLibraryRepository(services.tx).commit_bundle(
                    conn, tenant_id=task.tenant_id, name=p["skill_name"], bundle=bundle,
                    mutation_id=mutation_id, limits=config.bundle_limits, actor=p["actor"],
                    source="candidate-review", base_version=p["base_version"],
                    metadata={"candidate_id": p["candidate_id"], "revision": p["revision"]},
                )
                result = {**commit.to_response(), "status": commit.status, "accepted": True, "mode": p["mode"]}
                await repo.transition(
                    conn, tenant_id=task.tenant_id, candidate_id=p["candidate_id"],
                    to_status="validating" if commit.status == "accepted" else "published",
                    actor=p["actor"], expected_revision=p["revision"], decision=result,
                    published_version=commit.version if commit.status != "accepted" else None,
                    validate_task_id=result.get("job_id", task.task_id), mutation_id=mutation_id,
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

    if not job.get("replay_cases"):
        return {"verdict": "inconclusive", "reason": "No eligible Case for execution",
                "validation_method": job.get("validation_method", "full_ab")}
    failures = [job["ma_errors"][c["case_id"]] for c in job["replay_cases"] if c["case_id"] in job.get("ma_errors", {})]
    if failures:
        return {"verdict": "inconclusive", "reason": "; ".join(failures),
                "validation_method": job.get("validation_method", "full_ab"),
                "basic_validation": {"status": "blocked"}, "candidate_completed": 0}
    slot = None
    owner = f"{task.owner_id}:{task.task_id}:{task.fence}"
    try:
        host = await host_for({
            "config": job["replay_config"]["config"], "adapter": job["replay_adapter"],
            "ma_records": job.get("ma_records") or {},
        }, services, task.tenant_id, task=task)
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
            cached = {**(await repo.reusable_candidate_cases(conn, job)), **cached}

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
