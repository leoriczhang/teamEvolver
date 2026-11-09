"""Publish an immutable completed Replay under its frozen validation strategy."""
from __future__ import annotations

import copy

from team_replay._util import stable_hash
from team_replay.adapters.repository import ReplayAdapterRepository
from team_replay.config import load_replay_config
from team_replay.datasets.repository import DatasetError
from team_replay.execution_plan import normalize_execution_plan
from team_replay.repository import ReplayRepository
from team_skills.candidates.repository import CandidateRepository, CandidateStateError
from team_skills.config import DEFAULT_SKILLS_CONFIG, SkillsModuleConfig
from team_skills.evolution.repository import EvolutionRepository, digest
from team_skills.experiences.repository import ExperienceRepository
from team_skills.library.bundle import bundle_tree_sha256, decode_bundle_payload
from team_skills.library.repository import SkillLibraryError, SkillLibraryRepository
from teamEvolver.config_runtime import ConfigRepository
from teamEvolver.runtime import HandlerResult

from .candidates import publication_gate
from .publication import publish_candidate
from .repository import ExecutionRepository

VALIDATION_CONTRACT = "teamevolver.reviewed-execution.v1"
REVIEW_CONTRACT = "teamevolver.independent-validation.v2"


def candidate_snapshot(item):
    """Asset identity excludes the later declaration, but includes the explicit cost budget."""
    detail = copy.deepcopy(item["detail"])
    detail.pop("independent_validation", None)
    return digest({**{key: item.get(key) for key in (
        "candidate_id", "candidate_kind", "execution_profile_id", "revision", "skill_name", "base_version",
        "tree_sha256", "proposed_action",
    )}, "detail": detail})


async def completed_validation(conn, tx, item):
    """Read both public histories and reject legacy, stale or inconsistent run evidence."""
    run_id = item.get("validate_task_id")
    command = await EvolutionRepository(tx).command(conn, run_id) if run_id else None
    if not command or command["kind"] != "skill.validate":
        raise CandidateStateError("evaluate this execution candidate before independent verification or publication")
    payload = command["payload"]
    result = command["result"] or {}
    evaluation = result.get("evaluation") or {}
    if (command["payload_hash"] != digest(payload) or payload.get("candidate_id") != item["candidate_id"]
            or payload.get("revision") != item["revision"]
            or payload.get("execution_candidate_hash") != candidate_snapshot(item)
            or result.get("status") != "evaluated" or not command.get("finished_at")
            or evaluation.get("validation_contract") != VALIDATION_CONTRACT
            or evaluation.get("validation_run_id") != run_id
            or evaluation.get("candidate_revision") != item["revision"]
            or evaluation.get("validation_verdict") not in {"accept", "reject", "inconclusive"}
            or item["evaluation"] != evaluation):
        raise CandidateStateError("execution validation lacks current reviewed-result evidence; evaluate again")
    try:
        replay = await ReplayRepository(tx).get(conn, "skill", run_id)
    except DatasetError as exc:
        raise CandidateStateError("completed execution Replay evidence is unavailable") from exc
    job = replay["input"].get("job") or {}
    frozen = payload.get("detail") or {}
    if (not replay.get("finished_at") or replay.get("result") != evaluation
            or job.get("job_id") != item["candidate_id"] or job.get("candidate_revision") != item["revision"]
            or any(job.get(key) != frozen.get(key) for key in (
                "baseline_execution_plan", "candidate_execution_plan", "validation_plan", "replay_cases",
                "skill_dependencies", "validation_budget",
            ))):
        raise CandidateStateError("execution Replay does not match the frozen validation command")
    return {"command": command, "payload": payload, "evaluation": evaluation,
            "validation_run_id": run_id, "validation_payload_hash": command["payload_hash"],
            "validation_result_hash": stable_hash(evaluation)}


def reviewed_gate(item, saved):
    detail = copy.deepcopy(saved["payload"]["detail"])
    detail["independent_validation"] = copy.deepcopy(item["detail"].get("independent_validation") or {})
    evaluation = saved["evaluation"]
    replay = evaluation.get("replay") or {}
    gate = publication_gate(detail, replay, candidate_revision=item["revision"], **{
        key: saved[key] for key in ("validation_run_id", "validation_payload_hash", "validation_result_hash")
    })
    reasons = list(gate["reasons"])
    if evaluation.get("validation_verdict") != "accept":
        reasons.append("reviewed_validation_not_accepted")
    if not (evaluation.get("static_validation") or {}).get("passed"):
        reasons.append("static_validation_failed")
    if (replay.get("runtime_gate") or {}).get("status") != "passed":
        reasons.append("runtime_validation_incomplete")
    return detail, {"passed": not reasons, "reasons": reasons}


def _composition(plan):
    value = normalize_execution_plan(plan)
    value["target_component_ids"] = []
    value["binding"].pop("allow_skill_change", None)
    return value


async def require_current_environment(conn, tx, tenant_id, saved):
    """Public domain queries hold row/transaction locks until the release is committed."""
    payload, frozen = saved["payload"], saved["payload"]["detail"]
    configs = ConfigRepository(tx)
    skills = await configs.read_tenant_module(conn, "skills", for_update=True)
    await configs.read_tenant_module(conn, "replay", for_update=True)
    replay = await load_replay_config(configs, conn)
    if (skills.revision != payload["config_revision"]
            or {**DEFAULT_SKILLS_CONFIG, **skills.config} != payload["config"]
            or {"revision": replay.revision, "config": replay.config} != payload["replay_config"]):
        raise CandidateStateError("validation configuration changed after the reviewed Replay")
    adapter = await ReplayAdapterRepository(tx).binding(conn, for_update=True, tenant_id=tenant_id)
    binding = {key: adapter[key] for key in ("adapter_id", "revision", "source_hash")} if adapter else None
    if binding != payload.get("replay_adapter"):
        raise CandidateStateError("Replay adapter binding changed after the reviewed Replay")
    repository = ExecutionRepository(tx)
    profile = await repository.profile(conn, frozen["execution_profile_id"], for_update=True)
    release = await repository.release(conn, profile["current_release_id"])
    baseline = frozen["baseline_execution_plan"]
    if (profile["current_release_id"] != frozen["base_release_id"]
            or profile["environment"] != baseline["environment"]
            or any(profile[key] != baseline["binding"].get(key)
                   for key in ("profile_id", "runtime_type", "agent_id", "user_id"))
            or profile["affected_skill_ids"] != frozen["affected_skill_ids"]
            or _composition(release["document"]["execution_plan"]) != _composition(baseline)
            or release["document"]["skill_dependencies"] != frozen["skill_dependencies"]):
        raise CandidateStateError("execution profile or baseline composition changed after the reviewed Replay")
    await SkillLibraryRepository(tx).require_versions(conn, frozen["skill_dependencies"])


async def publication_payload(conn, tx, *, tenant_id, item, request, actor):
    saved = await completed_validation(conn, tx, item)
    _, gate = reviewed_gate(item, saved)
    if not gate["passed"]:
        raise CandidateStateError("reviewed execution publication blocked: " + ", ".join(gate["reasons"]))
    await require_current_environment(conn, tx, tenant_id, saved)
    events = await CandidateRepository(tx).events(conn, item["candidate_id"])
    return {**request, "operation": "execution_publish_reviewed", "actor": actor,
            "config_revision": saved["payload"]["config_revision"], "config": saved["payload"]["config"],
            "validation_run_id": saved["validation_run_id"],
            "validation_payload_hash": saved["validation_payload_hash"],
            "validation_result_hash": saved["validation_result_hash"],
            "candidate_state_hash": CandidateRepository.cost_state_hash(item),
            "candidate_event_seq": events[-1]["seq"] if events else 0}


async def publish_reviewed_command(task, services, payload):
    """Fenced atomic publication of the reviewed run; no Replay, judge or LLM calls."""
    tx = services.tx
    repository, history = CandidateRepository(tx), EvolutionRepository(tx)
    async with tx.tenant_tx(task.tenant_id) as conn:
        await services.tasks.require_current(conn, task)
        try:
            # On any last-minute conflict, roll back all asset writes before recording the blocked job.
            async with conn.transaction():
                item = await repository.require(conn, payload["candidate_id"], for_update=True)
                events = await repository.events(conn, item["candidate_id"])
                if (item["status"] != "open" or item["revision"] != payload["revision"]
                        or repository.cost_state_hash(item) != payload["candidate_state_hash"]
                        or (events[-1]["seq"] if events else 0) != payload["candidate_event_seq"]):
                    raise CandidateStateError("candidate or review history changed after publication was accepted")
                saved = await completed_validation(conn, tx, item)
                if any(saved[key] != payload[key] for key in (
                    "validation_run_id", "validation_payload_hash", "validation_result_hash",
                )):
                    raise CandidateStateError("reviewed validation result changed after publication was accepted")
                detail, gate = reviewed_gate(item, saved)
                if not gate["passed"]:
                    raise CandidateStateError("reviewed execution publication blocked: " + ", ".join(gate["reasons"]))
                await require_current_environment(conn, tx, task.tenant_id, saved)
                frozen = saved["payload"]
                bundle = await repository.read_bundle(conn, item["candidate_id"])
                if bundle_tree_sha256(bundle) != frozen["tree_sha256"]:
                    raise CandidateStateError("candidate Skill bytes changed after the reviewed Replay")
                await services.tasks.require_current(conn, task)
                published = await publish_candidate(
                    conn, tx, tenant_id=task.tenant_id,
                    payload={**frozen, "detail": detail, "actor": payload["actor"],
                             "command_id": saved["validation_run_id"]},
                    bundle=bundle, baseline=decode_bundle_payload(frozen["baseline"]) if frozen["baseline"] else {},
                    config=SkillsModuleConfig(frozen["config_revision"], frozen["config"]),
                )
                result = {**published, "status": "published", "accepted": True, "mode": payload["mode"],
                          "validation_run_id": saved["validation_run_id"],
                          "validation_result_hash": saved["validation_result_hash"], "publication_gate": gate}
                await repository.transition(
                    conn, tenant_id=task.tenant_id, candidate_id=item["candidate_id"], to_status="published",
                    actor=payload["actor"], expected_revision=item["revision"], verdict="accept", decision=result,
                    published_version=published.get("version"), mutation_id=published["release_id"],
                )
                await ExperienceRepository(tx).candidate_outcome(
                    conn, tenant_id=task.tenant_id, refs=detail.get("pattern_refs") or [],
                    outcome={"job_id": item["candidate_id"], "candidate_revision": item["revision"],
                             "status": "published", "version": published.get("version")},
                )
        except SkillLibraryError as exc:
            result = {"status": "blocked", "accepted": False, "reason": str(exc),
                      "validation_run_id": payload["validation_run_id"]}
        await services.tasks.require_current(conn, task)
        await history.finish(conn, task.task_id, result)
    return HandlerResult.succeed(task.task_id)
