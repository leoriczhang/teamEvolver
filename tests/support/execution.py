"""Small business execution-profile factories, without services or credentials."""


def profile_raw(profile_id="instructor", *, prefix="", user_id="user-1"):
    return {
        "profile_id": profile_id, "runtime_type": "observed_http", "agent_id": "agent-1", "user_id": user_id,
        "affected_skill_ids": ["demo", "helper"],
        "components": [
            {"component_id": prefix + "main-prompt", "kind": "agent_prompt", "slot": "system", "order": 0,
             "content": "Original business Prompt"},
            {"component_id": prefix + "shared-context", "kind": "context_fragment", "slot": "context", "order": 1,
             "content": "Keep the shared business policy"},
        ],
        "environment": {"request": {"model": "test-model", "tools": [], "temperature": 0}},
    }


def component_change(content="Improved business Prompt", *, component_id="main-prompt"):
    return {"component_id": component_id, "content": content, "reason": "Resolve a recorded failure",
            "evidence_refs": ["session:test/turn:1"]}


async def seed_reviewed_execution(conn, tx, *, tenant_id="default", run_id="validate:persistence-fixture"):
    """Persist a completed synthetic run for publication contracts; never execute an Agent or model."""
    import copy

    from team_replay.config import load_replay_config
    from team_replay.repository import ReplayRepository
    from team_skills.candidates.repository import CandidateRepository
    from team_skills.config import BundleLimits, load_skills_config
    from team_skills.evolution.repository import EvolutionRepository, digest
    from team_skills.execution.evolution import attach_execution_proposal
    from team_skills.execution.repository import ExecutionRepository
    from team_skills.execution.reviewed_publication import VALIDATION_CONTRACT, candidate_snapshot
    from team_skills.library.bundle import encode_bundle_payload
    from teamEvolver.config_runtime import ConfigRepository

    raw = profile_raw()
    raw["affected_skill_ids"] = []
    _, baseline = await ExecutionRepository(tx).register(
        conn, tenant_id=tenant_id, raw=raw, skill_dependencies=[], actor="persistence-fixture",
    )
    context = {"candidate_kind": "execution", "execution_profile_id": raw["profile_id"],
               "base_release_id": baseline["release_id"],
               "baseline_execution_plan": baseline["document"]["execution_plan"],
               "affected_skill_ids": [], "skill_dependencies": []}
    detail = attach_execution_proposal(
        {"component_changes": [component_change()]}, {}, context=context,
        allowed_modules=["prompt"], working_bundle={}, skill_changed=False,
    )["_execution_detail"]
    detail["replay_cases"] = [
        {"case_id": "fixture-current", "query": "Persistence fixture input", "skill_ids": [],
         "checks": [{"id": "R1", "kind": "output", "text": "Persistence fixture only"}]},
        {"case_id": "fixture-locked", "query": "Independent persistence fixture input", "skill_ids": [],
         "checks": [{"id": "R1", "kind": "output", "text": "Persistence fixture only"}]},
    ]
    configs = ConfigRepository(tx)
    skills = await load_skills_config(configs, conn)
    replay = await load_replay_config(configs, conn)
    replay_config = {"revision": replay.revision, "config": replay.config}
    detail["validation_plan"] = {
        "input_hash": digest({"cases": detail["replay_cases"],
                              "baseline": detail["baseline_execution_plan"],
                              "candidate": detail["candidate_execution_plan"], "replay_config": replay_config}),
        "holdout": {"status": "ready"}, "probe_policy": {"max_total_tokens": 1_000_000},
    }
    repo = CandidateRepository(tx)
    item = await repo.create(conn, tenant_id=tenant_id, skill_name=None, bundle={}, limits=BundleLimits(),
                             detail=detail, actor="persistence-fixture")
    payload = {"candidate_id": item["candidate_id"], "revision": item["revision"], "mode": "evaluate",
               "actor": "persistence-fixture", "candidate_kind": "execution", "skill_name": None,
               "base_version": 0, "bundle": encode_bundle_payload({}), "baseline": None,
               "tree_sha256": item["tree_sha256"], "execution_candidate_hash": candidate_snapshot(item),
               "config_revision": skills.revision, "config": skills.config,
               "replay_config": replay_config, "replay_adapter": None, "detail": copy.deepcopy(detail)}
    evaluation = {
        "validation_contract": VALIDATION_CONTRACT, "validation_run_id": run_id,
        "validation_verdict": "accept", "candidate_revision": item["revision"], "status": "evaluated",
        "static_validation": {"passed": True}, "execution_gate": {"status": "passed"},
        "publication_gate": {"passed": False, "reasons": ["independent_validation_missing"]},
        "replay": {"verdict": "inconclusive", "runtime_gate": {"status": "passed"},
                   "execution_gate": {"status": "passed"}, "probe_gate": {"status": "passed"},
                   "cost_gate": {"status": "passed"}},
        "fixture_kind": "synthetic completed persistence contract; no runtime or model calls",
    }
    command, _ = await EvolutionRepository(tx).accept(
        conn, tenant_id=tenant_id, command_id=run_id, kind="skill.validate",
        request={"candidate_id": item["candidate_id"], "revision": item["revision"], "mode": "evaluate"},
        payload=payload,
    )
    await repo.transition(conn, tenant_id=tenant_id, candidate_id=item["candidate_id"], to_status="validating",
                          actor="persistence-fixture", expected_revision=item["revision"], validate_task_id=run_id)
    replays = ReplayRepository(tx)
    await replays.accept(
        conn, tenant_id=tenant_id, domain="skill", run_id=run_id, subject_id=raw["profile_id"],
        request={"validation_command": run_id},
        payload={"job": {**copy.deepcopy(detail), "job_id": item["candidate_id"],
                         "candidate_revision": item["revision"]}},
    )
    await replays.finish(conn, "skill", run_id, evaluation)
    await EvolutionRepository(tx).finish(conn, run_id, {"status": "evaluated", "evaluation": evaluation})
    item = await repo.record_verdict(
        conn, tenant_id=tenant_id, candidate_id=item["candidate_id"], revision=item["revision"],
        verdict="inconclusive", evaluation=evaluation, task_id=run_id,
    )
    return {"candidate": item, "baseline": baseline, "run_id": run_id, "payload": payload,
            "evaluation": evaluation, "command_payload_hash": command["payload_hash"]}
