"""Audited recovery of a published Prompt proposal after its exact release was rolled back."""
from __future__ import annotations

import copy

from team_skills.evolution.repository import EvolutionRepository, digest
from team_skills.library.repository import SkillLibraryError

from .candidates import revise_execution_detail
from .repository import ExecutionRepository, ExecutionVersionConflict


def baseline_identity(document):
    """Rollback creates new asset revisions; every executable byte and binding must match."""
    value = copy.deepcopy(document)
    value.pop("origin", None)
    plan = value["execution_plan"]
    plan.pop("target_component_ids", None)
    if plan["binding"].get("allow_skill_change") is False:
        plan["binding"].pop("allow_skill_change")
    for part in plan["components"]:
        part.pop("revision", None)
    return value


async def prepare_recovery(conn, tx, item, *, expected_release_id, changes):
    """Only the completed, immediate rollback of this publication can resume its Candidate."""
    repository = ExecutionRepository(tx)
    profile = await repository.profile(conn, item["execution_profile_id"], for_update=True)
    if profile["current_release_id"] != expected_release_id:
        raise ExecutionVersionConflict("execution head changed before Candidate recovery")
    current = await repository.release(conn, expected_release_id)
    published_id = f"candidate:{item['candidate_id']}:r{item['revision']}"
    published = await repository.release(conn, published_id)
    original = await repository.release(conn, item["detail"]["base_release_id"])
    command = await EvolutionRepository(tx).command(conn, current["candidate_id"])
    payload, result = (command or {}).get("payload") or {}, (command or {}).get("result") or {}
    if (published["profile_id"] != profile["profile_id"]
            or current["profile_id"] != profile["profile_id"]
            or original["profile_id"] != profile["profile_id"]
            or published["candidate_id"] != item["candidate_id"]
            or published["candidate_revision"] != item["revision"]
            or item["mutation_id"] != published_id
            or item["decision"].get("release_id") != published_id
            or not current["restored_from"]
            or current["validation_ref"] != "rollback:" + current["restored_from"]
            or payload.get("operation") != "execution_rollback"
            or payload.get("profile_id") != profile["profile_id"]
            or payload.get("expected_release_id") != published_id
            or payload.get("release_id") != current["restored_from"]
            or result.get("status") != "rolled_back"
            or result.get("release_id") != expected_release_id
            or result.get("restored_from") != current["restored_from"]
            or baseline_identity(original["document"]) != baseline_identity(current["document"])):
        raise ExecutionVersionConflict("Candidate recovery requires its completed rollback to the original baseline")
    detail = copy.deepcopy(item["detail"])
    detail.update(base_release_id=expected_release_id,
                  baseline_execution_plan=copy.deepcopy(current["document"]["execution_plan"]))
    detail = revise_execution_detail(detail, changes)
    for key in ("independent_validation", "validation_plan", "dependency_treatments"):
        detail.pop(key, None)
    audit = {"event_type": "resume_after_rollback", "published_release_id": published_id,
             "rollback_release_id": expected_release_id, "rollback_command_id": current["candidate_id"],
             "prior_validation_run_id": item["validate_task_id"],
             "prior_evaluation_hash": digest(item["evaluation"])}
    return detail, audit


def budget_override(detail, maximum, reason):
    from .holdout import candidate_validation_policy

    previous = candidate_validation_policy(detail)["max_total_tokens"]
    if (previous is None or (maximum is not None and (
            type(maximum) is not int or not previous < maximum <= 10_000_000))
            or not isinstance(reason, str) or not reason.strip() or len(reason) > 2000):
        raise SkillLibraryError(
            "budget change requires a finite current ceiling, a higher bounded integer or explicit null, "
            "and an authorization reason"
        )
    value = copy.deepcopy(detail)
    value["validation_budget"] = {"max_total_tokens": maximum, "previous_max_total_tokens": previous,
                                  "reason": reason}
    return value
