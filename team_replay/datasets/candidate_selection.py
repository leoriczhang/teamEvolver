"""Freeze the current Candidate Dataset and configured historical regression Cases."""
from __future__ import annotations

import copy
import random

from .._util import stable_hash
from ..execution_plan import execution_plan_hash
from .repository import DatasetRepository
from .schema import make_document

HISTORICAL_CASE_COUNT = 3


def case_fingerprint(case):
    inputs = {
        "query": case.get("query"), "checks": case.get("checks"),
        "materials": [{key: item.get(key) for key in ("path", "sha256", "size")}
                      for item in case.get("materials", [])],
    }
    binding = (case.get("provenance") or {}).get("execution_inputs")
    if binding is not None:
        inputs["execution_inputs"] = {key: binding[key] for key in (
            "schema_version", "template_refs", "template_variables", "input_messages",
        )}
    return stable_hash(inputs)


def sample_history(rows, current, *, seed, count=HISTORICAL_CASE_COUNT):
    """Sample without replacement, covering different Datasets before repeats."""
    rng = random.Random(seed)
    excluded = {case_fingerprint(case) for case in current}
    groups = {}
    for row in rows:
        case = row["case"]
        fingerprint = case_fingerprint(case)
        if fingerprint in excluded:
            continue
        excluded.add(fingerprint)
        groups.setdefault(row["dataset_id"], []).append(row)
    keys = sorted(groups)
    rng.shuffle(keys)
    for values in groups.values():
        rng.shuffle(values)
    selected = []
    while keys and len(selected) < count:
        remaining = []
        for key in keys:
            selected.append(groups[key].pop())
            if len(selected) == count:
                break
            if groups[key]:
                remaining.append(key)
        keys = remaining
    return copy.deepcopy(selected)


def attach_dataset(case, dataset, *, selection):
    result = copy.deepcopy(case)
    provenance = result.setdefault("provenance", {})
    provenance.update(
        dataset_id=dataset["dataset_id"], dataset_name=dataset["name"],
        dataset_revision=dataset["revision"], dataset_hash=dataset["document_hash"],
        dataset_case_id=case["case_id"], replay_selection=selection,
        evidence_window="recent" if selection == "current" else "historical",
    )
    if selection == "historical":
        result["case_id"] = "history-" + stable_hash({
            "dataset_id": dataset["dataset_id"], "revision": dataset["revision"], "case_id": case["case_id"],
        })[:32]
    return result


async def freeze_candidate_cases(conn, tx, *, tenant_id, candidate_id, skill_name, detail, seed,
                                 candidate_kind=None, execution_profile_id=None,
                                 historical_count=HISTORICAL_CASE_COUNT):
    """Persist the current Dataset and freeze all selected inputs before enqueue."""
    if type(historical_count) is not int or not 1 <= historical_count <= 30:
        raise ValueError("historical Replay Case count must be an integer in 1..30")
    repo = DatasetRepository(tx)
    execution_only = (candidate_kind or detail.get("candidate_kind")) == "execution"
    profile_id = execution_profile_id or detail.get("execution_profile_id")
    binding = ({"execution_profile_id": profile_id,
                "baseline_execution_plan_hash": execution_plan_hash(detail["baseline_execution_plan"])}
               if execution_only else {})
    if execution_only and (not profile_id or skill_name):
        raise ValueError("execution Candidate requires its profile without a Skill name")
    current_cases = copy.deepcopy(detail.get("replay_cases") or [])
    if not current_cases:
        return {**detail, "replay_cases": [], "replay_sampling": {
            "historical_requested": historical_count, "historical_selected": 0,
        }}
    dataset_id = "evolution-" + stable_hash({"candidate_id": candidate_id, "cases": current_cases})[:32]
    existing = await conn.fetchval(
        f"SELECT revision FROM {repo.schema}.datasets WHERE dataset_id=$1", dataset_id,
    )
    if not existing:
        document = make_document(
            dataset_id=dataset_id, name=f"{profile_id if execution_only else skill_name} · 当次进化数据集",
            skills=[] if execution_only else [skill_name],
            cases=current_cases, source={"kind": "evolution", "candidate_id": candidate_id},
            metadata={"enabled_for_evolution": True, "read_only": True, **binding},
        )
        files = list({
            material["path"]: {"path": material["path"], "content_b64": material["content_b64"]}
            for case in current_cases for material in case.get("materials", []) if "content_b64" in material
        }.values())
        await repo.save(conn, tenant_id=tenant_id, document=document, files=files or None)
    current = await repo.freeze(conn, dataset_id, revision=1)
    rows = (await repo.execution_history(conn, profile_id=profile_id,
                                        baseline_hash=binding["baseline_execution_plan_hash"],
                                        before_dataset_id=dataset_id) if execution_only else
            await repo.historical_cases(conn, skill_id=skill_name, before_dataset_id=dataset_id))
    sampled = sample_history(rows, current["document"]["cases"], seed=seed, count=historical_count)
    cases = [
        attach_dataset(case, {**current["document"], **current}, selection="current")
        for case in current["execution_document"]["cases"]
    ]
    for row in sampled:
        case = copy.deepcopy(row["case"])
        case["materials"] = [await repo.material(conn, item) for item in case.get("materials", [])]
        cases.append(attach_dataset(case, row, selection="historical"))
    return {
        **detail, "replay_cases": cases,
        "replay_sampling": {
            "seed": seed, "current_dataset_id": dataset_id, "current_case_count": len(current_cases),
            "historical_requested": historical_count, "historical_selected": len(sampled),
            "historical_dataset_count": len({row["dataset_id"] for row in sampled}),
        },
    }


def dataset_groups(cases):
    groups = {}
    for case in cases:
        provenance = case.get("provenance") or {}
        dataset_id = provenance.get("dataset_id") or "current"
        group = groups.setdefault(dataset_id, {
            "dataset_id": dataset_id, "name": provenance.get("dataset_name") or "当次数据集",
            "revision": provenance.get("dataset_revision"), "document_hash": provenance.get("dataset_hash", ""),
            "selection": provenance.get("replay_selection") or "current", "case_ids": [],
        })
        group["case_ids"].append(case["case_id"])
    return list(groups.values())
