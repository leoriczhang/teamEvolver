"""Route accepted method units without promoting neighboring text to operative Evidence."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

PROTOCOL = "te-exact-source-units.v1"


def _io():
    from .ov_staged_orchestration import _read, _save

    return _read, _save


def _digest(value) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":")).encode()).hexdigest()


def _read_unit(root: Path, identity: str) -> dict:
    from .source_units import load_source_unit

    return load_source_unit(root, identity)


def _ids(values, *, empty=True) -> list[str]:
    if (not isinstance(values, list) or (not empty and not values)
            or any(not isinstance(value, str) or not value for value in values)
            or len(values) != len(set(values))):
        raise ValueError("Exact source scope requires unique unit identities")
    return values


def _ordered(root: Path, identities) -> list[str]:
    rows = [_read_unit(root, identity) for identity in set(identities)]
    return [row["id"] for row in sorted(rows, key=lambda row: row["unit_index"])]


def _content_ids(root: Path, identities) -> set[str]:
    """Formatting is archived and classified, but cannot support a generated method."""
    return {identity for identity in identities
            if _read_unit(root, identity)["kind"] not in {"separator", "empty"}}


def _method_ids(root: Path, identities) -> set[str]:
    return {identity for identity in _content_ids(root, identities)
            if _read_unit(root, identity)["kind"] not in {"heading", "table_header"}}


def accepted_source_classification(root: Path, state: dict) -> tuple[list[dict], set[str], list[set[str]]]:
    """Primary exact units are unique; structural context and transport chunks may repeat."""
    from .source_unit_extraction import retained_structural_ids

    read, _ = _io()
    records, accepted, shared, uncertain = [], set(), set(), []
    for index in range(state["unit_count"]):
        unit = read(root / "units/0" / f"{index:08d}.json")
        bindings = unit.get("source_unit_bindings")
        if not isinstance(bindings, dict):
            raise ValueError("Every accepted source index needs exact unit membership")
        classified = {identity for group in ("methods", "facts", "deferred")
                      for row in unit["value"][group] for ref in row["source_refs"]
                      for identity in _ids(bindings[ref], empty=False)}
        classified.update(retained_structural_ids(root, unit))
        primary = set(_ids(unit.get("assigned_unit_ids"), empty=False))
        allowed = primary | {context for identity in primary
                             for context in _read_unit(root, identity).get("context_unit_ids", [])}
        if not primary <= classified or not classified <= allowed:
            raise ValueError("Exact source classification changed its assigned units or structural context")
        if accepted.intersection(primary):
            raise ValueError("A frozen source unit was classified in more than one accepted extraction")
        accepted.update(primary)
        selected_context = {identity for ref in unit["value"]["context_refs"] for identity in _ids(bindings[ref])}
        if not selected_context <= allowed:
            raise ValueError("Exact shared constraints escaped their accepted original assignment")
        owned = {identity for row in unit["value"]["methods"] for ref in row["source_refs"]
                 for identity in bindings[ref]}
        owned_files = {_read_unit(root, identity)["source_uri"] for identity in _method_ids(root, owned)}
        # One packet may cross a file boundary. A separate rule file without its
        # own method owner stays global even when this packet also has other methods.
        shared.update(identity for identity in selected_context
                      if _read_unit(root, identity)["source_uri"] not in owned_files)
        if unit["value"]["context_uncertain"]:
            uncertain.append(classified)
        records.append(unit)
    positions = {_read_unit(root, identity)["unit_index"] for identity in accepted}
    if len(accepted) != state["source_unit_count"] or positions != set(range(state["source_unit_count"])):
        raise ValueError("Exact source classification does not match the complete frozen unit index")
    return records, _content_ids(root, shared), uncertain


def owned_source_context(root: Path, records: list[dict]) -> list[dict]:
    """An index's local context follows its own exact methods, never every candidate."""
    groups = []
    for unit in records:
        bindings = unit["source_unit_bindings"]
        methods = _method_ids(root, {identity for row in unit["value"]["methods"]
                                    for ref in row["source_refs"] for identity in bindings[ref]})
        context = _content_ids(root, {identity for ref in unit["value"]["context_refs"]
                                     for identity in bindings[ref]})
        files = {_read_unit(root, identity)["source_uri"] for identity in methods}
        for uri in sorted(files):
            owned = {identity for identity in methods if _read_unit(root, identity)["source_uri"] == uri}
            selected = {identity for identity in context if _read_unit(root, identity)["source_uri"] == uri}
            if selected:
                groups.append({"method_unit_ids": _ordered(root, owned),
                               "context_unit_ids": _ordered(root, selected)})
    return groups


def route_exact_source_candidates(root: Path, state: dict, source_prefix: str) -> dict:
    """Bind all final aliases to the exact units selected during the single source scan.

    Generated grouping decides membership, never new Evidence. Headers and explicitly
    shared/uncertain units remain context and cannot alone authorize new body steps.
    """
    read, save = _io()
    plan = read(root / "plan.json")
    names = {row["name"] for row in plan["skills"]}
    aliases_path = root / "identity/aliases.json"
    aliases = read(aliases_path) if aliases_path.is_file() else {}
    operative = {name: set() for name in names}
    related = {name: [] for name in names}
    catalog = read(root / "catalog/manifest.json")
    for index in range(catalog["method"]):
        record = read(root / "catalog/method" / f"{index:08d}.json")
        decision = read(root / "method-decisions" / (record["item"]["item_id"] + ".json"))
        if decision["target"] != "candidate":
            continue
        name, seen = decision["name"], set()
        while name in aliases:
            if name in seen:
                raise ValueError("Exact method membership contains an alias cycle")
            seen.add(name)
            name = aliases[name]["name"]
        if name not in names:
            raise ValueError("Exact method membership must target a final candidate")
        bindings = record.get("source_unit_bindings")
        if not isinstance(bindings, dict):
            raise ValueError("A method is missing its accepted exact source unit bindings")
        selected = {identity for ref in record["item"]["source_refs"]
                    for identity in _ids(bindings[ref], empty=False)}
        selected = _method_ids(root, selected)
        if not selected:
            raise ValueError("An accepted method needs original action text, not only structural headings")
        operative[name].update(selected)
        related[name].append({"item_id": record["item"]["item_id"], "task": record["item"]["task"],
                              "boundary": record["item"]["boundary"], "unit_ids": _ordered(root, selected)})

    records, shared, uncertain_groups = accepted_source_classification(root, state)
    owned_context = owned_source_context(root, records)
    assignment_count = 0
    for candidate in plan["skills"]:
        name = candidate["name"]
        if not operative[name]:
            raise ValueError("A final candidate needs operative method source units")
        context = set()
        for method in related[name]:
            method_context = set(shared)
            for identity in method["unit_ids"]:
                method_context.update(_read_unit(root, identity).get("context_unit_ids", []))
            for group in owned_context:
                if set(method["unit_ids"]).intersection(group["method_unit_ids"]):
                    method_context.update(group["context_unit_ids"])
            for group in uncertain_groups:
                if group.intersection(method["unit_ids"]):
                    method_context.update(group)
            method_context = _content_ids(root, method_context) - set(method["unit_ids"])
            method["context_ids"] = _ordered(root, method_context)
            context.update(method_context)
        context.difference_update(operative[name])
        identities = _ordered(root, operative[name] | context)
        rows = [_read_unit(root, identity) for identity in identities]
        if any(not row["source_uri"].startswith(source_prefix.rstrip("/") + "/") for row in rows):
            raise ValueError("Exact source unit escaped the frozen original collection")
        scope_path = root / "scopes" / (name + ".json")
        scope = read(scope_path)
        precise = {"protocol": PROTOCOL, "unit_ids": identities,
                   "operative_ids": _ordered(root, operative[name]), "context_ids": _ordered(root, context),
                   "method_groups": related[name],
                   "source_sha256": _digest([{key: row[key] for key in
                                              ("id", "source_uri", "start_char", "end_char", "sha256",
                                               "source_sha256")} for row in rows])}
        precise["sha256"] = _digest(precise)
        scope["source_units"] = precise
        scope["method_lineage_verified"] = True
        # Relative paths and exact ranges retain stable cache identity across adjacent rounds.
        scope["method_lineage_sha256"] = _digest([
            {**{key: row[key] for key in ("start_char", "end_char", "sha256", "source_sha256")},
             "path": row["source_uri"][len(source_prefix.rstrip("/")) + 1:]}
            for row in rows if row["id"] in operative[name]
        ] + [{"task": candidate["task"], "boundary": candidate["boundary"]}])
        save(scope_path, scope)
        assignment_count += len(identities)
    audit = {"protocol": PROTOCOL, "candidate_count": len(plan["skills"]),
             "source_unit_count": state["source_unit_count"], "routed_unit_assignments": assignment_count,
             "coverage_complete": True, "original_source_preserved": True,
             "semantic_correctness_verified": False}
    save(root / "source-routing/units.json", audit)
    return audit


def exact_candidate_scope(root: Path, scope: dict) -> dict:
    precise = scope.get("source_units")
    if (not isinstance(precise, dict) or precise.get("protocol") != PROTOCOL
            or precise.get("sha256") != _digest({key: value for key, value in precise.items() if key != "sha256"})):
        raise ValueError("Frozen exact candidate source scope changed")
    identities = _ids(precise["unit_ids"], empty=False)
    operative, context = set(_ids(precise["operative_ids"], empty=False)), set(_ids(precise["context_ids"]))
    if operative & context or operative | context != set(identities):
        raise ValueError("Exact method/context roles must partition the candidate source units")
    rows = [_read_unit(root, identity) for identity in identities]
    if precise["source_sha256"] != _digest([{key: row[key] for key in
                                            ("id", "source_uri", "start_char", "end_char", "sha256",
                                             "source_sha256")} for row in rows]):
        raise ValueError("Frozen exact original source units changed")
    return precise


def exact_case_groups(root: Path, scope: dict) -> list[dict]:
    """Keep each accepted association with its own headers; never round-robin unrelated context."""
    from . import bounded_compile_plan as planning

    precise = exact_candidate_scope(root, scope)
    groups, seen = [], set()
    for method in precise["method_groups"]:
        ids = _ids(method["unit_ids"], empty=False)
        if not set(ids) <= set(precise["operative_ids"]):
            raise ValueError("Method association escaped the candidate's operative source units")
        context_ids = _ids(method["context_ids"])
        if not set(context_ids) <= set(precise["unit_ids"]):
            raise ValueError("Method context escaped the frozen candidate scope")
        # Each bounded association keeps its complete constraints. Shrink operative
        # membership when needed; never silently drop a shared or uncertain condition.
        cursor = 0
        while cursor < len(ids):
            selected, after = None, cursor
            for end in range(cursor + 1, min(len(ids), cursor + 32) + 1):
                trial = _ordered(root, set(ids[cursor:end]) | set(context_ids))
                evidence = [_read_unit(root, identity) for identity in trial]
                if len(planning._json({"evidence": evidence})) > 26000:
                    break
                selected, after = trial, end
            if selected is None:
                raise ValueError("An exact method and its complete context exceed the Case packet budget")
            operative_ids = _ordered(root, ids[cursor:after])
            key = (tuple(selected), tuple(operative_ids))
            if key not in seen:
                seen.add(key)
                groups.append({"unit_ids": selected, "operative_ids": operative_ids})
            cursor = after
    if not groups:
        raise ValueError("A Case assignment needs an accepted operative source association")
    return groups


def exact_method_groups(root: Path, scope: dict) -> list[list[str]]:
    """Project transport IDs while keeping group order identical to the verified Case roles."""
    return [group["unit_ids"] for group in exact_case_groups(root, scope)]
