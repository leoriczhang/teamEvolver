"""Freeze current Candidate inputs; historical sampling is explicitly requested."""
from __future__ import annotations

import copy
import random

from .._util import stable_hash
from .repository import DatasetRepository
from .schema import make_document

HISTORICAL_CASE_COUNT = 3


def case_fingerprint(case):
    return stable_hash({
        "query": case.get("query"), "checks": case.get("checks"),
        "materials": [{key: item.get(key) for key in ("path", "sha256", "size")}
                      for item in case.get("materials", [])],
    })


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


def eligible_case(case, active_ma_connections):
    provenance = case.get("provenance") or {}
    runtime = provenance.get("source_runtime") or (provenance.get("session_snapshot") or {}).get("runtime") or {}
    return (active_ma_connections is None or runtime.get("type") != "managed-agents"
            or runtime.get("connection_id") in active_ma_connections)


async def freeze_candidate_cases(conn, tx, *, tenant_id, candidate_id, skill_name, detail, seed,
                                 active_ma_connections=None, historical_count=0, execution_fingerprints=None):
    """Persist the current Dataset and freeze all selected inputs before enqueue."""
    repo = DatasetRepository(tx)
    manual_ids = set((detail.get("manual_reprocess") or {}).get("session_ids") or [])

    def explicitly_selected(case):
        provenance = case.get("provenance") or {}
        ids = provenance.get("session_ids") or [provenance.get("session_id")]
        return bool(ids) and set(ids) <= manual_ids

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
            dataset_id=dataset_id, name=f"{skill_name} · 当次进化数据集", skills=[skill_name],
            cases=current_cases, source={
                "kind": "manual_evolution" if manual_ids else "evolution", "candidate_id": candidate_id,
            },
            metadata={"enabled_for_evolution": not bool(manual_ids), "read_only": True},
        )
        files = list({
            material["path"]: {"path": material["path"], "content_b64": material["content_b64"]}
            for case in current_cases for material in case.get("materials", []) if "content_b64" in material
        }.values())
        await repo.save(conn, tenant_id=tenant_id, document=document, files=files or None)
    from team_skills.library.queries import evolution_boundary
    await repo.archive_before_boundary(conn, skill_id=skill_name,
                                       boundary=await evolution_boundary(conn, tx, skill_name))
    current = await repo.freeze(conn, dataset_id, revision=1)
    rows = await repo.historical_cases(conn, skill_id=skill_name, before_dataset_id=dataset_id)
    if execution_fingerprints is not None:
        from ..fingerprints import execution_fingerprint, ineligibility_reason
        for case in current["execution_document"]["cases"]:
            if explicitly_selected(case):
                continue
            reason = ineligibility_reason(case, execution_fingerprints)
            if reason:
                await repo.archive_case(
                    conn, tenant_id=tenant_id, dataset_id=dataset_id, revision=1, case_id=case["case_id"],
                    reason=reason, fingerprint=execution_fingerprint((case.get("provenance") or {})
                                                                   .get("source_runtime") or {}) or {},
                )
        eligible_rows = []
        for row in rows:
            reason = ineligibility_reason(row["case"], execution_fingerprints)
            if reason:
                await repo.archive_case(
                    conn, tenant_id=tenant_id, dataset_id=row["dataset_id"], revision=row["revision"],
                    case_id=row["case"]["case_id"], reason=reason,
                    fingerprint=execution_fingerprint((row["case"].get("provenance") or {})
                                                       .get("source_runtime") or {}) or {},
                )
            else:
                eligible_rows.append(row)
        rows = eligible_rows
    archived = {row["case_id"] for row in await repo.archives(conn, dataset_id, 1)}
    eligible_current = [case for case in current["execution_document"]["cases"]
                        if eligible_case(case, active_ma_connections) and case["case_id"] not in archived]
    eligible_history = [row for row in rows if eligible_case(row["case"], active_ma_connections)]
    sampled = sample_history(eligible_history, current["document"]["cases"], seed=seed, count=historical_count)
    cases = [
        attach_dataset(case, {**current["document"], **current}, selection="current")
        for case in eligible_current
    ]
    for row in sampled:
        case = copy.deepcopy(row["case"])
        case["materials"] = [await repo.material(conn, item) for item in case.get("materials", [])]
        cases.append(attach_dataset(case, row, selection="historical"))
    return {
        **detail, "replay_cases": cases,
        "replay_sampling": {
            "seed": seed, "current_dataset_id": dataset_id, "current_case_count": len(eligible_current),
            "excluded_paused_current": len(current_cases) - len(eligible_current),
            "excluded_paused_history": len(rows) - len(eligible_history),
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
