"""Source-family isolation and one-candidate exposure of locked acceptance Datasets."""
from __future__ import annotations

import copy

from team_replay.datasets.candidate_selection import attach_dataset
from team_replay.datasets.repository import DatasetRepository
from team_skills.evolution.repository import lock_key
from team_skills.library.repository import SkillLibraryError

STANDARD_VALIDATION = "standard-v1"
ENHANCED_VALIDATION = "enhanced-v1"


def enhanced_validation(detail):
    """Only a newly frozen standard flag relaxes legacy enhanced acceptance."""
    strategy = (detail.get("validation_plan") or {}).get("validation_strategy", ENHANCED_VALIDATION)
    if not isinstance(strategy, str) or strategy not in {STANDARD_VALIDATION, ENHANCED_VALIDATION}:
        raise SkillLibraryError("invalid frozen execution validation strategy")
    return strategy == ENHANCED_VALIDATION


def source_families(value):
    provenance = value.get("provenance") or {}
    if not isinstance(provenance, dict):
        return set()
    values = provenance.get("source_session_keys") or []
    if not isinstance(values, list) or any(not isinstance(v, str) or not v for v in values):
        return set()
    key = provenance.get("source_session_key")
    if key is not None and (not isinstance(key, str) or not key):
        return set()
    if key:
        values = [*values, key]
    return {v for v in values if isinstance(v, str) and v}


def validation_policy(environment):
    raw = environment.get("validation") or {}
    if not isinstance(raw, dict) or set(raw) - {
        "holdout_dataset_ids", "max_probe_cases", "max_extra_runs", "repetitions", "max_total_tokens", "strategy",
    }:
        raise SkillLibraryError("invalid frozen execution validation policy")
    ids = raw.get("holdout_dataset_ids", [])
    if (not isinstance(ids, list) or len(ids) > 8 or any(not isinstance(v, str) or not v for v in ids)
            or len(set(ids)) != len(ids)):
        raise SkillLibraryError("holdout_dataset_ids must contain at most eight distinct Dataset IDs")
    strategy = raw.get("strategy", STANDARD_VALIDATION)
    if not isinstance(strategy, str) or strategy not in {STANDARD_VALIDATION, ENHANCED_VALIDATION}:
        raise SkillLibraryError("invalid execution validation strategy")
    result = {"holdout_dataset_ids": ids, "strategy": strategy}
    for key, default, lower, upper in (
        ("max_probe_cases", 4, 1, 8), ("max_extra_runs", 5, 2, 8),
        ("repetitions", 2, 2, 3), ("max_total_tokens", 1_000_000, 1, 10_000_000),
    ):
        value = raw.get(key, default)
        if type(value) is not int or not lower <= value <= upper:
            raise SkillLibraryError(f"{key} must be an integer in {lower}..{upper}")
        result[key] = value
    return result


def _authorized_budget_policy(policy, override, *, label, exact_previous=False):
    """Validate the same raise-only authorization for validation and generation."""
    if (not isinstance(override, dict)
            or set(override) != {"max_total_tokens", "previous_max_total_tokens", "reason"}
            or type(override["previous_max_total_tokens"]) is not int
            or not policy["max_total_tokens"] <= override["previous_max_total_tokens"] <= 10_000_000
            or (exact_previous and override["previous_max_total_tokens"] != policy["max_total_tokens"])
            or (override["max_total_tokens"] is not None and (
                type(override["max_total_tokens"]) is not int
                or not override["previous_max_total_tokens"] < override["max_total_tokens"] <= 10_000_000
            ))
            or not isinstance(override["reason"], str) or not override["reason"].strip()
            or len(override["reason"]) > 2000):
        raise SkillLibraryError(f"invalid {label} budget authorization")
    return {**policy, "max_total_tokens": override["max_total_tokens"]}


def candidate_validation_policy(detail):
    """An audited override raises or removes the token ceiling, never changing model settings."""
    policy = validation_policy(detail["candidate_execution_plan"]["environment"])
    override = detail.get("validation_budget")
    if override is None:
        return policy
    return _authorized_budget_policy(policy, override, label="Candidate validation")


def generation_budget_policy(baseline_execution_plan, authorization=None):
    """Apply a frozen one-command authorization without modifying the Agent plan.

    The original profile remains finite. Explicit authorization may raise or
    remove this execution's generation ceiling; missing authorization retains
    the original policy. Historical usage and other acceptance rules are not
    altered by this policy selection.
    """
    if not isinstance(baseline_execution_plan, dict) or not isinstance(
        baseline_execution_plan.get("environment"), dict
    ):
        raise SkillLibraryError("generation budget requires a frozen execution plan")
    policy = copy.deepcopy(validation_policy(baseline_execution_plan["environment"]))
    if authorization is None:
        return policy
    return _authorized_budget_policy(policy, authorization, label="generation", exact_previous=True)


def freeze_generation_budget(raw, *, frozen_baseline, allowed_modules):
    """Freeze explicit generation authority beside, never inside, a release document."""
    if not isinstance(allowed_modules, (list, tuple)) or "prompt" not in allowed_modules:
        raise SkillLibraryError("generation_budget is only supported when Prompt evolution is selected")
    if not isinstance(raw, dict) or set(raw) != {"max_total_tokens", "reason"}:
        raise SkillLibraryError("generation_budget requires max_total_tokens and an authorization reason")
    if not isinstance(frozen_baseline, dict) or frozen_baseline.get("status") != "frozen":
        raise SkillLibraryError("generation budget requires a frozen business execution baseline")
    try:
        plan = frozen_baseline["release"]["document"]["execution_plan"]
    except (KeyError, TypeError) as exc:
        raise SkillLibraryError("generation budget requires a frozen business execution baseline") from exc
    original = generation_budget_policy(plan)
    authorization = {"max_total_tokens": copy.deepcopy(raw["max_total_tokens"]),
                     "previous_max_total_tokens": original["max_total_tokens"],
                     "reason": copy.deepcopy(raw["reason"])}
    generation_budget_policy(plan, authorization)
    return authorization


async def reserved_families(conn, tx, plan):
    """Only family IDs enter planning; no locked queries, answers or Checks enter the writer."""
    policy = validation_policy(plan["environment"])
    # Existing generation commands lack a strategy marker. Preserve their
    # locked-source exclusions; only an explicit profile choice removes them.
    if (plan["environment"].get("validation") or {}).get("strategy") == STANDARD_VALIDATION:
        return []
    families = set()
    for dataset_id in policy["holdout_dataset_ids"]:
        saved = await DatasetRepository(tx).get(conn, dataset_id)
        for case in saved["cases"]:
            families.update(source_families(case))
    return sorted(families)


async def freeze_holdout(conn, tx, *, tenant_id, detail, candidate_id, candidate_revision):
    policy = candidate_validation_policy(detail)
    cases, frozen, reasons, families = [], [], [], set()
    diagnostic = set()
    for session in detail.get("session_evidence", []):
        keys = source_families(session)
        if not keys:
            reasons.append("diagnostic_source_unknown")
        diagnostic.update(keys)
    for case in detail.get("replay_cases", []):
        keys = source_families(case)
        if not keys:
            reasons.append("diagnostic_source_unknown")
        diagnostic.update(keys)
    if not diagnostic:
        reasons.append("diagnostic_source_unknown")
    repo = DatasetRepository(tx)
    pid = detail["execution_profile_id"]
    # Serialize all claims for a profile before reading usage, including absent rows.
    await lock_key(conn, tenant_id, "execution-holdout")
    for dataset_id in policy["holdout_dataset_ids"]:
        saved = await repo.freeze(conn, dataset_id)
        document = saved["document"]
        if document.get("metadata", {}).get("validation_role") != "locked_acceptance":
            reasons.append("holdout_not_locked:" + dataset_id)
            continue
        source_keys = set().union(*(source_families(case) for case in document["cases"]))
        if not document["cases"] or any(not source_families(case) for case in document["cases"]):
            reasons.append("holdout_source_unknown:" + dataset_id)
            continue
        if source_keys & (diagnostic | families):
            reasons.append("holdout_source_overlap:" + dataset_id)
            continue
        old = await conn.fetch(
            f"SELECT source_family,candidate_id,candidate_revision FROM {tx.pool.schema}.execution_holdout_uses "
            "WHERE source_family=ANY($1::text[])", sorted(source_keys),
        )
        if any(r["candidate_id"] != candidate_id or r["candidate_revision"] != candidate_revision for r in old):
            reasons.append("holdout_already_exposed:" + dataset_id)
            continue
        await conn.executemany(
            f"INSERT INTO {tx.pool.schema}.execution_holdout_uses "
            "(tenant_id,profile_id,source_family,dataset_id,dataset_revision,candidate_id,candidate_revision) "
            "VALUES($1,$2,$3,$4,$5,$6,$7) ON CONFLICT DO NOTHING",
            [(tenant_id, pid, key, dataset_id, saved["revision"], candidate_id, candidate_revision)
             for key in sorted(source_keys)],
        )
        families.update(source_keys)
        frozen.append({"dataset_id": dataset_id, "revision": saved["revision"],
                       "document_hash": saved["document_hash"], "source_families": sorted(source_keys)})
        for case in saved["execution_document"]["cases"]:
            value = attach_dataset(copy.deepcopy(case), {**document, **saved}, selection="historical")
            value["provenance"]["replay_selection"] = "locked_acceptance"
            cases.append(value)
    if not frozen:
        reasons.append("locked_acceptance_missing")
    return {"status": "ready" if not reasons else "inconclusive", "reasons": reasons,
            "datasets": frozen, "case_ids": [c["case_id"] for c in cases], "policy": policy}, cases
