"""Atomic publication of only the changed assets and one complete release."""
from __future__ import annotations

import copy

from team_skills.library.bundle import bundle_tree_sha256
from team_skills.library.repository import SkillLibraryError, SkillLibraryRepository

from .repository import ExecutionRepository, ExecutionVersionConflict


async def publish_candidate(conn, tx, *, tenant_id, payload, bundle, baseline, config):
    detail = payload["detail"]
    repository = ExecutionRepository(tx)
    profile = await repository.profile(conn, detail["execution_profile_id"], for_update=True)
    release_id = f"candidate:{payload['candidate_id']}:r{payload['revision']}"
    if profile["current_release_id"] != detail["base_release_id"]:
        # A successfully committed idempotent invocation is handled by the command ledger.
        if profile["current_release_id"] == release_id:
            saved = await repository.release(conn, release_id)
            return {"release_id": release_id, "composition_hash": saved["composition_hash"],
                    "actual_modules": detail["actual_modules"],
                    "skill_version": payload["base_version"] if payload["skill_name"] else None}
        raise ExecutionVersionConflict("execution composition changed after validation")
    library = SkillLibraryRepository(tx)
    await library.require_versions(conn, detail["skill_dependencies"])
    skill_changed = bundle_tree_sha256(bundle) != bundle_tree_sha256(baseline)
    if skill_changed and "skill" not in detail.get("allowed_modules", ["skill"]):
        raise SkillLibraryError("scope_violation: Skill was not selected")
    version = payload["base_version"]
    if skill_changed:
        commit = await library.commit_bundle(
            conn, tenant_id=tenant_id, name=payload["skill_name"], bundle=bundle,
            mutation_id=release_id, limits=config.bundle_limits, actor=payload["actor"],
            source="candidate-review", base_version=payload["base_version"],
            metadata={"candidate_id": payload["candidate_id"], "revision": payload["revision"]},
        )
        version = commit.version
    dependencies = copy.deepcopy(detail["skill_dependencies"])
    for dependency in dependencies:
        if dependency["name"] == payload["skill_name"]:
            dependency.update(version=version, tree_sha256=bundle_tree_sha256(bundle))
    document = {"execution_plan": detail["candidate_execution_plan"], "skill_dependencies": dependencies,
                "origin": "candidate-review"}
    release = await repository.publish(
        conn, tenant_id=tenant_id, profile_id=detail["execution_profile_id"],
        expected_release_id=detail["base_release_id"], document=document, actor=payload["actor"],
        candidate_id=payload["candidate_id"], candidate_revision=payload["revision"],
        validation_ref=payload["command_id"],
    )
    return {"release_id": release["release_id"], "composition_hash": release["composition_hash"],
            "skill_version": version if payload["skill_name"] else None,
            "version": version if skill_changed else None,
            "actual_modules": detail["actual_modules"]}


async def rollback_command(task, services, payload):
    from team_skills.evolution.repository import EvolutionRepository
    from teamEvolver.runtime import HandlerResult

    async with services.tx.tenant_tx(task.tenant_id) as conn:
        await services.tasks.require_current(conn, task)
        repository = ExecutionRepository(services.tx)
        profile, document = await repository.rollback_document(conn, payload["profile_id"], payload["release_id"])
        if profile["current_release_id"] != payload["expected_release_id"]:
            raise ExecutionVersionConflict("composition changed before rollback")
        library = SkillLibraryRepository(services.tx)
        restored = []
        for dependency in sorted(document["skill_dependencies"], key=lambda item: item["name"]):
            # Lock every dependency, including unchanged ones, before moving the release head.
            head = await library._lock_head(conn, dependency["name"])
            if head and head["tree_sha256"] == dependency["tree_sha256"]:
                restored.append({**dependency, "version": head["current_version"]})
            else:
                commit = await library.commit_rollback(
                    conn, tenant_id=task.tenant_id, name=dependency["name"], target_version=dependency["version"],
                    mutation_id=f"{task.task_id}:{dependency['name']}", actor=payload["actor"],
                    source="composition-rollback",
                )
                restored.append({**dependency, "version": commit.version})
        document["skill_dependencies"] = restored
        release = await repository.publish(
            conn, tenant_id=task.tenant_id, profile_id=profile["profile_id"],
            expected_release_id=profile["current_release_id"], document=document, actor=payload["actor"],
            candidate_id=task.task_id, candidate_revision=1, validation_ref="rollback:" + payload["release_id"],
            restored_from=payload["release_id"],
        )
        await EvolutionRepository(services.tx).finish(conn, task.task_id, {
            "status": "rolled_back", "release_id": release["release_id"], "restored_from": payload["release_id"],
            "composition_hash": release["composition_hash"],
        })
    return HandlerResult.succeed(task.task_id)
