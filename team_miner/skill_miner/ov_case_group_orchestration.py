"""Fresh, bounded groups of independently validated Cases over public file Map."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from starlette.exceptions import HTTPException

from . import bounded_compile_plan as planning
from .compile_strategy import (
    MAX_FILE_MAP_PACKETS,
    MINING_POLICY_SCHEMA,
    MULTI_COMPILE_PLAN,
    STAGE_RULES,
    compiler_business_policy,
    multi_compile_contract,
)
from .output_language import output_language_instruction

CASE_GROUP_PROTOCOL = "te-case-groups.v1"
CASE_GROUP_SCHEMA = "skill-miner.case-group.v1"
CASE_GROUP_CAPACITY_SCHEMA = "skill-miner.case-group-capacity.v1"
CASE_GROUP_COMPILER_STAGE = "case_group_batch"
MAX_CASES_PER_GROUP = 4
MAX_GROUP_OUTPUT_CHARS = MAX_CASES_PER_GROUP * planning.MAX_OUTPUT_CHARS + 1024


class FrozenCaseGroupError(ValueError):
    """Integrity failures are not model capacity or formatting repairs."""


def render_case_group_compiler(base_body: str, max_packets: int = MAX_FILE_MAP_PACKETS, *,
                               source_bound_context: bool = False) -> str:
    """Derive only this fresh compiler; old renderers and frozen bodies stay unchanged."""
    if type(max_packets) is not int or not 1 <= max_packets <= MAX_FILE_MAP_PACKETS:
        raise ValueError("A TE Case group Compile supports one to eight packet files")
    policy = compiler_business_policy(base_body)
    contract = multi_compile_contract()
    # Rewrite only owned transport and stage constants, before appending any user policy text.
    transport = contract["synthesize"]["instructions"].replace(
        "Each Compile reads one TE-owned bounded packet file.",
        f"One Compile has one to {max_packets} TE-owned bounded packet files. With input_unit=file, "
        "each Map reads only its own packet's supplied native ranges and generates only its Case group.",
        1,
    )
    case_rules = STAGE_RULES["case"]
    if source_bound_context:
        from .case_context import source_bound_case_rules

        case_rules = source_bound_case_rules(case_rules)
    replacements = {
        "Return only the current complete canonical case JSON, never a dataset or sibling cases.":
            "Return one bounded group containing only this packet's assigned complete canonical Cases.",
        "assigned case_id and skill_name from controls;":
            "derived_controls.cases[i].case_id and the shared derived_controls.skill_name;",
        "derived_controls.coverage_dimension": "each assignment's coverage_dimension",
    }
    for old, new in replacements.items():
        case_rules = case_rules.replace(old, new)
    case_rules = case_rules.replace(
        "original source_uri values from THIS raw Evidence group.",
        "original source_uri values from THIS Case assignment's own raw Evidence, not the packet union.",
    ).replace(
        "return the explicit deferred object instead.",
        "place that Case's exact deferred row in the envelope's deferred array instead.",
    )
    custom = "".join(
        "\n\nCustom policy for " + key + ":\n" + policy["stages"][key]
        for key in ("case", "case_batch", CASE_GROUP_COMPILER_STAGE) if key in policy["stages"]
    )
    rules = (
        transport + "\n\nAuthoritative mining business policy (" + MINING_POLICY_SCHEMA + "):\n"
        + policy["common"] + custom + "\n\nCurrent per-file stage: case.\n" + case_rules
        + " Each deferred assignment row has exactly case_id, reason (nonempty text, at most 2000 "
        "characters) and source_refs (a nonempty list of unique exact assigned original URIs). "
        "Use it only for unsupported scenarios, never padding."
    )
    rules += (
        "\n\nAuthoritative fresh Case group envelope: exactly schema_version, cases, deferred. "
        "schema_version is 'skill-miner.case-group.v1'; cases is a list of complete canonical Case objects; "
        "deferred is a list of exact {case_id,reason,source_refs} rows. Each assigned case_id appears "
        "exactly once in one of those lists, with no unassigned IDs. There are one to four assignments "
        "in derived_controls.cases. Do not share a query or Checklist between assignments or pad the count. "
        "Each Case may cite ONLY that assignment's allowed_source_refs and use ONLY its input_ids from "
        "this packet's supplied original Evidence. The union of packet Evidence is not permission to "
        "use another assignment's sources. Preserve every assignment's own coverage_dimension. "
        "Compare queries within this group and against previous_queries; changing an ID is not a distinct scenario. "
        "The complete group content is at most 49024 characters, and each Case JSON still at most 12000. "
        "If the complete group cannot fit, return only the explicit capacity object with exactly "
        "schema_version='skill-miner.case-group-capacity.v1', case_ids (all assigned IDs exactly once), "
        "source_refs (the exact union of assigned original URIs) and reason (nonempty, at most 1000 "
        "characters). Capacity is allowed only for multiple assignments; do not call a format problem "
        "a missing-Evidence gap. TE will split only the affected group. All schemas are inline contracts, "
        "not relative skill resources. Packet text is already supplied in native inputs[].payload.text; "
        "do not re-read packet/schema resources. Emit one FileResponse file at packet.output_path, "
        "whose content is the group JSON string. FileResponse.inputs uses only this file job's supplied "
        "native range IDs; inner Case provenance uses only each assignment's original source URIs. "
        "Native completion may be partial; TE validates and commits each Case separately."
    )
    if source_bound_context:
        rules = rules.replace("Preserve every assignment's own coverage_dimension.",
                              "Retain each assignment's coverage preference; it is not a required domain condition.")
        rules = rules.replace("Compare queries within this group and against previous_queries;",
                              "Compare queries within this group; the host checks whole previous queries locally;")
    contract["synthesize"]["instructions"] = rules
    return (
        "---\nname: skill-miner-te-case-groups\n"
        "description: Produce bounded groups of independently validated TE Cases through file Map and Finalize.\n"
        "---\n\n"
        "# Independent TE Case groups\n\n"
        f"This Compile reads at most {max_packets} packet files. Each file Map reads only its own supplied "
        "packet and emits only packet.output_path. Each inner Case retains its own exact source assignment. "
        "The synthesize instructions contain the complete business policy and Case contract. This compiler "
        "has no auxiliary resources or scripts. Use only this existing public OV plan.\n\n"
        "```python\n" + MULTI_COMPILE_PLAN + "\n```\n\n"
        "```json\n" + json.dumps(contract, ensure_ascii=False, indent=2) + "\n```\n"
    )


def group_instruction(policy: dict) -> str:
    return (
        "Map each packet file independently to its own output_path containing one bounded Case group. "
        "Honor each assignment's own original Evidence whitelist; never use another assignment's sources. "
        "Finalize the separate files. " + output_language_instruction(policy)
    )


def _host():
    from .ov_case_batch_orchestration import _host as host

    return host()


def _group_packet(root: Path, state: dict, candidate: dict, assignments: list[tuple], published: str):
    from .case_context import case_history_controls, compact_history_controls
    from .ov_case_batch_orchestration import _bounded_case_revision

    _, _, json_text, _, _, _, _, _, _, _ = _host()
    # A prompt whitelist cannot isolate sibling source facts inside one model context. Only
    # assignments with identical raw Evidence share a Map; distinct scopes get separate files.
    if len({tuple(context["input_ids"]) for _, context, _ in assignments}) != 1:
        raise ValueError("Different Case source scopes need a smaller lossless source assignment")
    chunks, ids = [], set()
    for _, _, own_chunks in assignments:
        for chunk in own_chunks:
            if chunk["id"] not in ids:
                chunks.append(chunk)
                ids.add(chunk["id"])
    if len(chunks) > 8 or len(json_text(chunks)) > 26000:
        raise ValueError("Case group original Evidence exceeds its lossless union budget")
    contexts = [context for _, context, _ in assignments]
    controls = {
        "candidate": {key: candidate[key] for key in ("name", "task", "boundary", "trigger")},
        "skill_name": candidate["name"],
        "cases": [{
            "case_id": context["case_id"], "input_ids": context["input_ids"],
            "method_input_ids": context["method_input_ids"],
            "context_input_ids": context["context_input_ids"],
            "allowed_source_refs": context["source_refs"],
            "coverage_dimension": planning.CASE_DIMENSIONS[context["case_index"]],
            **({"repair_error": slot["error"][:1000]} if slot["error"] else {}),
        } for slot, context, _ in assignments],
        **state.get("case_group_history_controls", case_history_controls([])),
        "source_scope_rule": "Each Case uses only its own assignment's exact original Evidence, not the union.",
    }
    first = contexts[0]["case_index"]
    # Only read-only prior query projections may shrink; original Evidence and all assignment IDs are exact.
    while True:
        try:
            packet = planning.build_stage_packet(
                "case", f"case-group-output-{first:02d}.json", evidence_chunks=chunks, controls=controls,
                language_policy=state["language_policy"],
            )
            context = {
                "skill_name": candidate["name"], "case_index": first, "case_slots": contexts,
                "input_ids": [chunk["id"] for chunk in chunks],
                "source_refs": list(dict.fromkeys(chunk["source_uri"] for chunk in chunks)),
            }
            packet = _bounded_case_revision(
                root, packet, context, published, group_instruction(state["language_policy"])
            )
            return packet, context
        except ValueError as exc:
            if "budget" not in str(exc) and "exceeds" not in str(exc) and "smaller lossless" not in str(exc):
                raise
            queries = controls["previous_queries"]
            if not queries:
                if compact_history_controls(controls):
                    continue
                raise
            controls["previous_queries"] = [query[:len(query) // 2] for query in queries] if any(queries) else []
            controls["previous_queries_excerpted"] = True


async def next_case_groups(owner, job: dict, root: Path, state: dict, candidate: dict, scope: dict):
    from .case_context import CASE_CONTEXT_GROUP_STAGE
    from .ov_case_batch_orchestration import MAX_CASE_PACKETS, _batch_ledger, case_source_roles

    _, chunks_at, _, method_groups, package, prior_rows, read, save, sha, _ = _host()
    name = candidate["name"]
    prior = await asyncio.to_thread(lambda: next(prior_rows(root, name=name), None))
    prior_ids = prior["case_ids"] if prior else []
    ids = prior_ids if len(prior_ids) == 12 else [f"{name}-case-{index + 1:02d}" for index in range(12)]
    ledger = await asyncio.to_thread(_batch_ledger, root, name, ids, state["case_index"])
    pending = [slot for slot in ledger["slots"] if slot["status"] != "valid"]
    if not pending or any(slot.get("group_limit") == 1 for slot in pending):
        return await owner._staged_case_batch_next(job, root, state, candidate, scope, force_single_cases=True)
    groups = await asyncio.to_thread(method_groups, root, scope)
    base = await asyncio.to_thread((root / "compiler-base.md").read_text, encoding="utf-8")
    if sha(base) != state["base_sha256"]:
        raise ValueError("Frozen base compiler changed")
    await owner._ensure_aux_compiler(root, state, CASE_CONTEXT_GROUP_STAGE,
                                     render_case_group_compiler(base, source_bound_context=True))
    _, published = package(await asyncio.to_thread(
        (root / "aux-compilers" / (CASE_CONTEXT_GROUP_STAGE + ".md")).read_text, encoding="utf-8"
    ))
    previous = [await asyncio.to_thread(read, root / "cases" / name / f"{slot['index']:02d}.json")
                for slot in ledger["slots"] if slot["status"] == "valid"]
    # These transient projections are not persisted in state; the full accepted Cases remain local.
    from .case_context import case_history_controls

    packet_state = {**state, "case_group_history_controls": case_history_controls(previous)}
    assignments = []
    for slot in pending:
        index = slot["index"]
        group_index = (index * len(groups) // 12 + slot["group_attempt"]) % len(groups)
        chunks = await asyncio.to_thread(chunks_at, root, groups[group_index])
        context = {
            "skill_name": name, "case_id": slot["case_id"], "case_index": index,
            "case_group_index": group_index, "case_group_count": len(groups),
            "input_ids": [chunk["id"] for chunk in chunks],
            **case_source_roles(chunks, scope, root=root, group_index=group_index),
            "source_refs": list(dict.fromkeys(chunk["source_uri"] for chunk in chunks)),
            "format_attempts": slot["format_attempts"], "group_attempt": slot["group_attempt"],
        }
        assignments.append((slot, context, chunks))
    packets, contexts, cursor = [], [], 0
    while cursor < len(assignments) and len(packets) < MAX_CASE_PACKETS:
        width = min(MAX_CASES_PER_GROUP, len(assignments) - cursor,
                    assignments[cursor][0].get("group_limit", MAX_CASES_PER_GROUP))
        while width:
            group = assignments[cursor:cursor + width]
            if any(slot.get("group_limit", 4) < width for slot, _, _ in group):
                width -= 1
                continue
            try:
                packet, context = await asyncio.to_thread(
                    _group_packet, root, packet_state, candidate, group, published
                )
                break
            except ValueError as exc:
                if "budget" not in str(exc) and "exceeds" not in str(exc) and "smaller lossless" not in str(exc):
                    raise
                width -= 1
        if not width:
            assignments[cursor][0]["group_limit"] = 1
            await asyncio.to_thread(save, root / "case-batches" / name / "ledger.json", ledger)
            return await owner._staged_case_batch_next(job, root, state, candidate, scope, force_single_cases=True)
        packets.append(packet)
        contexts.append(context)
        cursor += width
    return await owner._submit_case_batch(
        job, root, state, packets, contexts, compiler_stage=CASE_CONTEXT_GROUP_STAGE,
        input_protocol=CASE_GROUP_PROTOCOL, instruction=group_instruction(state["language_policy"]),
    )


def verified_group_batch(root: Path, current: dict, ledger: dict | None = None):
    from .ov_case_batch_orchestration import case_source_roles

    _, chunks_at, json_text, method_groups, package, _, read, _, sha, _ = _host()
    request = read(root / current["request_file"])
    folder = (root / "children" / current["child"]).resolve()
    paths = [root / current[key] for key in ("compiler_file", "packet_file", "request_file")]
    if any(path.is_symlink() or path.resolve().parent != folder for path in paths):
        raise ValueError("Frozen Case group files escape their private child directory")
    body, manifest = paths[0].read_text(encoding="utf-8"), paths[1].read_text(encoding="utf-8")
    if (sha(json_text(request)) != current["request_sha256"] or sha(body) != current["compiler_sha256"]
            or sha(manifest) != current["packet_sha256"]):
        raise ValueError("Frozen Case group request changed")
    state = read(root / "state.json")
    from .case_context import CASE_CONTEXT_GROUP_STAGE

    compiler_stage = current.get("compiler_stage", CASE_GROUP_COMPILER_STAGE)
    if compiler_stage not in {CASE_GROUP_COMPILER_STAGE, CASE_CONTEXT_GROUP_STAGE}:
        raise ValueError("Frozen Case group compiler stage changed")
    source_path = root / "aux-compilers" / (compiler_stage + ".md")
    if source_path.is_symlink():
        raise ValueError("Frozen Case group compiler cannot be a symbolic link")
    source = source_path.read_text(encoding="utf-8")
    compiler_name, published = package(source)
    if (state.get("case_group_protocol") != CASE_GROUP_PROTOCOL
            or sha(source) != state["aux_compilers"][compiler_stage]
            or sha(source) != current["compiler_source_sha256"] or published != body
            or request["skill"] != "viking://agent/skills/" + compiler_name):
        raise ValueError("Frozen Case group compiler or protocol changed")
    locators = json.loads(manifest)
    if not isinstance(locators, list) or not 1 <= len(locators) <= MAX_FILE_MAP_PACKETS:
        raise ValueError("Case groups require one to eight independent packet files")
    name = current["context"]["skill_name"]
    candidate = next((row for row in read(root / "plan.json")["skills"] if row["name"] == name), None)
    if candidate is None:
        raise ValueError("Case group Candidate is absent from its accepted planning ledger")
    scope = read(root / "scopes" / (name + ".json"))
    groups = method_groups(root, scope)
    if ledger is None:
        ledger = read(root / "case-batches" / name / "ledger.json")
    if (not isinstance(ledger, dict) or len(ledger["case_ids"]) != 12 or len(set(ledger["case_ids"])) != 12
            or len(ledger["slots"]) != 12):
        raise ValueError("Case groups retain twelve exact unique Case identities")
    if request["from"] != [current["input_uri"]] or request["to"] != current["output_uri"]:
        raise ValueError("Frozen Case group directory or destination changed")
    seen, output_paths, texts = set(), set(), []
    for row in locators:
        context = row["context"]
        assignments = context["case_slots"]
        if not isinstance(assignments, list) or not 1 <= len(assignments) <= 4:
            raise ValueError("Case group requires one to four exact owned Case assignments")
        index = assignments[0]["case_index"]
        path = root / row["local_path"]
        if path.is_symlink() or path.resolve().parent != folder:
            raise ValueError("Frozen Case group packet escapes its private child directory")
        text = path.read_text(encoding="utf-8")
        if (sha(text) != row["sha256"] or len(text) > 32000
                or len(body) + len(text) + len(request["instruction"]) > 48000):
            raise ValueError("Frozen Case group packet changed or exceeds its bounded envelope")
        packet = json.loads(text)
        controls, chunks = packet["derived_controls"], packet["evidence"]
        planning.validate_chunks(chunks)
        if len(chunks) > 8 or len(json_text(chunks)) > 26000:
            raise ValueError("Case group original Evidence exceeds its lossless union budget")
        if (packet["stage"] != "case" or context["skill_name"] != name or context["case_index"] != index
                or controls["skill_name"] != name or len(controls["cases"]) != len(assignments)
                or controls["candidate"] != {key: candidate[key] for key in ("name", "task", "boundary", "trigger")}
                or packet["output_path"] != f"case-group-output-{index:02d}.json"
                or row["output_path"] != packet["output_path"] or row["output_path"] in output_paths
                or row["uri"] != current["input_uri"] + f"/packet-{index:02d}.json"):
            raise ValueError("Frozen Case group output or Candidate assignment changed")
        union, union_ids = [], set()
        if len({tuple(own["input_ids"]) for own in assignments}) != 1:
            raise ValueError("Independent Case Evidence scopes cannot share a file Map")
        for own, control in zip(assignments, controls["cases"], strict=True):
            ordinal = own["case_index"]
            if type(ordinal) is not int or not 0 <= ordinal < 12 or ordinal in seen:
                raise ValueError("Case groups must assign each exact ordinal once")
            slot = ledger["slots"][ordinal]
            if (any(type(own[key]) is not int or own[key] < 0 for key in ("group_attempt", "format_attempts"))
                    or type(own["case_group_count"]) is not int or type(own["case_group_index"]) is not int):
                raise ValueError("Case group retries and source group assignments must retain exact integer counters")
            group_index = (ordinal * len(groups) // 12 + own["group_attempt"]) % len(groups)
            original = chunks_at(root, groups[group_index])
            original_ids = [chunk["id"] for chunk in original]
            original_refs = list(dict.fromkeys(chunk["source_uri"] for chunk in original))
            roles = case_source_roles(original, scope, root=root, group_index=group_index)
            if any(control.get(key) != ids or own.get(key) != ids for key, ids in roles.items()):
                raise ValueError("Case group changed its method/context Evidence roles")
            if (slot["index"] != ordinal or slot["case_id"] != own["case_id"]
                    or ledger["case_ids"][ordinal] != own["case_id"] or own["skill_name"] != name
                    or own["case_group_count"] != len(groups) or own["case_group_index"] != group_index
                    or own["input_ids"] != original_ids or own["source_refs"] != original_refs
                    or control["case_id"] != own["case_id"] or control["input_ids"] != original_ids
                    or control["allowed_source_refs"] != original_refs
                    or control["coverage_dimension"] != planning.CASE_DIMENSIONS[ordinal]
                    or slot["status"] not in {"pending", "valid"}):
                raise ValueError("Case group changed an individual Case's exact original Evidence or identity")
            for chunk in original:
                if chunk["id"] not in union_ids:
                    union.append(chunk)
                    union_ids.add(chunk["id"])
            seen.add(ordinal)
        if (chunks != union or packet["input_ids"] != [chunk["id"] for chunk in union]
                or context["input_ids"] != packet["input_ids"]
                or context["source_refs"] != list(dict.fromkeys(chunk["source_uri"] for chunk in union))):
            raise ValueError("Case group union differs from its independent original assignments")
        output_paths.add(row["output_path"])
        texts.append(text)
    return request, body, locators, texts


def decode_group(value: object, context: dict) -> dict[int, tuple[str, str, bool]]:
    """Project a bounded envelope, without relaxing any individual Case validation."""
    _, _, json_text, _, _, _, _, _, _, _ = _host()
    assignments = context["case_slots"]
    ids = [own["case_id"] for own in assignments]
    if not isinstance(value, dict):
        raise ValueError("Case group output requires an object")
    if value.get("schema_version") == CASE_GROUP_CAPACITY_SCHEMA:
        if (set(value) != {"schema_version", "case_ids", "source_refs", "reason"} or len(ids) <= 1
                or not isinstance(value["case_ids"], list) or len(value["case_ids"]) != len(ids)
                or set(value["case_ids"]) != set(ids) or not isinstance(value["source_refs"], list)
                or len(value["source_refs"]) != len(context["source_refs"])
                or set(value["source_refs"]) != set(context["source_refs"])
                or not isinstance(value["reason"], str) or not value["reason"].strip() or len(value["reason"]) > 1000):
            raise ValueError("Case group capacity marker must retain every exact assigned identity and source")
        return {own["case_index"]: ("", value["reason"], True) for own in assignments}
    if (set(value) != {"schema_version", "cases", "deferred"} or value["schema_version"] != CASE_GROUP_SCHEMA
            or not isinstance(value["cases"], list) or not isinstance(value["deferred"], list)
            or len(value["cases"]) + len(value["deferred"]) > 8):
        raise ValueError("Case group requires exact schema_version, cases and deferred lists")
    rows = {}
    duplicates = set()
    for deferred, values in ((False, value["cases"]), (True, value["deferred"])):
        for row in values:
            if not isinstance(row, dict) or row.get("case_id") not in ids:
                raise ValueError("Case group contains an unknown Case identity")
            identity = row["case_id"]
            if identity in rows:
                duplicates.add(identity)
            if deferred:
                if set(row) != {"case_id", "reason", "source_refs"}:
                    rows[identity] = ("", "Deferred Case row requires exact case_id, reason and source_refs", False)
                    continue
                row = {"schema_version": planning.DEFERRED_SCHEMA, "reason": row["reason"],
                       "source_refs": row["source_refs"]}
            text = json_text(row)
            rows[identity] = (
                text, "" if len(text) <= planning.MAX_OUTPUT_CHARS else "Individual Case exceeds 12K", False
            )
    return {own["case_index"]: (("", "Case identity appears more than once in its group", False)
                               if own["case_id"] in duplicates else rows.get(
                                   own["case_id"], ("", "Assigned Case is missing from its group", False)))
            for own in assignments}


async def read_group_outputs(owner, job: dict, root: Path, current: dict, locators: list[dict], files: list[str]):
    *_, sha, write = _host()
    flat, outputs = [], {}
    for row in sorted(locators, key=lambda item: item["context"]["case_index"]):
        context = row["context"]
        for own in context["case_slots"]:
            flat.append({**row, "context": own, "group_width": len(context["case_slots"])})
        try:
            uri = current["output_uri"] + "/" + row["output_path"]
            if uri not in files:
                raise ValueError("Expected Case group output is missing")
            try:
                response = await owner._knowledge_request(
                    job["account"], "GET", "/api/v1/content/read",
                    params={"uri": uri, "offset": 0, "limit": -1, "raw": "true"}, timeout=20.0,
                )
            except HTTPException as exc:
                if exc.status_code != 404:
                    raise
                raise ValueError("Expected Case group output disappeared") from exc
            content = response if isinstance(response, str) else response.get("content", response.get("text", ""))
            if isinstance(content, str):
                raw_path = root / "children" / current["child"] / (row["output_path"] + ".raw")
                if raw_path.is_symlink():
                    raise FrozenCaseGroupError("Frozen Case group raw output cannot be a symbolic link")
                if raw_path.is_file():
                    old = await asyncio.to_thread(raw_path.read_text, encoding="utf-8")
                    if sha(old) != sha(content):
                        raise FrozenCaseGroupError("Frozen Case group raw output changed during recovery")
                else:
                    await asyncio.to_thread(write, raw_path, content)
            if not isinstance(content, str) or not content.strip() or len(content) > MAX_GROUP_OUTPUT_CHARS:
                raise ValueError("Case group output is empty or exceeds its bounded wrapper budget")
            outputs.update(decode_group(json.loads(content), context))
        except (ValueError, KeyError, TypeError) as exc:
            if isinstance(exc, FrozenCaseGroupError):
                raise
            outputs.update({own["case_index"]: ("", str(exc)[:1000], False) for own in context["case_slots"]})
    return flat, outputs
