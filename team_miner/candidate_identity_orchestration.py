"""Durable TE identity review; the public Compile transport and frozen requests stay unchanged."""

from __future__ import annotations

import asyncio
import copy
import json
import os
from pathlib import Path

from . import bounded_compile_plan as planning
from .candidate_identity import (
    CONSOLIDATING_PARTITION_PROTOCOL,
    IDENTITY_FIELDS,
    IDENTITY_GROUP_PROTOCOL_V2,
    IDENTITY_PROTOCOL,
    MAX_PARTITION_IDENTITIES,
    MAX_RETRIEVAL_PASSES,
    SUPPORTED_IDENTITY_PARTITION_PROTOCOLS,
    IdentityCatalog,
    candidate_id,
    identity_brief,
    identity_version,
    render_identity_partition_compiler,
    validate_identity_groups,
)
from .candidate_refinement import (
    BUSINESS_FIELDS,
    latest_refinement,
    mapped_method_ordinals,
    mapped_previous_source_scope,
    merge_candidate_scopes,
    metadata_sha256,
)
from .compile_strategy import render_single_stage_compiler

GROUP_RULE = '''Only compare the supplied known Candidate identities. Candidate metadata is NOT factual Evidence.
Return exactly {"schema_version":"skill-miner.identity-groups.v1","candidate_ids":[all assigned IDs],
"groups":[{"candidate_ids":[known IDs],"reason":"nonempty reason"}]}.
Groups must be a complete disjoint partition, including singleton groups. Merge only identities that represent
one complete independently usable workflow with equivalent purpose, trigger, required inputs, intended output and
boundary. Also merge a narrower Candidate when it is only a substep of a supplied broader phase workflow and does
not have a distinct trigger/output that requires independent invocation; nominate the broader Candidate as the
survivor when its existing canonical fields already cover all members. Typical consolidation includes an intro,
one sales technique, one data field, or one handoff substep already inside a complete role-play phase. Do not merge
independent lifecycle phases, different user goals or conflicting normative boundaries. A common source, product,
business number or formula alone does not justify merging. When uncertain keep singleton groups and record the
specific distinction. Do not output new identities, business fields, rules or numerical values. The host preserves
the nominated survivor's canonical metadata and exact previous Case IDs; it unions only the original source scopes.
'''


def _io():
    # Imported lazily to avoid a dependency cycle with the host's transport mixin.
    from .ov_staged_orchestration import _json, _read, _save, _sha, _write
    return _json, _read, _save, _sha, _write


def _explicit_survivor(state: dict) -> bool:
    from .source_scope import SOURCE_SCOPE_PROTOCOL_V3

    return state.get("source_scope_protocol") == SOURCE_SCOPE_PROTOCOL_V3


def _identity_controls(rows: list[dict], state: dict) -> dict:
    explicit = _explicit_survivor(state)
    result = {
        "identities": [identity_brief(row, include_authority=explicit) for row in rows],
        "required_candidate_ids": [row["candidate_id"] for row in rows],
        "allowed_source_refs": [], "metadata_only": True,
    }
    if explicit:
        result["identity_group_protocol"] = IDENTITY_GROUP_PROTOCOL_V2
    if state.get("identity_partition_protocol") in SUPPORTED_IDENTITY_PARTITION_PROTOCOLS:
        result["identity_partition_protocol"] = state["identity_partition_protocol"]
        result["max_identities"] = MAX_PARTITION_IDENTITIES
    if state.get("identity_partition_protocol") == CONSOLIDATING_PARTITION_PROTOCOL:
        from .identity_components import PROTOCOL
        result["identity_consolidation_protocol"] = PROTOCOL
    return result


def _freeze_group_protocol(context: dict, state: dict) -> None:
    if _explicit_survivor(state):
        context["identity_group_protocol"] = IDENTITY_GROUP_PROTOCOL_V2
    if state.get("identity_partition_protocol") == CONSOLIDATING_PARTITION_PROTOCOL:
        from .identity_components import PROTOCOL
        context["identity_consolidation_protocol"] = PROTOCOL


def _partition_level(root: Path, state: dict, level: int) -> dict:
    """Freeze each current identity/version once in bounded, durable nonoverlapping windows."""
    _, read, save, sha, _ = _io()
    folder = root / "identity" / "partitions" / f"level-{level}"
    ready = folder / "ready.json"
    if ready.is_file():
        value = read(ready)
        if value.get("protocol") != state.get("identity_partition_protocol") or value.get("level") != level:
            raise ValueError("Frozen identity partition level changed")
        return value
    budget = state["identity_partition_packet_budget"]
    catalog = IdentityCatalog(root / "identity" / "catalog.sqlite3")
    batch, identity_count, pending = 0, 0, []

    def flush(rows):
        nonlocal batch, identity_count
        controls = _identity_controls(rows, state)
        versions = {row["candidate_id"]: identity_version(row) for row in rows}
        context = {
            "identity_ids": list(versions), "identity_versions": versions,
            "identity_pass": level, "anchor_ordinal": rows[0]["ordinal"], "after": batch + 1,
            "identity_partition_protocol": state["identity_partition_protocol"],
            "identity_partition_level": level, "identity_partition_batch": batch,
            "identity_partition_file": f"identity/partitions/level-{level}/{batch:08d}.json",
            "coverage_receipts": [{
                "ordinal": row["ordinal"], "candidate_id": row["candidate_id"],
                "candidate_version": versions[row["candidate_id"]],
                "status": "partition-reviewed", "assigned": list(versions),
                "semantic_pairs_exhaustive": False,
            } for row in rows],
        }
        _freeze_group_protocol(context, state)
        value = {"protocol": state["identity_partition_protocol"], "controls": controls, "context": context}
        path = root / context["identity_partition_file"]
        if path.is_file() and read(path) != value:
            raise ValueError("Frozen identity partition assignment changed")
        save(path, value)
        if level == 0:
            for row in rows:
                row = copy.deepcopy(row)
                row["identity_partition_origins"] = [batch]
                catalog.put(row)
        batch += 1
        identity_count += len(rows)

    def fits(rows):
        if len(rows) > MAX_PARTITION_IDENTITIES:
            return False
        try:
            planning.build_stage_packet(
                "identity_group", "groups.json", controls=_identity_controls(rows, state),
                language_policy=state["language_policy"], max_chars=budget,
            )
        except ValueError:
            return False
        return True

    try:
        for row in catalog.partition_rows(cross_batch=level == 1):
            if not fits(pending + [row]):
                if pending:
                    flush(pending)
                    pending = []
                if not fits([row]):
                    raise ValueError("One complete candidate identity exceeds its frozen compiler input budget")
            pending.append(row)
        if pending:
            flush(pending)
        value = {"protocol": state["identity_partition_protocol"], "level": level,
                 "batch_count": batch, "identity_count": identity_count, "packet_budget": budget,
                 "collection_version": catalog.collection_version()}
        # Freeze a checksum for every bounded file, without holding candidate
        # metadata or source bodies for the entire collection in memory.
        value["batches_sha256"] = [
            sha(json.dumps(read(folder / f"{index:08d}.json"), ensure_ascii=False,
                           separators=(",", ":"), allow_nan=False)) for index in range(batch)
        ]
        save(ready, value)
        return value
    finally:
        catalog.close()


def _partition_assignment(root: Path, context: dict) -> dict:
    json_text, read, _, sha, _ = _io()
    level, index = context["identity_partition_level"], context["identity_partition_batch"]
    if (type(level) is not int or level not in {0, 1} or type(index) is not int or index < 0
            or context["identity_partition_file"] != f"identity/partitions/level-{level}/{index:08d}.json"):
        raise ValueError("Frozen identity partition locator changed")
    value = read(root / context["identity_partition_file"])
    ready = read(root / "identity" / "partitions" / f"level-{level}" / "ready.json")
    digest = sha(json_text(value))
    if (value.get("protocol") != context.get("identity_partition_protocol")
            or index >= ready["batch_count"]
            or ready["batches_sha256"][index] != digest
            or context.get("identity_partition_sha256") != digest
            or any(context.get(key) != expected for key, expected in value["context"].items())):
        raise ValueError("Frozen identity partition assignment or versions changed")
    return value


def _partition_window(root: Path, state: dict) -> tuple[list[dict], dict] | None:
    if not _explicit_survivor(state):
        raise ValueError("Identity partitions require the frozen explicit-survivor protocol")
    json_text, read, save, sha, _ = _io()
    level = state.setdefault("identity_partition_level", 0)
    cursor = state.setdefault("identity_partition_cursor", 0)
    while level < 2:
        manifest = _partition_level(root, state, level)
        state.setdefault("identity_partition_levels", {})[str(level)] = {
            key: manifest[key] for key in ("batch_count", "identity_count", "packet_budget")
        }
        if cursor >= manifest["batch_count"]:
            if level == 0 and manifest["batch_count"] > 1:
                level, cursor = 1, 0
                state.update(identity_partition_level=level, identity_partition_cursor=cursor)
                continue
            state.update(identity_partition_level=2, identity_pass=MAX_RETRIEVAL_PASSES)
            return None
        value = read(root / "identity" / "partitions" / f"level-{level}" / f"{cursor:08d}.json")
        context = copy.deepcopy(value["context"])
        context["identity_partition_sha256"] = sha(json_text(value))
        _partition_assignment(root, context)
        catalog = IdentityCatalog(root / "identity" / "catalog.sqlite3")
        try:
            rows = [catalog.get(identity) for identity in context["identity_ids"]]
            if any(not catalog.is_active(row["candidate_id"])
                   or identity_version(row) != context["identity_versions"][row["candidate_id"]] for row in rows):
                raise ValueError("Frozen identity partition versions differ from the durable catalog")
            if _identity_controls(rows, state) != value["controls"]:
                raise ValueError("Frozen identity partition business metadata changed")
        finally:
            catalog.close()
        if len(rows) == 1:
            # A singleton cannot merge anything; record that limited decision
            # locally instead of paying for another independent model call.
            receipt = context["coverage_receipts"][0]
            receipt.update(status="partition-singleton", semantic_pairs_exhaustive=False)
            save(root / "identity" / "coverage" / f"{level}-{rows[0]['ordinal']:08d}.json", receipt)
            cursor += 1
            state["identity_partition_cursor"] = cursor
            continue
        state["identity_pass"], state["identity_cursor"] = level, cursor
        return rows, context
    return None


def _prior_authorities(job: dict, root: Path):
    """Use reviewed same-job metadata and the verified exact source mapping; never guess filenames."""
    from .ov_staged_orchestration import _prior_candidate_rows
    _, read, _, sha, _ = _io()
    number = int(job["current_round"]) - 1
    if number < 1:
        return
    reviewed = root.parents[2] / "rounds" / f"round-{number}" / "reviewed_output"
    plan_path = reviewed / "semantic_reports" / "capability-plan.json"
    if not plan_path.is_file():
        plan_path = reviewed / "analysis" / "capability-plan.json"
    if not plan_path.is_file():
        return
    if plan_path.is_symlink() or reviewed.resolve() not in plan_path.resolve().parents:
        raise ValueError("Reviewed candidate authority path escapes the same-job archive")
    for candidate in read(plan_path).get("skills", []):
        prior = next(_prior_candidate_rows(root, name=candidate["name"]), None)
        if not prior:
            continue
        uris, ordinals = [], []
        for relative in prior["scope_relative_paths"]:
            uri = job["source_snapshot"].rstrip("/") + "/" + relative
            path = root / "source-files" / (sha(uri) + ".json")
            if path.is_file():
                source = read(path)
                if source["uri"] != uri:
                    raise ValueError("Prior candidate source mapping is not exact")
                uris.append(uri)
                ordinals.extend(source["ordinals"])
        if not ordinals:
            continue
        canonical = {key: copy.deepcopy(candidate[key]) for key in IDENTITY_FIELDS}
        canonical["source_refs"] = uris
        planning.validate_plan(
            {"schema_version": planning.PLAN_SCHEMA, "skills": [canonical], "deferred": []},
            source_refs=uris, bounded=False, require_nonempty_deferred=False,
        )
        from .source_scope import EFFICIENCY_PROTOCOL
        scope = None
        if job.get("staged_efficiency_protocol") == EFFICIENCY_PROTOCOL:
            scope = mapped_previous_source_scope(job, root, candidate["name"], prior["scope_relative_paths"])
        if scope is None:
            scope = {
                "ordinals": sorted(set(ordinals)), "method_ordinals": sorted(set(ordinals)),
                "refinement_ordinals": mapped_method_ordinals(
                    job, root, candidate["name"], prior["scope_relative_paths"],
                ),
            }
        yield canonical, scope


def _fork_prior_collision(job: dict, root: Path, directory: Path, plan: dict,
                          prior: dict, current: dict) -> None:
    """A reused slug is not a reviewed identity: preserve both proposals and exact scopes."""
    json_text, read, save, sha, _ = _io()
    original_name = current["name"]
    digest = sha(json_text(current))
    journal = directory / "name-conflicts" / (original_name + ".json")
    if journal.exists():
        transaction = read(journal)
        if transaction["proposed_metadata_sha256"] != digest:
            raise ValueError("Frozen candidate name conflict differs from its recorded proposal")
    else:
        taken = {row["name"] for row in plan["skills"]}
        replacement = original_name[:47] + "-" + digest[:16]
        for suffix in range(len(taken) + 1):
            if replacement not in taken:
                break
            replacement = original_name[:47] + "-" + sha(digest + ":" + str(suffix))[:16]
        else:
            raise ValueError("Cannot reserve an independent candidate name")
        transaction = {
            "protocol": "te-prior-name-conflict.v1", "old_name": original_name, "new_name": replacement,
            "prior_candidate_id": candidate_id(job["job_id"], original_name),
            "new_candidate_id": candidate_id(job["job_id"], replacement),
            "prior_metadata": copy.deepcopy(prior), "proposed_metadata": copy.deepcopy(current),
            "proposed_metadata_sha256": digest,
            "scope": read(root / "scopes" / (original_name + ".json")),
            "reason": "同名提议的业务边界发生变化，分配独立身份并保留上一轮身份，等待正常业务归组与来源复核。",
        }
        save(journal, transaction)
    replacement = transaction["new_name"]
    save(root / "scopes" / (replacement + ".json"), transaction["scope"])
    for path in (root / "method-decisions").glob("*.json"):
        row = read(path)
        if row.get("target") == "candidate" and row.get("name") == original_name:
            save(path, {**row, "name": replacement})
    current["name"] = replacement
    save(root / "plan.json", plan)


def _seed_catalog(job: dict, root: Path, folder: str, *, include_reviews: bool) -> None:
    _, read, save, _, _ = _io()
    directory = root / folder
    marker = directory / "ready.json"
    if marker.is_file():
        return
    original_plan = directory / "original-plan.json"
    source_plan = original_plan if include_reviews and original_plan.exists() else root / "plan.json"
    plan = read(source_plan) if source_plan.exists() else {
        "schema_version": planning.PLAN_SCHEMA, "skills": [], "deferred": [],
    }
    if include_reviews:
        if not (directory / "original-plan.json").exists():
            save(directory / "original-plan.json", plan)
        review_paths = sorted((root / "evidence-review" / "results").glob("*.json"))
        manifest_path = root / "evidence-review" / "manifest.json"
        if manifest_path.is_file():
            assigned = read(manifest_path)["proposal_indexes"]
            completed = [read(path)["proposal_index"] for path in review_paths]
            if set(assigned) != set(completed) or len(completed) != len(set(completed)):
                raise ValueError(
                    "Every deferred method needs its complete final Evidence review before identity grouping"
                )
        remaining_review_gaps = []
        for path in review_paths:
            review = read(path)
            gap = review["previous_gap"]
            matches = [item for item in plan["deferred"] if item["reason"] == gap["reason"]
                       and set(item["source_refs"]) == set(gap["source_refs"])]
            if review["outcome"] == "deferred":
                remaining_review_gaps.append({"reason": review["reason"], "source_refs": review["source_refs"]})
            for item in matches:
                plan["deferred"].remove(item)
            candidate = copy.deepcopy(review.get("candidate"))
            if review["outcome"] == "candidate":
                existing = next((row for row in plan["skills"] if row["name"] == candidate["name"]), None)
                if existing and any(existing[key] != candidate[key] for key in IDENTITY_FIELDS if key != "name"):
                    base = candidate["name"][:32] + "-review-" + str(review["proposal_index"])
                    taken = {row["name"] for row in plan["skills"]}
                    suffix = 0
                    replacement = base
                    while replacement in taken:
                        suffix += 1
                        replacement = base + "-" + str(suffix)
                    candidate["name"] = replacement
                    existing = None
                if existing:
                    existing["source_refs"] = sorted(set(existing["source_refs"] + candidate["source_refs"]))
                    old = read(root / "scopes" / (candidate["name"] + ".json"))
                    scope = merge_candidate_scopes(old, review)
                else:
                    plan["skills"].append(candidate)
                    scope = {key: review[key] for key in ("ordinals", "method_ordinals")}
                save(root / "scopes" / (candidate["name"] + ".json"), scope)
            for leaf in review["leaf_items"]:
                decision_path = root / "method-decisions" / (leaf + ".json")
                original = directory / "original-decisions" / (leaf + ".json")
                if decision_path.is_file() and not original.exists():
                    save(original, read(decision_path))
                save(decision_path, {
                    "target": review["outcome"],
                    "name": candidate["name"] if candidate else "", "reason": review["reason"],
                })
        for gap in remaining_review_gaps:
            if gap not in plan["deferred"]:
                plan["deferred"].append(gap)
        save(root / "plan.json", plan)
    catalog = IdentityCatalog(directory / "catalog.sqlite3")
    try:
        ordinal, prior_names = 0, set()
        # Whole-workflow planning regenerates the current round's metadata from
        # its complete members and review feedback. A previous round must not
        # overwrite that result as an immutable canonical authority. Exact
        # unchanged outputs can still be reused by the separate dependency gate.
        frozen_state = read(root / "state.json") if (root / "state.json").exists() else {}
        prior = (() if frozen_state.get("workflow_partition_protocol") == "te-workflow-partition.v2"
                 else _prior_authorities(job, root))
        for candidate, scope in prior:
            prior_names.add(candidate["name"])
            refinement = latest_refinement(root, candidate_id(job["job_id"], candidate["name"]))
            if refinement:
                candidate.update({key: copy.deepcopy(refinement["after"][key]) for key in IDENTITY_FIELDS})
            current = next((row for row in plan["skills"] if row["name"] == candidate["name"]), None)
            if current:
                if any(current[key] != candidate[key] for key in IDENTITY_FIELDS if key not in {"name", "rationale"}):
                    _fork_prior_collision(job, root, directory, plan, candidate, current)
                    current = None
            if current:
                candidate["source_refs"] = sorted(set(candidate["source_refs"] + current["source_refs"]))
                current_scope = read(root / "scopes" / (candidate["name"] + ".json"))
                refinement_ordinals = scope.get("refinement_ordinals", [])
                scope = merge_candidate_scopes(scope, current_scope)
                scope["refinement_ordinals"] = sorted(set(scope.get("refinement_ordinals", []) + refinement_ordinals))
            catalog.put({
                "candidate_id": candidate_id(job["job_id"], candidate["name"]), "candidate": candidate,
                "scope": scope, "ordinal": ordinal, "prior": True, "selected": bool(current),
            })
            ordinal += 1
        for candidate in plan["skills"]:
            if candidate["name"] in prior_names:
                continue
            catalog.put({
                "candidate_id": candidate_id(job["job_id"], candidate["name"]), "candidate": candidate,
                "scope": read(root / "scopes" / (candidate["name"] + ".json")),
                "ordinal": ordinal, "prior": False, "selected": True,
            })
            ordinal += 1
        save(marker, {"count": ordinal, "protocol": IDENTITY_PROTOCOL})
    finally:
        catalog.close()


def _selection_known(job: dict, root: Path, records: list[dict], controls: dict, *,
                     refinement_enabled: bool = False) -> None:
    json_text, read, _, _, _ = _io()
    _seed_catalog(job, root, "selection-identities", include_reviews=False)
    db = IdentityCatalog(root / "selection-identities" / "catalog.sqlite3")
    try:
        # Selection grows during the round; only new accepted candidates enter the global disk index.
        if (root / "plan.json").is_file():
            for index, candidate in enumerate(read(root / "plan.json")["skills"]):
                identity = candidate_id(job["job_id"], candidate["name"])
                try:
                    old = db.get(identity)
                except ValueError:
                    old = {
                        "ordinal": read(root / "selection-identities" / "ready.json")["count"] + index, "prior": False,
                    }
                refinement = latest_refinement(root, identity)
                if refinement and metadata_sha256(candidate) != refinement["metadata_sha256"]:
                    raise ValueError("Selected candidate differs from its accepted business refinement")
                if not old["prior"] or refinement:
                    db.put({
                        **old, "candidate_id": identity, "candidate": candidate,
                        "scope": {**read(root / "scopes" / (candidate["name"] + ".json")),
                                  "refinement_ordinals": old.get("scope", {}).get("refinement_ordinals", [])},
                        "selected": True,
                    })
        refinement_enabled = refinement_enabled and int(job["current_round"]) > 1
        controls["selection_schema"] = (
            planning.SELECTION_SCHEMA_V3 if refinement_enabled else planning.SELECTION_SCHEMA_V2
        )
        controls["known_candidates"] = []
        controls.pop("already_selected", None)
        identities = set()
        for index, record in enumerate(records):
            item = record["item"]
            probe = {
                "name": "__te-retrieval-probe-" + str(index), "task": item["task"], "trigger": item["task"],
                "inputs": [item["task"]], "outputs": [item["task"]], "boundary": item["boundary"],
                "rationale": item["task"], "source_refs": item.get("source_files", []),
            }
            probe_id = "probe-" + str(index)
            db.put({"candidate_id": probe_id, "candidate": probe, "ordinal": -1, "prior": False})
            db.deactivate(probe_id)
            for row in db.related(probe_id):
                if row["candidate_id"] in identities:
                    continue
                brief = {"candidate_id": row["candidate_id"], **{
                    key: row["candidate"][key] for key in IDENTITY_FIELDS
                }, "candidate_metadata_is_evidence": False}
                if refinement_enabled:
                    eligible = [record["item"]["item_id"] for record in records if set(record["ordinals"]).intersection(
                        row.get("scope", {}).get("refinement_ordinals", [])
                    )]
                    brief.update(base_metadata_sha256=metadata_sha256(row["candidate"]),
                                 refinement_allowed=bool(row["prior"] and eligible), refinement_item_ids=eligible)
                controls["known_candidates"].append(brief)
                if refinement_enabled or controls.get("direct_workflow_protocol"):
                    # Readonly generated notes must not crowd out an exact identity needed for revision.
                    # Typed method assignments and original Evidence are never removed.
                    for field in ("provisional_gaps", "related_facts"):
                        while controls.get(field) and len(json_text(controls)) > 28000:
                            controls[field].pop()
                            controls["context_omitted"] = True
                if len(controls["known_candidates"]) > 8 or len(json_text(controls)) > 28000:
                    controls["known_candidates"].pop()
                    controls["known_candidates_omitted"] = True
                    break
                identities.add(row["candidate_id"])
    finally:
        db.close()


def _legacy_identity_window(root: Path, state: dict) -> tuple[list[dict], dict] | None:
    """Keep already frozen rounds on their original bounded retrieval contract."""
    _, read, save, _, _ = _io()
    count = read(root / "identity" / "ready.json")["count"]
    catalog = IdentityCatalog(root / "identity" / "catalog.sqlite3")
    try:
        while state["identity_pass"] < MAX_RETRIEVAL_PASSES:
            row = catalog.at(state["identity_cursor"])
            if row is None:
                state["identity_pass"] += 1
                state["identity_cursor"] = 0
                continue
            after = row["ordinal"] + 1
            if not catalog.is_active(row["candidate_id"]) or not row["selected"]:
                save(root / "identity" / "coverage" / f"{state['identity_pass']}-{row['ordinal']:08d}.json", {
                    "candidate_id": row["candidate_id"], "status": "alias" if row["selected"] else "prior-context",
                })
                state["identity_cursor"] = after
                continue
            rows = [row]
            for related in catalog.related(row["candidate_id"]):
                tentative = rows + [related]
                try:
                    planning.build_stage_packet("identity_group", "groups.json",
                                               controls=_identity_controls(tentative, state),
                                               language_policy=state["language_policy"])
                except ValueError:
                    break
                rows = tentative
            context = {"identity_ids": [item["candidate_id"] for item in rows], "after": after,
                       "identity_pass": state["identity_pass"], "anchor_ordinal": row["ordinal"]}
            _freeze_group_protocol(context, state)
            if len(rows) == 1:
                save(root / "identity" / "coverage" / f"{state['identity_pass']}-{row['ordinal']:08d}.json", {
                    "candidate_id": row["candidate_id"], "status": "no-uncompared-related-identity",
                    "complete_catalog_count": count, "semantic_pairs_exhaustive": False,
                })
                state["identity_cursor"] = after
                continue
            return rows, context
        return None
    finally:
        catalog.close()


def _identity_window(root: Path, state: dict) -> tuple[list[dict], dict] | None:
    """Review fixed top-neighbour relations once per exact version, batching compatible anchors."""
    if state.get("identity_partition_protocol") in SUPPORTED_IDENTITY_PARTITION_PROTOCOLS:
        return _partition_window(root, state)
    if state.get("execution_protocol") != "te-mining-optimized.v1":
        return _legacy_identity_window(root, state)
    _, read, save, _, _ = _io()
    count = read(root / "identity" / "ready.json")["count"]
    catalog = IdentityCatalog(root / "identity" / "catalog.sqlite3")

    def fits(rows: list[dict]) -> bool:
        if len(rows) > 8:
            return False
        try:
            planning.build_stage_packet("identity_group", "groups.json", controls=_identity_controls(rows, state),
                                        language_policy=state["language_policy"])
        except ValueError:
            return False
        return True

    def targets(row: dict) -> list[dict]:
        # Including reviewed neighbours keeps the retrieval set fixed. Filtering
        # them before LIMIT would manufacture a new top-seven set on every pass.
        return catalog.related(row["candidate_id"], include_reviewed=True)

    def covered(row: dict, assigned: set[str]) -> bool:
        return all(other["candidate_id"] in assigned or catalog.reviewed(row, other) for other in targets(row))

    try:
        if "identity_pass_version" not in state:
            state["identity_pass_version"] = catalog.collection_version()
        state.setdefault("identity_passes_scheduled", 1)
        while state["identity_pass"] < MAX_RETRIEVAL_PASSES:
            row = catalog.at(state["identity_cursor"])
            if row is None:
                version = catalog.collection_version()
                if version == state["identity_pass_version"]:
                    state["identity_pass"] = MAX_RETRIEVAL_PASSES
                    return None
                state["identity_pass"] += 1
                state["identity_pass_version"] = version
                state["identity_cursor"] = 0
                state["identity_passes_scheduled"] = min(MAX_RETRIEVAL_PASSES, state["identity_pass"] + 1)
                continue
            after = row["ordinal"] + 1
            coverage_path = root / "identity" / "coverage" / f"{state['identity_pass']}-{row['ordinal']:08d}.json"
            if not catalog.is_active(row["candidate_id"]) or not row["selected"]:
                save(coverage_path, {
                    "candidate_id": row["candidate_id"], "status": "alias" if row["selected"] else "prior-context",
                })
                state["identity_cursor"] = after
                continue
            related = targets(row)
            missing = [other for other in related if not catalog.reviewed(row, other)]
            if not missing:
                save(coverage_path, {
                    "candidate_id": row["candidate_id"], "candidate_version": identity_version(row),
                    "status": "versioned-relations-reviewed" if related else "no-related-identity",
                    "required_related_ids": [other["candidate_id"] for other in related],
                    "complete_catalog_count": count, "semantic_pairs_exhaustive": False,
                })
                state["identity_cursor"] = after
                continue
            rows = [row]
            for other in missing:
                if fits(rows + [other]):
                    rows.append(other)
            if len(rows) == 1:
                raise ValueError("An exact candidate identity pair exceeds the bounded review packet")
            # A small metadata pair leaves room for independent nearby anchors.
            # Each receives its own relation coverage receipt, not an implicit
            # claim that merely appearing in a partition covered every neighbour.
            cursor = after
            for _ in range(7):
                anchor = catalog.at(cursor)
                if anchor is None or len(rows) == 8:
                    break
                cursor = anchor["ordinal"] + 1
                if not catalog.is_active(anchor["candidate_id"]) or not anchor["selected"]:
                    continue
                assigned = {item["candidate_id"] for item in rows}
                required = [other for other in targets(anchor) if not catalog.reviewed(anchor, other)]
                extra = [item for item in [anchor, *required] if item["candidate_id"] not in assigned]
                if required and extra and fits(rows + extra):
                    rows.extend(extra)
            assigned = {item["candidate_id"] for item in rows}
            receipts = []
            for anchor in rows:
                if anchor["selected"] and covered(anchor, assigned):
                    receipts.append({
                        "ordinal": anchor["ordinal"], "candidate_id": anchor["candidate_id"],
                        "candidate_version": identity_version(anchor),
                        "required_related_ids": [other["candidate_id"] for other in targets(anchor)],
                        "status": "model-reviewed", "assigned": [item["candidate_id"] for item in rows],
                        "semantic_pairs_exhaustive": False,
                    })
            context = {
                "identity_ids": [item["candidate_id"] for item in rows],
                "identity_versions": {item["candidate_id"]: identity_version(item) for item in rows},
                "after": after if covered(row, assigned) else row["ordinal"],
                "identity_pass": state["identity_pass"], "anchor_ordinal": row["ordinal"],
                "coverage_receipts": receipts,
            }
            _freeze_group_protocol(context, state)
            return rows, context
        return None
    finally:
        catalog.close()


def _replay_group_journal(root: Path, path: Path):
    _, read, save, _, _ = _io()
    journal = read(path)
    catalog = IdentityCatalog(root / "identity" / "catalog.sqlite3")
    try:
        for row in journal["survivors"]:
            catalog.put(row)
            save(root / "scopes" / (row["candidate"]["name"] + ".json"), row["scope"])
            if row.get("consolidated_members"):
                from .identity_components import PROTOCOL, digest
                save(root / "identity/components" / (row["candidate"]["name"] + ".json"), {
                    "protocol": PROTOCOL, "name": row["candidate"]["name"],
                    "members": row["consolidated_members"], "members_sha256": digest(row["consolidated_members"]),
                })
        for identity in journal["deactivated"]:
            catalog.deactivate(identity)
        catalog.compared(journal["assigned"], versions=journal.get("identity_versions"))
        save(root / "identity" / "aliases.json", journal["aliases"])
        save(root / "plan.json", journal["plan"])
        save(root / "identity" / "coverage" / journal["coverage_file"], journal["coverage"])
        for receipt in journal.get("coverage_receipts", []):
            save(root / "identity" / "coverage" /
                 f"{journal['identity_pass']}-{receipt['ordinal']:08d}.json", receipt)
        save(path.with_suffix(".committed"), {"committed": True})
    finally:
        catalog.close()


def _accept_groups(root: Path, current: dict, value) -> None:
    _, read, save, _, _ = _io()
    context = current["context"]
    protocol = context.get("identity_group_protocol")
    if protocol is not None and protocol != IDENTITY_GROUP_PROTOCOL_V2:
        raise ValueError("Unknown frozen candidate grouping protocol")
    explicit = protocol == IDENTITY_GROUP_PROTOCOL_V2
    partition_protocol = context.get("identity_partition_protocol")
    from .identity_components import PROTOCOL as consolidation_protocol
    consolidate = context.get("identity_consolidation_protocol") == consolidation_protocol
    if context.get("identity_consolidation_protocol") and (
            not consolidate or partition_protocol != CONSOLIDATING_PARTITION_PROTOCOL):
        raise ValueError("Unknown frozen candidate consolidation protocol")
    if partition_protocol == CONSOLIDATING_PARTITION_PROTOCOL and not consolidate:
        raise ValueError("Candidate consolidation policy is missing")
    if partition_protocol is not None:
        if partition_protocol not in SUPPORTED_IDENTITY_PARTITION_PROTOCOLS or not explicit:
            raise ValueError("Unknown frozen candidate identity partition protocol")
        _partition_assignment(root, context)
    groups = validate_identity_groups(value, allowed_ids=context["identity_ids"], allow_survivor=explicit)
    journal_path = root / "identity" / "journals" / (current["child"] + ".json")
    if journal_path.is_file():
        _replay_group_journal(root, journal_path)
        return
    catalog = IdentityCatalog(root / "identity" / "catalog.sqlite3")
    try:
        versions = {identity: identity_version(catalog.get(identity)) for identity in context["identity_ids"]}
        if context.get("identity_versions", versions) != versions:
            raise ValueError("Frozen identity review versions differ from the durable candidate catalog")
        plan = read(root / "plan.json")
        aliases_path = root / "identity" / "aliases.json"
        aliases = read(aliases_path) if aliases_path.is_file() else {}
        survivors, deactivated, host_splits = [], [], []
        requested_groups = copy.deepcopy(groups)
        effective_groups = []

        def group_survivor(group):
            preferred = catalog.survivor(group["candidate_ids"])
            if explicit and (consolidate or not preferred["prior"]):
                return catalog.get(group["survivor_candidate_id"])
            return preferred

        for group in groups:
            survivor = group_survivor(group)
            priors = [identity for identity in group["candidate_ids"] if catalog.get(identity)["prior"]]
            split_reason = None
            if explicit and not consolidate and len(priors) > 1:
                split_reason = "Keep separate identities: distinct previous authoritative identities cannot be replaced"
            elif explicit and not consolidate and priors and group["survivor_candidate_id"] != survivor["candidate_id"]:
                split_reason = "Keep separate identities: the nominee would replace a previous authoritative identity"
            conflicts = [
                identity for identity in group["candidate_ids"]
                if identity != survivor["candidate_id"] and (
                    latest_refinement(root, identity)
                    or any(read(path).get("new_candidate_id") == identity
                           for path in (root / "identity/name-conflicts").glob("*.json"))
                )
                and any(catalog.get(identity)["candidate"][key] != survivor["candidate"][key]
                        for key in BUSINESS_FIELDS)
            ]
            if (conflicts and not consolidate) or split_reason:
                reason = split_reason or (
                    "Keep separate identities: the chosen survivor would discard an accepted business refinement"
                )
                host_splits.append({"candidate_ids": group["candidate_ids"], "refined_candidate_ids": conflicts,
                                    "survivor_candidate_id": survivor["candidate_id"], "reason": reason})
                effective_groups.extend({"candidate_ids": [identity], "reason": reason,
                                         **({"survivor_candidate_id": identity} if explicit else {})}
                                        for identity in group["candidate_ids"])
            else:
                effective_groups.append(group)
        groups = effective_groups
        for group in groups:
            if len(group["candidate_ids"]) == 1:
                continue
            survivor = copy.deepcopy(group_survivor(group))
            members = [catalog.get(identity) for identity in group["candidate_ids"]]
            if consolidate:
                from .identity_components import members_for
                survivor["consolidated_members"] = members_for(members)
            survivor["selected"] = any(row["selected"] for row in members)
            survivor["candidate"]["source_refs"] = sorted({
                ref for row in members for ref in row["candidate"]["source_refs"]
            })
            survivor["scope"] = merge_candidate_scopes(*(row["scope"] for row in members))
            if partition_protocol:
                survivor["identity_partition_origins"] = sorted({
                    origin for row in members for origin in row.get("identity_partition_origins", [])
                })
            names = {row["candidate"]["name"] for row in members}
            for row in members:
                if row["candidate_id"] != survivor["candidate_id"]:
                    deactivated.append(row["candidate_id"])
                    aliases[row["candidate"]["name"]] = {
                        "name": survivor["candidate"]["name"], "candidate_id": survivor["candidate_id"],
                        "reason": group["reason"], "original_candidate_id": row["candidate_id"],
                    }
            positions = [i for i, row in enumerate(plan["skills"]) if row["name"] in names]
            position = min(positions) if positions else len(plan["skills"])
            plan["skills"] = [row for row in plan["skills"] if row["name"] not in names]
            if survivor["selected"]:
                plan["skills"].insert(position, survivor["candidate"])
            survivors.append(survivor)
        # Flatten aliases without replacing the original identity metadata or raw decisions.
        for row in aliases.values():
            seen = set()
            while row["name"] in aliases:
                if row["name"] in seen:
                    raise ValueError("Candidate alias cycle")
                seen.add(row["name"])
                target = aliases[row["name"]]
                row["name"], row["candidate_id"] = target["name"], target["candidate_id"]
        coverage_file = f"{context['identity_pass']}-{context['anchor_ordinal']:08d}.json"
        journal = {
            "assigned": context["identity_ids"], "identity_versions": versions,
            "identity_pass": context["identity_pass"], "coverage_receipts": context.get("coverage_receipts", []),
            "groups": groups, "survivors": survivors,
            "requested_groups": requested_groups, "host_splits": host_splits,
            **({"identity_consolidation_protocol": consolidation_protocol} if consolidate else {}),
            "deactivated": deactivated, "aliases": aliases, "plan": plan,
            "coverage_file": coverage_file,
            "coverage": {"candidate_id": context["identity_ids"][0], "status": "model-reviewed",
                         "assigned": context["identity_ids"], "semantic_pairs_exhaustive": False},
        }
        save(journal_path, journal)
    finally:
        catalog.close()
    _replay_group_journal(root, journal_path)


def _split_audit(root: Path):
    """Stream requested/effective grouping decisions without collecting the whole audit in memory."""
    _, read, _, _, _ = _io()
    for path in sorted((root / "identity" / "journals").glob("*.json")):
        row = read(path)
        if row.get("host_splits"):
            yield json.dumps({"child": path.stem, "requested_groups": row["requested_groups"],
                              "effective_groups": row["groups"], "host_splits": row["host_splits"]},
                             ensure_ascii=False) + "\n"


class CandidateIdentityMixin:
    async def _ensure_aux_compiler(self, root: Path, state: dict, stage: str, body: str):
        _, _, _, sha, write = _io()
        path = root / "aux-compilers" / (stage + ".md")
        hashes = state.setdefault("aux_compilers", {})
        if stage in hashes:
            frozen = await asyncio.to_thread(path.read_text, encoding="utf-8")
            if sha(frozen) != hashes[stage]:
                raise ValueError("Frozen auxiliary compiler changed")
            return
        if path.exists():
            body = await asyncio.to_thread(path.read_text, encoding="utf-8")
        else:
            await asyncio.to_thread(write, path, body)
        hashes[stage] = sha(body)

    async def _staged_identity_enter(self, job: dict, root: Path, state: dict):
        if state.get("current"):
            raise ValueError("Must recover and accept the frozen writer child before identity review")
        state["writer_resume"] = {key: copy.deepcopy(state.get(key)) for key in (
            "phase", "candidate_names", "candidate_index", "cursor", "section_count", "case_index",
            "case_group_attempt",
        )}
        state.update(phase="evidence_review", identity_protocol=IDENTITY_PROTOCOL)
        return await self._staged_save(job, root, state)

    async def _staged_identity_next(self, job: dict, root: Path, state: dict):
        _, read, save, _, _ = _io()
        await asyncio.to_thread(_seed_catalog, job, root, "identity", include_reviews=True)
        # Replaying a prepared transaction is idempotent, even after a late filesystem failure.
        for path in sorted((root / "identity" / "journals").glob("*.json")):
            if not path.with_suffix(".committed").is_file():
                await asyncio.to_thread(_replay_group_journal, root, path)
        partitioning = state.get("identity_partition_protocol") in SUPPORTED_IDENTITY_PARTITION_PROTOCOLS
        if partitioning:
            base = await asyncio.to_thread((root / "compiler-base.md").read_text, encoding="utf-8")
            compiler = render_identity_partition_compiler(
                base, protocol=state["identity_partition_protocol"]
            )
            await self._ensure_aux_compiler(root, state, "identity_group", compiler)
            from .ov_staged_orchestration import _package

            compiler = await asyncio.to_thread(
                (root / "aux-compilers" / "identity_group.md").read_text, encoding="utf-8"
            )
            published = _package(compiler)[1]
            packet_budget = min(32000, 48000 - len(published) - 3072 - 2000)
            if packet_budget < 1000:
                raise ValueError("The frozen identity compiler leaves no bounded business metadata budget")
            previous_budget = state.setdefault("identity_partition_packet_budget", packet_budget)
            if previous_budget != packet_budget:
                raise ValueError("Frozen identity partition compiler budget changed")
        if "identity_pass" not in state:
            state.update(identity_pass=0, identity_cursor=0)
            count = (await asyncio.to_thread(read, root / "identity" / "ready.json"))["count"]
            if not partitioning:
                state["estimated_tasks"] += count * MAX_RETRIEVAL_PASSES
        window = await asyncio.to_thread(_identity_window, root, state)
        if partitioning:
            estimated = sum(v["batch_count"] for v in state.get("identity_partition_levels", {}).values())
            previous = state.get("identity_partition_estimated_tasks", 0)
            state["estimated_tasks"] += max(0, estimated - previous)
            state["identity_partition_estimated_tasks"] = max(estimated, previous)
        if window is None:
            return await self._staged_identity_finish(job, root, state)
        rows, context = window
        base = await asyncio.to_thread((root / "compiler-base.md").read_text, encoding="utf-8")
        if partitioning:
            compiler = render_identity_partition_compiler(base)
        elif _explicit_survivor(state):
            from .compile_strategy import render_workflow_identity_compiler

            compiler = render_workflow_identity_compiler(base)
        else:
            rule = GROUP_RULE
            from .workflow_planning import WORKFLOW_PLANNING_PROTOCOL, WORKFLOW_PLANNING_RULE
            if state.get("workflow_planning_protocol") == WORKFLOW_PLANNING_PROTOCOL:
                rule += "\n" + WORKFLOW_PLANNING_RULE
            compiler = render_single_stage_compiler(base, "identity_group", rule)
        await self._ensure_aux_compiler(root, state, "identity_group", compiler)
        await asyncio.to_thread(save, root / "state.json", state)
        return await self._staged_submit(job, root, state, "identity_group", "groups.json", [],
                                         _identity_controls(rows, state), context)

    async def _staged_identity_accept(self, job: dict, root: Path, state: dict, current: dict, value):
        if (current["context"].get("identity_partition_protocol")
                != state.get("identity_partition_protocol")):
            raise ValueError("Frozen identity partition protocol changed")
        await asyncio.to_thread(_accept_groups, root, current, value)
        state["identity_cursor"] = current["context"]["after"]
        if current["context"].get("identity_partition_protocol") in SUPPORTED_IDENTITY_PARTITION_PROTOCOLS:
            state["identity_partition_cursor"] = current["context"]["after"]

    async def _staged_identity_finish(self, job: dict, root: Path, state: dict):
        _, read, save, _, _ = _io()
        plan = await asyncio.to_thread(read, root / "plan.json")
        aliases_path = root / "identity" / "aliases.json"
        aliases = await asyncio.to_thread(read, aliases_path) if aliases_path.exists() else {}
        resume = state["writer_resume"]
        old_names = resume.get("candidate_names") or []
        index = int(resume.get("candidate_index") or 0)
        old_name = old_names[index] if index < len(old_names) else ""
        names = [row["name"] for row in plan["skills"]]
        state.pop("writer_pending_ordinals", None)
        state.update(identity_review_complete=True, candidate_names=names, candidate_index=0,
                     phase="skill_section", cursor=0, section_count=0, case_index=0)
        # Already accepted output remains valid only under its surviving canonical identity.
        # Raw child outputs and alias sections remain archival even when regeneration is required.
        surviving_name = aliases.get(old_name, {}).get("name", old_name)
        if surviving_name in names:
            # Resume the accepted current identity first, then cover every other active identity.
            state["candidate_names"] = [surviving_name] + [name for name in names if name != surviving_name]
            if surviving_name != old_name:
                for folder in ("sections", "section-groups"):
                    for i in range(int(resume.get("section_count") or 0)):
                        value = await asyncio.to_thread(read, root / folder / old_name / f"{i:08d}.json")
                        await asyncio.to_thread(save, root / folder / surviving_name / f"{i:08d}.json", value)
            scope = await asyncio.to_thread(read, root / "scopes" / (surviving_name + ".json"))
            covered = set()
            for i in range(int(resume.get("section_count") or 0)):
                group = await asyncio.to_thread(read, root / "section-groups" / surviving_name / f"{i:08d}.json")
                covered.update(group["ordinals"])
            state["section_count"] = int(resume.get("section_count") or 0)
            if covered <= set(scope["ordinals"]):
                pending = [item for item in scope["ordinals"] if item not in covered]
                prefix = scope["ordinals"][:len(covered)]
                if set(prefix) == covered:
                    state["writer_pending_ordinals"] = scope["ordinals"]
                    state["cursor"] = len(covered)
                else:
                    state["writer_pending_ordinals"] = pending
                # Subsequent evaluation/Cases are regenerated when the canonical identity or scope changes.
                if not pending and old_name == surviving_name:
                    state.update(phase=resume["phase"], cursor=int(resume.get("cursor") or 0),
                                 case_index=int(resume.get("case_index") or 0),
                                 case_group_attempt=int(resume.get("case_group_attempt") or 0))
                    if state["phase"] != "skill_section":
                        state.pop("writer_pending_ordinals", None)
            else:
                state.update(section_count=0, cursor=0)
        from .ov_staged_orchestration import _group_count
        writer_work = 0
        for name in names:
            scope = await asyncio.to_thread(read, root / "scopes" / (name + ".json"))
            writer_work += 13 + await asyncio.to_thread(_group_count, root, scope["ordinals"])
        state["estimated_tasks"] = max(state["estimated_tasks"], state["completed_tasks"] + writer_work + 1)
        summary = {
            "protocol": IDENTITY_PROTOCOL, "retrieval_passes": MAX_RETRIEVAL_PASSES,
            "max_identities_per_call": 8, "semantic_pairs_exhaustive": False,
            "aliases": aliases, "selected_candidate_count": len(names),
            "raw_dispositions_preserved": True, "retrieval": "global disk-backed token/source inverted index",
            **({"review_policy": "fixed top-seven relations per exact candidate version",
                "candidate_version_receipts": True, "unchanged_collection_skips": True,
                "passes_scheduled": state.get("identity_passes_scheduled", 1)}
               if state.get("execution_protocol") == "te-mining-optimized.v1" else {}),
        }
        partitioning = state.get("identity_partition_protocol") in SUPPORTED_IDENTITY_PARTITION_PROTOCOLS
        if partitioning:
            summary.update(
                partition_protocol=state["identity_partition_protocol"],
                max_identities_per_call=MAX_PARTITION_IDENTITIES,
                review_policy="nonoverlapping complete budget partitions; at most one cross-batch workflow layer",
                retrieval="workflow tokens only for the optional cross-batch layer; no source-overlap graph",
                partition_levels=state.get("identity_partition_levels", {}),
                candidate_version_receipts=True,
            )
            summary.pop("passes_scheduled", None)
        await asyncio.to_thread(save, root / "identity" / "summary.json", summary)
        repository = self._ov_skill_mining_repository()
        for alias in aliases:
            source = repository.workspace(job) / "compiled_skill" / alias
            if source.is_dir():
                destination = root / "identity" / "archived-output" / alias
                await asyncio.to_thread(destination.parent.mkdir, parents=True, exist_ok=True)
                await asyncio.to_thread(os.replace, source, destination)
        report = {
            "protocol": IDENTITY_PROTOCOL, "retrieval_passes": MAX_RETRIEVAL_PASSES,
            "max_identities_per_call": 8, "semantic_pairs_exhaustive": False,
            "selected_candidate_count": len(names), "alias_count": len(aliases),
            "raw_dispositions_preserved": True,
            **({"review_policy": "fixed top-seven relations per exact candidate version",
                "candidate_version_receipts": True, "unchanged_collection_skips": True,
                "passes_scheduled": state.get("identity_passes_scheduled", 1)}
               if state.get("execution_protocol") == "te-mining-optimized.v1" else {}),
        }
        if partitioning:
            report.update({key: summary[key] for key in (
                "partition_protocol", "max_identities_per_call", "review_policy", "retrieval", "partition_levels",
            )})
            report.pop("passes_scheduled", None)
        await asyncio.to_thread(repository.write_output, job["job_id"], "analysis/candidate-identities.json",
                                json.dumps(report, ensure_ascii=False))
        conflict_paths = sorted((root / "identity/name-conflicts").glob("*.json"))
        for index, path in enumerate(conflict_paths):
            await asyncio.to_thread(repository.write_output, job["job_id"],
                                    f"analysis/name-conflicts/{index + 1:04d}.json",
                                    json.dumps(await asyncio.to_thread(read, path), ensure_ascii=False))
        split_count = await asyncio.to_thread(lambda: sum(1 for _ in _split_audit(root)))
        if split_count:
            await asyncio.to_thread(repository.write_output_stream, job["job_id"],
                                    "analysis/identity-split-audit.jsonl", _split_audit(root))
        chinese = state["language_policy"].get("language") == "zh-CN"
        text = "# Candidate 身份复核\n\n" if chinese else "# Candidate identity review\n\n"
        if partitioning:
            text += (
                "按完整业务身份与实际预算审查不重叠集合，每个身份及版本在一层内只出现一次。"
                "跨批最多追加一层业务词摘要复核，不以共同来源反复扫描；未承诺穷尽所有语义组合。\n\n"
                if chinese else "Complete business identities are reviewed in nonoverlapping budget partitions, "
                "each identity and version once per layer. At most one additional workflow-based cross-batch "
                "layer is used; common sources do not trigger repeated scans. Semantic pairs are not exhaustive.\n\n"
            )
        else:
            text += (
            "从完整本地候选索引检索相似身份，最多两遍、每窗最多八个；未承诺穷尽所有语义组合。\n\n"
            if chinese else "Related identities are retrieved from the complete local catalog in at most two passes, "
            "with at most eight identities per window. This does not exhaust every semantic pair.\n\n"
        )
        if state.get("execution_protocol") == "te-mining-optimized.v1":
            text += (
                "每个候选版本检索最多七个相似身份；批次可覆盖多个候选。已复核的相同版本关系会复用回执，"
                "只有身份合并、业务字段或来源范围变化后才需要下一遍复核。未检索关系不代表已通过。\n\n"
                if chinese else "Each candidate version retrieves at most seven related identities. A batch may "
                "cover several anchors. Exact version receipts are reused; another pass is needed only after "
                "grouping, business metadata or source scope changes. Unretrieved relations are not approved.\n\n"
            )
        if split_count:
            text += (
                "含不同已接受业务修订的身份合并请求已保守拆开，保留各自身份与案例编号。"
                "详见 [请求与实际分组审计](identity-split-audit.jsonl)。\n\n"
                if chinese else "Grouping requests that would discard a different accepted business revision "
                "were conservatively split, preserving their identities and Case IDs. "
                "See [requested and effective groups](identity-split-audit.jsonl).\n\n"
            )
        text += (
            "原始来源、逐方法谱系及未改写的模型输出保存在本轮审计中。\n\n"
            if chinese else "Original sources, method lineage and unchanged model outputs "
            "remain in this round's audit.\n\n"
        )
        for index in range(0, len(aliases), 64):
            rows = list(aliases.items())[index:index + 64]
            page = f"analysis/identities/page-{index // 64 + 1:04d}.md"
            title = "# Candidate 身份合并原因\n\n" if chinese else "# Candidate identity grouping reasons\n\n"
            content = title + "\n\n".join(
                f"{name} → {row['name']}\n\n{row['reason']}" for name, row in rows
            )
            await asyncio.to_thread(repository.write_output, job["job_id"], page, content)
            text += f"[{index // 64 + 1}](identities/page-{index // 64 + 1:04d}.md)\n\n"
        await asyncio.to_thread(repository.write_output, job["job_id"], "analysis/candidate-identities.md", text)
        reviews = sorted((root / "evidence-review" / "results").glob("*.json"))
        for offset in range(0, len(reviews), 64):
            page = f"analysis/evidence-review/page-{offset // 64 + 1:04d}"
            rows = []
            for path in reviews[offset:offset + 64]:
                row = await asyncio.to_thread(read, path)
                rows.append({key: row[key] for key in (
                    "item_id", "proposal_index", "leaf_items", "outcome", "reason", "source_refs", "previous_gap",
                )})
            await asyncio.to_thread(repository.write_output, job["job_id"], page + ".json",
                                    json.dumps({"items": rows}, ensure_ascii=False))
            heading = "# 原文复核前后去向\n\n" if chinese else "# Original Evidence review dispositions\n\n"
            content = heading + "\n\n".join(
                f"{row['item_id']}: deferred → {row['outcome']}\n\n"
                f"{row['previous_gap']['reason']}\n\n{row['reason']}" for row in rows
            )
            await asyncio.to_thread(repository.write_output, job["job_id"], page + ".md", content)
        job = await self._staged_save(job, root, state)
        return await self._staged_next(job, root, state)
