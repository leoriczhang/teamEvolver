"""Route original Evidence once; generated indexes only choose ranges, never replace their text."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

EFFICIENCY_PROTOCOL = "te-mining-efficient.v2"
SOURCE_SCOPE_PROTOCOL = "te-source-scope.v2"
SOURCE_SCOPE_PROTOCOL_V3 = "te-source-scope.v3"
SOURCE_SCOPE_PROTOCOLS = (SOURCE_SCOPE_PROTOCOL, SOURCE_SCOPE_PROTOCOL_V3)
UNCERTAINTY_SCOPE_PROTOCOL = "te-uncertain-context-units.v1"


def _io():
    from .ov_staged_orchestration import _read, _save, _sha

    return _read, _save, _sha


def prepare_source_routing(root: Path, state: dict) -> dict:
    """Require the complete accepted source scan before shrinking any writer assignment."""
    if state.get("source_unit_protocol") == "te-exact-source-units.v1":
        from .source_unit_review import prepare_routing

        return prepare_routing(root, state)
    if state.get("source_scope_protocol") == SOURCE_SCOPE_PROTOCOL_V3:
        return _prepare_local_source_routing(root, state)
    from .bounded_compile_plan import INDEX_SCHEMA_V2

    read, save, sha = _io()
    count = int(state["chunk_count"])
    covered, shared, uncertain_files = set(), set(), set()
    digest = hashlib.sha256()
    complete = True
    for index in range(int(state["unit_count"])):
        unit = read(root / "units/0" / f"{index:08d}.json")
        digest.update(json.dumps(unit, ensure_ascii=False, sort_keys=True).encode("utf-8"))
        ordinals = unit.get("ordinals", [])
        if (not isinstance(ordinals, list) or any(type(i) is not int or not 0 <= i < count for i in ordinals)
                or covered.intersection(ordinals)):
            raise ValueError("Source routing needs unique, exact accepted original ranges")
        covered.update(ordinals)
        if unit["value"].get("schema_version") != INDEX_SCHEMA_V2:
            complete = False
            continue
        context = unit.get("context_ordinals")
        uncertain = unit.get("context_uncertain")
        if (not isinstance(context, list) or any(type(i) is not int for i in context)
                or len(context) != len(set(context)) or not set(context) <= set(ordinals)
                or not isinstance(uncertain, bool)):
            raise ValueError("Source routing context must remain bound to its accepted original ranges")
        shared.update(context)
        if uncertain:
            for ordinal in ordinals:
                uncertain_files.add(read(root / "chunk-metadata" / f"{ordinal:08d}.json")["source_uri"])
    complete = complete and covered == set(range(count))
    if not complete:
        # Old indexes or incomplete classifications do not prove an absence of shared constraints.
        shared.update(range(count))
    else:
        for uri in uncertain_files:
            shared.update(read(root / "source-files" / (sha(uri) + ".json"))["ordinals"])
    manifest = {
        "protocol": SOURCE_SCOPE_PROTOCOL,
        "chunk_count": count,
        "accepted_units_sha256": digest.hexdigest(),
        "coverage_complete": complete,
        "shared_ordinals": sorted(shared),
        "uncertain_files": sorted(uncertain_files),
        "routing_is_evidence": False,
    }
    path = root / "source-routing/context.json"
    if path.is_file() and read(path) != manifest:
        raise ValueError("Accepted source routing changed after its complete scan")
    save(path, manifest)
    return manifest


def route_candidate_scope(root: Path, scope: dict, routing: dict) -> dict:
    """Keep method, heading/boundary neighbours and shared constraints as actual original Evidence."""
    if routing.get("source_unit_protocol") == "te-exact-source-units.v1":
        raise ValueError("Exact source routing requires original unit membership, not transport-only anchors")
    if routing.get("protocol") == SOURCE_SCOPE_PROTOCOL_V3:
        return _route_local_candidate_scope(root, scope, routing)
    read, _, sha = _io()
    method = sorted(set(scope.get("method_ordinals") or scope["ordinals"]))
    count = routing["chunk_count"]
    if not method or any(type(i) is not int or not 0 <= i < count for i in method):
        raise ValueError("Candidate source routing requires exact original method ranges")
    selected = set(method) | set(routing["shared_ordinals"])
    prior_context, prior_shared = set(), set()
    if scope.get("scope_protocol") == SOURCE_SCOPE_PROTOCOL:
        for field, target in (("context_ordinals", prior_context), ("shared_context_ordinals", prior_shared)):
            values = scope.get(field, [])
            if (not isinstance(values, list) or any(type(i) is not int or not 0 <= i < count for i in values)
                    or len(values) != len(set(values)) or not set(values) <= set(scope["ordinals"])):
                raise ValueError("Mapped original context must remain inside its verified precise scope")
            target.update(values)
        selected.update(prior_context | prior_shared)
    full = set(scope.get("full_ordinals") or scope["ordinals"])
    for ordinal in method:
        uri = read(root / "chunk-metadata" / f"{ordinal:08d}.json")["source_uri"]
        file = read(root / "source-files" / (sha(uri) + ".json"))
        if file["uri"] != uri or ordinal not in file["ordinals"]:
            raise ValueError("Method range does not match its exact frozen source file")
        full.update(file["ordinals"])
        # Include the heading and adjacent original ranges to retain split tables and boundary steps.
        if file["ordinals"]:
            selected.add(file["ordinals"][0])
        position = file["ordinals"].index(ordinal)
        selected.update(file["ordinals"][max(0, position - 1):position + 2])
    full.update(selected)
    shared = set(routing["shared_ordinals"]) | prior_shared
    return {
        "scope_protocol": SOURCE_SCOPE_PROTOCOL,
        "ordinals": sorted(selected),
        "method_ordinals": method,
        "context_ordinals": sorted((selected - set(method)) | shared),
        "shared_context_ordinals": sorted(shared),
        "full_ordinals": sorted(full),
        "routing_sha256": routing["accepted_units_sha256"],
        "routing_complete": routing["coverage_complete"],
    }


def _private_path(root: Path, path: Path) -> Path:
    root, path = root.absolute(), path.absolute()
    if not path.resolve().is_relative_to(root.resolve()):
        raise ValueError("Local source routing must stay inside its own frozen round")
    current = path
    while current != root:
        if current.is_symlink():
            raise ValueError("Local source routing metadata cannot use symbolic links")
        current = current.parent
    return path


def _private_read(root: Path, path: Path) -> dict:
    read, _, _ = _io()
    path = _private_path(root, path)
    value = read(path)
    if not isinstance(value, dict):
        raise ValueError("Local source routing metadata must be an object")
    return value


def _exact_ordinals(values, count: int, *, allow_empty: bool = True) -> set[int]:
    if (not isinstance(values, list) or any(type(i) is not int or not 0 <= i < count for i in values)
            or len(values) != len(set(values)) or (not allow_empty and not values)):
        raise ValueError("Local source routing needs unique, exact frozen original range ordinals")
    return set(values)


def _local_unit(root: Path, index: int, count: int) -> dict:
    """Bind local context to one accepted unit's original classification and exact aliases."""
    from .bounded_compile_plan import INDEX_SCHEMA_V2

    _, _, sha = _io()
    unit = _private_read(root, root / "units/0" / f"{index:08d}.json")
    ordinals = _exact_ordinals(unit.get("ordinals"), count, allow_empty=False)
    identity = unit.get("id")
    if not isinstance(identity, str) or not identity:
        raise ValueError("Local source routing needs the accepted unit identity")
    source_uris = sorted({
        _private_read(root, root / "chunk-metadata" / f"{ordinal:08d}.json")["source_uri"]
        for ordinal in ordinals
    })
    value = unit["value"]
    complete = value.get("schema_version") == INDEX_SCHEMA_V2
    methods, context = set(), set()
    uncertain = False
    if complete:
        bindings = unit.get("bindings")
        if not isinstance(bindings, dict):
            raise ValueError("Local source routing requires accepted original alias bindings")
        for selected in bindings.values():
            if not _exact_ordinals(selected, count, allow_empty=False) <= ordinals:
                raise ValueError("Accepted source alias escapes its original unit")

        def selected(refs, *, empty=False):
            if (not isinstance(refs, list) or any(not isinstance(ref, str) or ref not in bindings for ref in refs)
                    or len(refs) != len(set(refs)) or (not empty and not refs)):
                raise ValueError("Local context must use exact accepted original source aliases")
            return {ordinal for ref in refs for ordinal in bindings[ref]}

        classified = set()
        for group in ("methods", "facts", "deferred"):
            if not isinstance(value.get(group), list):
                raise ValueError("Local routing requires accepted classification arrays")
            for row in value[group]:
                ranges = selected(row["source_refs"])
                classified.update(ranges)
                if group == "methods":
                    methods.update(ranges)
        if unit.get("structural_coverage"):
            from .source_unit_extraction import retained_structural_ids
            from .source_units import load_source_unit

            structural = {ordinal for identity in retained_structural_ids(root, unit)
                          for ordinal in load_source_unit(root, identity)["ordinals"]}
            if not structural <= ordinals:
                raise ValueError("Retained structural coverage escaped its original source unit")
            classified.update(structural)
        context = selected(value.get("context_refs"), empty=True)
        if context != _exact_ordinals(unit.get("context_ordinals"), count):
            raise ValueError("Local context mapping differs from its accepted classification")
        uncertain = unit.get("context_uncertain")
        if type(uncertain) is not bool or value.get("context_uncertain") is not uncertain:
            raise ValueError("Local context uncertainty differs from its accepted classification")
        complete = classified == ordinals
    return {
        "unit_index": index, "unit_id": identity,
        "unit_sha256": sha(json.dumps(unit, ensure_ascii=False, sort_keys=True)),
        "ordinals": sorted(ordinals), "method_ordinals": sorted(methods),
        "context_ordinals": sorted(context), "context_uncertain": uncertain,
        "source_uris": source_uris, "classification_complete": complete,
    }


def _unit_uncertainty_enabled(value: dict) -> bool:
    protocol = value.get("uncertainty_scope_protocol")
    if (protocol is not None and not isinstance(protocol, str)) or protocol not in {
        None, "", UNCERTAINTY_SCOPE_PROTOCOL,
    }:
        raise ValueError("Unknown frozen context uncertainty scope protocol")
    return protocol == UNCERTAINTY_SCOPE_PROTOCOL


def _uncertain_context_ranges(root: Path, units: list[dict], files: set[str], count: int,
                              *, unit_scope: bool) -> set[int]:
    if not unit_scope:
        return {ordinal for uri in files for ordinal in _source_file(root, uri, count)}
    # The flag describes an actual constraint with uncertain applicability in
    # this assigned unit. It does not classify unseen portions of its file.
    selected = {ordinal for unit in units if unit["context_uncertain"] for ordinal in unit["ordinals"]}
    source_files = {uri: _source_file(root, uri, count) for uri in files}
    selected.update(file[0] for file in source_files.values())
    for unit in units:
        if not unit["context_uncertain"]:
            continue
        assigned = set(unit["ordinals"])
        for uri in unit["source_uris"]:
            file = source_files[uri]
            positions = {position for position, ordinal in enumerate(file) if ordinal in assigned}
            # Guard each contiguous run in its own original file, once. Never
            # fill gaps between disjoint assignments or expand these guards again.
            for position in positions:
                if position > 0 and position - 1 not in positions:
                    selected.add(file[position - 1])
                if position + 1 < len(file) and position + 1 not in positions:
                    selected.add(file[position + 1])
    return selected


def _uncertainty_scope_sha(routing: dict, uncertain_context: set[int]) -> str:
    _, _, sha = _io()
    return sha(json.dumps({
        "protocol": UNCERTAINTY_SCOPE_PROTOCOL,
        "chunk_count": routing["chunk_count"],
        "accepted_units_sha256": routing["accepted_units_sha256"],
        "uncertain_context_ordinals": sorted(uncertain_context),
    }, ensure_ascii=False, sort_keys=True))


def _prepare_local_source_routing(root: Path, state: dict) -> dict:
    _, save, sha = _io()
    unit_scope = _unit_uncertainty_enabled(state)
    count, unit_count = state["chunk_count"], state["unit_count"]
    if type(count) is not int or count < 1 or type(unit_count) is not int or unit_count < 0:
        raise ValueError("Local source routing needs exact frozen source and accepted-unit counts")
    covered, uncertain_files, unowned_context, unowned_uncertain = set(), set(), set(), set()
    units, digest = [], hashlib.sha256()
    complete = True
    for index in range(unit_count):
        unit = _local_unit(root, index, count)
        if covered.intersection(unit["ordinals"]):
            raise ValueError("Local source routing needs a unique accepted unit for each original range")
        covered.update(unit["ordinals"])
        units.append(unit)
        digest.update(bytes.fromhex(unit["unit_sha256"]))
        complete = complete and unit["classification_complete"]
        if unit["context_uncertain"]:
            uncertain_files.update(unit["source_uris"])
        if not unit["method_ordinals"]:
            # A separate rule document has no proven method owner. Its marked
            # constraints must stay globally available until ownership is known.
            unowned_context.update(unit["context_ordinals"])
            if unit["context_uncertain"]:
                unowned_uncertain.update(unit["source_uris"])
    complete = complete and covered == set(range(count))
    shared = set(unowned_context)
    # Unknown applicability remains globally available as original Evidence.
    # Fresh unit-scoped profiles retain the assigned ranges and their headings;
    # previously frozen profiles keep their original whole-file fallback.
    uncertain_context = _uncertain_context_ranges(root, units, uncertain_files, count, unit_scope=unit_scope)
    shared.update(uncertain_context)
    manifest = {
        "protocol": SOURCE_SCOPE_PROTOCOL_V3, "chunk_count": count,
        "accepted_units_sha256": digest.hexdigest(), "coverage_complete": complete,
        "unit_contexts": units,
        "associations_sha256": sha(json.dumps(units, ensure_ascii=False, sort_keys=True)),
        "shared_ordinals": sorted(shared) if complete else list(range(count)),
        "unowned_context_ordinals": sorted(unowned_context),
        "unowned_uncertain_files": sorted(unowned_uncertain),
        "uncertain_files": sorted(uncertain_files), "routing_is_evidence": False,
    }
    if unit_scope:
        manifest.update(
            uncertainty_scope_protocol=UNCERTAINTY_SCOPE_PROTOCOL,
            uncertain_context_ordinals=sorted(uncertain_context),
            uncertainty_scope_sha256=_uncertainty_scope_sha(manifest, uncertain_context),
        )
    path = _private_path(root, root / "source-routing/context.json")
    if path.is_file() and _private_read(root, path) != manifest:
        raise ValueError("Accepted local source routing changed after its frozen complete scan")
    # The generated manifest contains only routing metadata; raw chunks remain untouched.
    save(path, manifest)
    return manifest


def _source_file(root: Path, uri: str, count: int) -> list[int]:
    _, _, sha = _io()
    source = _private_read(root, root / "source-files" / (sha(uri) + ".json"))
    ordinals = source.get("ordinals")
    _exact_ordinals(ordinals, count, allow_empty=False)
    if source.get("uri") != uri or ordinals != sorted(ordinals):
        raise ValueError("Local routing file membership differs from the exact frozen source")
    for ordinal in ordinals:
        if _private_read(root, root / "chunk-metadata" / f"{ordinal:08d}.json")["source_uri"] != uri:
            raise ValueError("Local routing source file contains a foreign original range")
    return ordinals


def _route_local_candidate_scope(root: Path, scope: dict, routing: dict) -> dict:
    _, _, sha = _io()
    unit_scope = _unit_uncertainty_enabled(routing)
    if (scope.get("uncertainty_scope_protocol")
            and scope["uncertainty_scope_protocol"] != routing.get("uncertainty_scope_protocol")):
        raise ValueError("Frozen candidate context uncertainty scope protocol changed")
    if routing != _private_read(root, root / "source-routing/context.json"):
        raise ValueError("Local context association changed from its frozen routing manifest")
    units = routing["unit_contexts"]
    if routing["associations_sha256"] != sha(json.dumps(units, ensure_ascii=False, sort_keys=True)):
        raise ValueError("Local context association SHA differs from its frozen manifest")
    count = routing["chunk_count"]
    associated, uncertain_files, unowned_context, unowned_uncertain = set(), set(), set(), set()
    digest = hashlib.sha256()
    complete = True
    for index, unit in enumerate(units):
        if _local_unit(root, index, count) != unit:
            raise ValueError("Local context association differs from its accepted original unit")
        ordinals = _exact_ordinals(unit["ordinals"], count, allow_empty=False)
        if unit["unit_index"] != index or associated.intersection(ordinals):
            raise ValueError("Local context associations need unique consecutive accepted units")
        associated.update(ordinals)
        complete = complete and unit["classification_complete"]
        digest.update(bytes.fromhex(unit["unit_sha256"]))
        if unit["context_uncertain"]:
            uncertain_files.update(unit["source_uris"])
        if not unit["method_ordinals"]:
            unowned_context.update(unit["context_ordinals"])
            if unit["context_uncertain"]:
                unowned_uncertain.update(unit["source_uris"])
    complete = complete and associated == set(range(count))
    global_context = set(unowned_context)
    uncertain_context = _uncertain_context_ranges(root, units, uncertain_files, count, unit_scope=unit_scope)
    global_context.update(uncertain_context)
    if (type(routing["coverage_complete"]) is not bool or routing["coverage_complete"] != complete
            or routing["accepted_units_sha256"] != digest.hexdigest()
            or routing["uncertain_files"] != sorted(uncertain_files)
            or routing["unowned_context_ordinals"] != sorted(unowned_context)
            or routing["unowned_uncertain_files"] != sorted(unowned_uncertain)
            or routing["shared_ordinals"] != (sorted(global_context) if complete else list(range(count)))):
        raise ValueError("Local context routing summary differs from its accepted unit associations")
    if unit_scope and (
        routing.get("uncertain_context_ordinals") != sorted(uncertain_context)
        or routing.get("uncertainty_scope_sha256") != _uncertainty_scope_sha(routing, uncertain_context)
    ):
        raise ValueError("Frozen unit uncertainty scope differs from its accepted original ranges")
    method = _exact_ordinals(scope.get("method_ordinals") or scope["ordinals"], count, allow_empty=False)
    selected, shared = set(method) | set(routing["shared_ordinals"]), set(routing["shared_ordinals"])
    full = _exact_ordinals(scope.get("full_ordinals") or scope["ordinals"], count, allow_empty=False)
    related_units = []
    uncertain_units = set()
    if not routing["coverage_complete"]:
        selected.update(range(count))
        shared.update(range(count))
    else:
        for unit in units:
            if not method.intersection(unit["ordinals"]):
                continue
            related_units.append(unit["unit_id"])
            selected.update(unit["context_ordinals"])
            shared.update(unit["context_ordinals"])
            if unit["context_uncertain"]:
                uncertain_units.update(unit["source_uris"])
        if not method <= associated:
            raise ValueError("Every method anchor needs an accepted original unit before local routing")
    if scope.get("scope_protocol") == SOURCE_SCOPE_PROTOCOL_V3:
        context = _exact_ordinals(scope.get("context_ordinals", []), count)
        previous_shared = _exact_ordinals(scope.get("shared_context_ordinals", []), count)
        previous = _exact_ordinals(scope["ordinals"], count, allow_empty=False)
        if not previous_shared <= context <= previous:
            raise ValueError("Prior local context must remain within its exact mapped original scope")
        selected.update(context)
        shared.update(previous_shared)
    file_cache = {}

    def source_file(uri: str) -> list[int]:
        if uri not in file_cache:
            file_cache[uri] = _source_file(root, uri, count)
        return file_cache[uri]

    for ordinal in method:
        uri = _private_read(root, root / "chunk-metadata" / f"{ordinal:08d}.json")["source_uri"]
        file = source_file(uri)
        if ordinal not in file:
            raise ValueError("Local method range does not belong to its exact original source file")
        full.update(file)
        position = file.index(ordinal)
        selected.add(file[0])
        selected.update(file[max(0, position - 1):position + 2])
    selected_files = {
        _private_read(root, root / "chunk-metadata" / f"{ordinal:08d}.json")["source_uri"]
        for ordinal in selected
    }
    uncertain = (selected_files & set(routing["uncertain_files"])) | uncertain_units
    for uri in selected_files | uncertain:
        file = source_file(uri)
        full.update(file)
        if uri in uncertain and not unit_scope:
            selected.update(file)
            shared.update(file)
    full.update(selected)
    result = {
        "scope_protocol": SOURCE_SCOPE_PROTOCOL_V3, "ordinals": sorted(selected),
        "method_ordinals": sorted(method), "context_ordinals": sorted((selected - method) | shared),
        "shared_context_ordinals": sorted(shared), "full_ordinals": sorted(full),
        "routing_sha256": routing["accepted_units_sha256"], "routing_complete": routing["coverage_complete"],
        "context_unit_ids": related_units, "uncertain_source_files": sorted(uncertain),
    }
    if unit_scope:
        result.update(
            uncertainty_scope_protocol=UNCERTAINTY_SCOPE_PROTOCOL,
            uncertainty_scope_sha256=routing["uncertainty_scope_sha256"],
        )
    return result


def candidate_context_ordinals(root: Path, method_ordinals: list[int], routing: dict) -> list[int]:
    """Expose validated context routing for readonly planning without promoting generated notes to Evidence."""
    if routing.get("protocol") == SOURCE_SCOPE_PROTOCOL_V3:
        scope = {"ordinals": method_ordinals, "method_ordinals": method_ordinals}
        return route_candidate_scope(root, scope, routing)["shared_context_ordinals"]
    return list(routing["shared_ordinals"])


def _method_lineages(root: Path, names: set[str], source_prefix: str) -> dict:
    """Bind cache identity to every original method disposition, including same-range merged branches."""
    read, _, sha = _io()
    manifest_path = root / "catalog/manifest.json"
    if not manifest_path.is_file() or not source_prefix:
        return {}
    alias_path = root / "identity/aliases.json"
    aliases = read(alias_path) if alias_path.is_file() else {}
    digests = {name: [] for name in names}
    for index in range(read(manifest_path)["method"]):
        row = read(root / "catalog/method" / f"{index:08d}.json")
        decision_path = root / "method-decisions" / (row["item"]["item_id"] + ".json")
        if not decision_path.is_file():
            raise ValueError("Every original method needs a disposition before writer routing")
        decision = read(decision_path)
        if decision["target"] != "candidate":
            continue
        name, seen = decision["name"], set()
        while name in aliases:
            if name in seen:
                raise ValueError("Candidate method lineage contains an alias cycle")
            seen.add(name)
            name = aliases[name]["name"]
        if name not in names:
            raise ValueError("Candidate method lineage must name an actual final candidate")
        ranges = []
        for ordinal in row["ordinals"]:
            chunk = read(root / "chunk-metadata" / f"{ordinal:08d}.json")
            if not chunk["source_uri"].startswith(source_prefix.rstrip("/") + "/"):
                raise ValueError("Method lineage source escapes the frozen original collection")
            ranges.append({
                "path": chunk["source_uri"][len(source_prefix.rstrip("/")) + 1:],
                **{key: chunk[key] for key in ("start_char", "end_char", "sha256", "source_sha256")},
            })
        digests[name].append(sha(json.dumps({
            "task": row["item"]["task"], "boundary": row["item"]["boundary"], "ranges": ranges,
        }, ensure_ascii=False, sort_keys=True)))
    return {name: sha(json.dumps(sorted(set(values)))) for name, values in digests.items() if values}


def route_final_candidates(root: Path, state: dict, source_prefix: str = "") -> dict:
    """Apply after semantic grouping so aliases and later-round authorities cannot re-expand writers."""
    if state.get("source_unit_protocol") == "te-exact-source-units.v1":
        from .source_unit_scope import route_exact_source_candidates

        return route_exact_source_candidates(root, state, source_prefix)
    read, save, _ = _io()
    routing = prepare_source_routing(root, state)
    plan = read(root / "plan.json")
    lineages = _method_lineages(root, {candidate["name"] for candidate in plan["skills"]}, source_prefix)
    before, after = 0, 0
    for candidate in plan["skills"]:
        path = root / "scopes" / (candidate["name"] + ".json")
        scope = read(path)
        routed = route_candidate_scope(root, scope, routing)
        routed["method_lineage_verified"] = candidate["name"] in lineages
        routed["method_lineage_sha256"] = lineages.get(candidate["name"], "")
        before += len(routed["full_ordinals"])
        after += len(routed["ordinals"])
        save(path, routed)
        candidate["source_refs"] = list(dict.fromkeys(candidate["source_refs"] + [
            read(root / "chunk-metadata" / f"{ordinal:08d}.json")["source_uri"]
            for ordinal in routed["ordinals"]
        ]))
    save(root / "plan.json", plan)
    audit = {
        "protocol": routing["protocol"],
        "candidate_count": len(plan["skills"]),
        "full_range_assignments": before,
        "routed_range_assignments": after,
        "shared_context_count": len(routing["shared_ordinals"]),
        "coverage_complete": routing["coverage_complete"],
        "original_source_preserved": True,
    }
    if routing["protocol"] == SOURCE_SCOPE_PROTOCOL_V3:
        audit.update(local_context_units=len(routing["unit_contexts"]),
                     context_relation="accepted_method_unit", global_context_union=False,
                     unowned_context_count=len(routing["unowned_context_ordinals"]),
                     unowned_uncertain_file_count=len(routing["unowned_uncertain_files"]),
                     unknown_scope_file_count=len(routing["uncertain_files"]))
        if _unit_uncertainty_enabled(routing):
            audit.update(
                uncertainty_scope_protocol=UNCERTAINTY_SCOPE_PROTOCOL,
                uncertain_context_range_count=len(routing["uncertain_context_ordinals"]),
                uncertainty_scope_sha256=routing["uncertainty_scope_sha256"],
            )
    save(root / "source-routing/summary.json", audit)
    return audit
