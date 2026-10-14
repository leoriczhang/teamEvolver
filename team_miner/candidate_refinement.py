"""Explicit, source-scoped business revisions; candidate routing identity stays stable."""

from __future__ import annotations

import copy
import hashlib
import json
import re
from bisect import bisect_left
from pathlib import Path

REFINEMENT_PROTOCOL = "te-candidate-refinement.v1"
BUSINESS_FIELDS = ("task", "trigger", "inputs", "outputs", "boundary", "rationale")


def _json(value) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def metadata_sha256(candidate: dict) -> str:
    return hashlib.sha256(_json({key: candidate[key] for key in ("name", *BUSINESS_FIELDS)}).encode()).hexdigest()


def _private(root: Path, path: Path) -> None:
    if path.is_symlink() or root.resolve() not in path.resolve().parents:
        raise ValueError("Refinement audit or frozen request path escapes its private round")


def _text(root: Path, path: Path) -> str:
    _private(root, path)
    return path.read_text(encoding="utf-8")


def mapped_method_ordinals(job: dict, root: Path, name: str, relatives: list[str]) -> list[int]:
    """Map the previous method's exact original slices, never its whole source file."""
    from .ov_staged_orchestration import _read, _sha

    number = int(job["current_round"]) - 1
    previous = root.parents[1] / f"round-{number}" / "staged"
    scope_path = previous / "scopes" / (name + ".json")
    archive = root.parents[2] / "rounds" / f"round-{number}" / "input.json"
    if not scope_path.is_file() or not archive.is_file():
        return []
    snapshot = _read(archive).get("source_snapshot")
    from .ov_compile_mining import RUNS_ROOT
    if snapshot != f"{RUNS_ROOT}/{job['job_id']}/rounds/round-{number}/source":
        raise ValueError("Refinement prior source snapshot belongs to a different job or round")
    by_uri = {snapshot.rstrip("/") + "/" + relative: relative for relative in relatives}
    slices = {}
    for ordinal in _read(scope_path).get("method_ordinals", []):
        old = _read(previous / "chunk-metadata" / f"{ordinal:08d}.json")
        relative = by_uri.get(old["source_uri"])
        if relative is None:
            continue
        slices.setdefault((relative, old["source_sha256"]), []).append((old["start_char"], old["end_char"]))
    result = set()
    for (relative, source_sha256), intervals in slices.items():
        uri = job["source_snapshot"].rstrip("/") + "/" + relative
        source_path = root / "source-files" / (_sha(uri) + ".json")
        if not source_path.is_file():
            continue
        source = _read(source_path)
        if source["uri"] != uri:
            raise ValueError("Refinement method source mapping is not exact")
        # A candidate stores only scalar intervals; each current metadata row is read once per source.
        # This avoids a quadratic disk scan for many chunks of one large source file.
        merged = []
        for start, end in sorted(intervals):
            if merged and start <= merged[-1][1]:
                merged[-1] = (merged[-1][0], max(merged[-1][1], end))
            else:
                merged.append((start, end))
        ends = [end for _, end in merged]
        for current_ordinal in source["ordinals"]:
            current = _read(root / "chunk-metadata" / f"{current_ordinal:08d}.json")
            position = bisect_left(ends, current["start_char"] + 1)
            if (current["source_uri"] == uri and current["source_sha256"] == source_sha256
                    and position < len(merged) and merged[position][0] < current["end_char"]):
                result.add(current_ordinal)
    return sorted(result)


def mapped_previous_source_scope(job: dict, root: Path, name: str, relatives: list[str]) -> dict | None:
    """Reuse exact reviewed method/context intervals only when all original slices still map."""
    from .ov_compile_mining import RUNS_ROOT
    from .ov_staged_orchestration import _read, _sha
    from .source_scope import SOURCE_SCOPE_PROTOCOL, SOURCE_SCOPE_PROTOCOLS

    number = int(job["current_round"]) - 1
    previous = root.parents[1] / f"round-{number}" / "staged"
    scope_path = previous / "scopes" / (name + ".json")
    archive = root.parents[2] / "rounds" / f"round-{number}" / "input.json"
    if not scope_path.is_file() or not archive.is_file():
        return None
    _private(previous, scope_path)
    snapshot = _read(archive).get("source_snapshot")
    if snapshot != f"{RUNS_ROOT}/{job['job_id']}/rounds/round-{number}/source":
        raise ValueError("Prior source scope belongs to a different job or round")
    scope = _read(scope_path)
    fields = {"method_ordinals": scope.get("method_ordinals", [])}
    if scope.get("scope_protocol") in SOURCE_SCOPE_PROTOCOLS:
        fields["context_ordinals"] = scope.get("context_ordinals", [])
        fields["shared_context_ordinals"] = scope.get("shared_context_ordinals", [])
    else:
        # Old full-file writer assignments are not proof that every range was a shared constraint.
        fields["context_ordinals"] = []
        fields["shared_context_ordinals"] = []
    if not fields["method_ordinals"]:
        return None
    by_uri = {snapshot.rstrip("/") + "/" + relative: relative for relative in relatives}
    intervals: dict[tuple[str, str], dict[str, list[tuple[int, int]]]] = {}
    for field, ordinals in fields.items():
        if (not isinstance(ordinals, list) or any(type(i) is not int or i < 0 for i in ordinals)
                or len(ordinals) != len(set(ordinals))):
            raise ValueError("Prior source scope requires exact original range ordinals")
        for ordinal in ordinals:
            path = previous / "chunk-metadata" / f"{ordinal:08d}.json"
            if not path.is_file():
                return None
            _private(previous, path)
            old = _read(path)
            relative = by_uri.get(old["source_uri"])
            start, end = old["start_char"], old["end_char"]
            if relative is None or not old.get("source_sha256"):
                return None
            if type(start) is not int or type(end) is not int or not 0 <= start < end:
                raise ValueError("Prior source scope has an invalid original interval")
            intervals.setdefault((relative, old["source_sha256"]), {}).setdefault(field, []).append((start, end))
    if not set(fields["shared_context_ordinals"]) <= set(fields["context_ordinals"]):
        raise ValueError("Prior shared constraints must belong to the exact prior context scope")
    mapped = {field: set() for field in fields}
    full = set()
    for (relative, source_hash), field_intervals in intervals.items():
        uri = job["source_snapshot"].rstrip("/") + "/" + relative
        source_path = root / "source-files" / (_sha(uri) + ".json")
        if not source_path.is_file():
            return None
        _private(root, source_path)
        source = _read(source_path)
        if source["uri"] != uri:
            raise ValueError("Prior source scope mapping is not exact")
        full.update(source["ordinals"])
        merged = {}
        for field, ranges in field_intervals.items():
            merged[field] = []
            for start, end in sorted(ranges):
                if merged[field] and start <= merged[field][-1][1]:
                    merged[field][-1] = (merged[field][-1][0], max(merged[field][-1][1], end))
                else:
                    merged[field].append((start, end))
        ends = {field: [end for _, end in ranges] for field, ranges in merged.items()}
        # Only scalar range coverage is retained; source text is never read or collected.
        covered = {field: [] for field in merged}
        for ordinal in source["ordinals"]:
            path = root / "chunk-metadata" / f"{ordinal:08d}.json"
            if not path.is_file():
                return None
            _private(root, path)
            current = _read(path)
            if current["source_uri"] != uri:
                raise ValueError("Prior source scope current range mapping is not exact")
            if current.get("source_sha256") != source_hash:
                return None
            start, end = current["start_char"], current["end_char"]
            for field, ranges in merged.items():
                position = bisect_left(ends[field], start + 1)
                while position < len(ranges) and ranges[position][0] < end:
                    mapped[field].add(ordinal)
                    covered[field].append((max(start, ranges[position][0]), min(end, ranges[position][1])))
                    position += 1
        for field, expected in merged.items():
            actual = []
            for start, end in sorted(covered[field]):
                if actual and start <= actual[-1][1]:
                    actual[-1] = (actual[-1][0], max(actual[-1][1], end))
                else:
                    actual.append((start, end))
            if actual != expected:
                return None
    methods, shared = mapped["method_ordinals"], mapped["shared_context_ordinals"]
    context = (mapped["context_ordinals"] - methods) | shared
    result = {
        "scope_protocol": scope.get("scope_protocol", SOURCE_SCOPE_PROTOCOL),
        "ordinals": sorted(methods | context), "method_ordinals": sorted(methods),
        "context_ordinals": sorted(context), "shared_context_ordinals": sorted(shared),
        "full_ordinals": sorted(full),
        "refinement_ordinals": sorted(methods),
    }
    if "uncertainty_scope_protocol" in scope:
        result["uncertainty_scope_protocol"] = scope["uncertainty_scope_protocol"]
    return result


def merge_candidate_scopes(*scopes: dict) -> dict:
    """Keep precise method anchors and shared constraints through semantic identity grouping."""
    from .source_scope import SOURCE_SCOPE_PROTOCOLS

    result = {
        field: sorted({i for scope in scopes for i in scope.get(field, scope["ordinals"])})
        for field in ("ordinals", "method_ordinals")
    }
    protocols = {scope["scope_protocol"] for scope in scopes if scope.get("scope_protocol") in SOURCE_SCOPE_PROTOCOLS}
    if not protocols:
        return result
    if len(protocols) != 1:
        raise ValueError("Candidate scopes cannot silently mix frozen routing protocols")
    routed_scopes = [scope for scope in scopes if scope.get("scope_protocol") in SOURCE_SCOPE_PROTOCOLS]
    uncertainty_profiles = {scope.get("uncertainty_scope_protocol") for scope in routed_scopes}
    if len(uncertainty_profiles) != 1:
        raise ValueError("Candidate scopes cannot mix frozen uncertainty routing profiles")
    uncertainty_profile = next(iter(uncertainty_profiles))
    if uncertainty_profile is not None:
        result["uncertainty_scope_protocol"] = uncertainty_profile
    context = {i for scope in scopes for i in scope.get("context_ordinals", [])}
    shared = {i for scope in scopes for i in scope.get("shared_context_ordinals", [])}
    context = (context - set(result["method_ordinals"])) | shared
    result.update(
        scope_protocol=next(iter(protocols)),
        context_ordinals=sorted(context), shared_context_ordinals=sorted(shared),
        full_ordinals=sorted({i for scope in scopes for i in scope.get("full_ordinals", scope["ordinals"])}),
        refinement_ordinals=sorted({i for scope in scopes for i in scope.get("refinement_ordinals", [])}),
    )
    result["ordinals"] = sorted(set(result["ordinals"]) | context | set(result["method_ordinals"]))
    return result


def latest_refinement(root: Path, identity: str) -> dict | None:
    """Read a hash-bound local audit projection, checking its frozen request and raw response."""
    from .ov_staged_orchestration import _read, _sha

    if not re.fullmatch(r"candidate-[a-f0-9]{24}", identity):
        raise ValueError("Invalid refinement candidate identity")
    path = root / "refinements" / "latest" / (identity + ".json")
    if not path.is_file():
        return None
    locator = json.loads(_text(root, path))
    child = locator["child"]
    if not isinstance(child, str) or not re.fullmatch(r"plan-[a-f0-9]{24}", child):
        raise ValueError("Invalid refinement child identity")
    journal_path = root / "refinements" / "journals" / (child + ".json")
    text = _text(root, journal_path)
    if _sha(text) != locator["journal_sha256"]:
        raise ValueError("Refinement audit hash differs from its immutable projection")
    journal = json.loads(text)
    if journal.get("schema_version") != REFINEMENT_PROTOCOL or journal.get("child") != child:
        raise ValueError("Refinement audit protocol or child identity is invalid")
    for file, key in (("packet.json", "packet_sha256"), ("request.json", "request_sha256"),
                      ("result.txt", "result_sha256")):
        if _sha(_text(root, root / "children" / child / file)) != journal[key]:
            raise ValueError("Refinement frozen request or raw response has changed")
    row = journal["changes"][locator["index"]]
    if row["candidate_id"] != identity or metadata_sha256(row["after"]) != row["metadata_sha256"]:
        raise ValueError("Refinement audit changed candidate identity or business fields")
    packet = _read(root / "children" / child / "packet.json")
    raw = _read(root / "children" / child / "result.txt")
    output_limit = 12000
    if "planning_aliases" in journal:
        from .direct_workflow_planning import FrozenPlanningIntegrityError
        from .ov_staged_orchestration import _selection_alias_result, _selection_packet

        current = _read(root / "children" / child / "metadata.json")
        if any(current.get(key) != value for key, value in journal["planning_aliases"].items()):
            raise FrozenPlanningIntegrityError("Refinement frozen planning alias dependencies changed")
        state = _read(root / "state.json")
        packet, manifest = _selection_packet(root, state, current)
        if manifest is None:
            raise FrozenPlanningIntegrityError("Refinement alias authority is missing")
        raw, output_limit = _selection_alias_result(raw, state, manifest)
    if "reuse_coalescing" in journal:
        from .ov_staged_orchestration import _canonical_planning_header
        from .workflow_planning import canonical_selection_reuse, coalesce_exact_authority_reuse

        current = _read(root / "children" / child / "metadata.json")
        dependency = journal["reuse_coalescing"]
        expected = f"children/{child}/reuse-coalescing.json"
        if (dependency.get("reuse_coalescing_file") != expected
                or any(current.get(key) != value for key, value in dependency.items())):
            raise ValueError("Refinement reuse projection dependencies changed")
        audit_text = _text(root, root / expected)
        if _sha(_json(json.loads(audit_text))) != dependency["reuse_coalescing_sha256"]:
            raise ValueError("Refinement reuse projection audit changed")
        controls = packet["derived_controls"]
        raw = _canonical_planning_header(raw, input_ids=controls["input_ids"],
                                        item_ids=[item["item_id"] for item in controls["items"]])
        raw = canonical_selection_reuse(raw, controls)
        raw, audit = coalesce_exact_authority_reuse(raw, controls, _max_output_chars=output_limit)
        if audit != json.loads(audit_text):
            raise ValueError("Refinement reuse projection differs from frozen raw output")
    authority = next((candidate for candidate in packet["derived_controls"]["known_candidates"]
                      if candidate["candidate_id"] == identity), None)
    proposed = next((candidate for candidate in raw["skills"]
                     if candidate.get("revise_candidate_id") == identity), None)
    if not authority or not proposed:
        raise ValueError("Refinement audit identity is absent from its frozen assignment or original response")
    if (row["name"] != authority["name"] or row["after"]["name"] != authority["name"]
            or metadata_sha256(row["before"]) != metadata_sha256(authority)
            or row["base_metadata_sha256"] != proposed["base_metadata_sha256"]
            or row["base_metadata_sha256"] != metadata_sha256(authority)
            or any(row["after"][key] != proposed[key] for key in BUSINESS_FIELDS)
            or row["derived_from"] != proposed["derived_from"] or row["reason"] != proposed["reason"]):
        raise ValueError("Refinement audit differs from its frozen authority and original response")
    return {**copy.deepcopy(row), "accepted_child": child}


def prepare_refinements(root: Path, job: dict, current: dict, value: dict, checked: dict,
                        records: list[dict], known_candidates: list[dict], *, state: dict | None = None) -> dict:
    """Prepare the entire packet before mutations; compare against its frozen authority."""
    from .candidate_identity import candidate_id
    from .ov_staged_orchestration import _read, _sha

    rows = [row for row in value.get("skills", []) if "revise_candidate_id" in row]
    if rows and int(job["current_round"]) <= 1:
        raise ValueError("Business metadata refinement requires a later round")
    authorities = {row["candidate_id"]: row for row in known_candidates}
    by_item = {row["item"]["item_id"]: row for row in records}
    changes = []
    for proposed in rows:
        identity = proposed["revise_candidate_id"]
        authority = authorities[identity]
        if candidate_id(job["job_id"], authority["name"]) != identity:
            raise ValueError("Refinement cannot rename a known routing identity")
        if not authority.get("refinement_allowed") or not set(proposed["derived_from"]) <= set(
            authority.get("refinement_item_ids", [])
        ):
            raise ValueError("Refinement proposals do not belong to the previous method scope")
        if (proposed["base_metadata_sha256"] != authority.get("base_metadata_sha256")
                or proposed["base_metadata_sha256"] != metadata_sha256(authority)):
            raise ValueError("Refinement base metadata hash differs from frozen authority")
        after = next(row for row in checked["skills"] if row["name"] == authority["name"])
        after = {key: copy.deepcopy(after[key]) for key in ("name", *BUSINESS_FIELDS)}
        if any(after[key] != proposed[key] for key in BUSINESS_FIELDS):
            raise ValueError("Checked refinement business fields differ from its original output")
        method_ordinals = sorted({
            ordinal for item in proposed["derived_from"] for ordinal in by_item[item]["ordinals"]
        })
        after["source_refs"] = sorted({
            _read(root / "chunk-metadata" / f"{ordinal:08d}.json")["source_uri"] for ordinal in method_ordinals
        })
        change = {
            "candidate_id": identity, "name": authority["name"],
            "before": {key: copy.deepcopy(authority[key]) for key in ("name", *BUSINESS_FIELDS)},
            "after": after, "base_metadata_sha256": proposed["base_metadata_sha256"],
            "metadata_sha256": metadata_sha256(after), "reason": proposed["reason"],
            "derived_from": proposed["derived_from"],
            "leaf_items": sorted({leaf for item in proposed["derived_from"] for leaf in by_item[item]["leaf_items"]}),
            "method_ordinals": method_ordinals,
        }
        existing = latest_refinement(root, identity)
        if existing and existing["accepted_child"] != current["child"] and (
            existing["metadata_sha256"] != change["base_metadata_sha256"]
        ):
            raise ValueError("Refinement conflicts with an already accepted business version")
        changes.append(change)
    if len({row["candidate_id"] for row in changes}) != len(changes):
        raise ValueError("A refinement packet cannot revise one candidate twice")
    child = current["child"]
    if not re.fullmatch(r"plan-[a-f0-9]{24}", child):
        raise ValueError("Invalid refinement child identity")
    packet_text = _text(root, root / current["packet_file"])
    request_text = _text(root, root / current["request_file"])
    if (_sha(_json(json.loads(packet_text))) != current["packet_sha256"]
            or _sha(_json(json.loads(request_text))) != current["request_sha256"]):
        raise ValueError("Frozen refinement packet or request has changed")
    packet = json.loads(packet_text)
    if current.get("planning_alias_protocol"):
        from .ov_staged_orchestration import _selection_packet

        if state is None:
            raise ValueError("Refinement needs its frozen planning alias state")
        packet, _ = _selection_packet(root, state, current)
    if packet["derived_controls"]["known_candidates"] != known_candidates:
        raise ValueError("Refinement authority differs from the frozen packet")
    if [row["item_id"] for row in packet["derived_controls"]["items"]] != list(by_item):
        raise ValueError("Refinement source proposals differ from the frozen assignment")
    journal = {
        "schema_version": REFINEMENT_PROTOCOL, "child": child,
        "packet_sha256": _sha(packet_text), "request_sha256": _sha(request_text),
        "result_sha256": _sha(_text(root, root / "children" / child / "result.txt")),
        "changes": changes,
    }
    if current.get("planning_alias_protocol"):
        journal["planning_aliases"] = {key: current[key] for key in (
            "planning_alias_protocol", "planning_alias_file", "planning_alias_sha256",
            "canonical_packet_file", "canonical_packet_sha256",
        )}
    if current.get("reuse_coalescing_file"):
        expected = f"children/{child}/reuse-coalescing.json"
        if (current["reuse_coalescing_file"] != expected
                or _sha(_json(json.loads(_text(root, root / expected)))) != current["reuse_coalescing_sha256"]):
            raise ValueError("Refinement reuse projection audit changed")
        journal["reuse_coalescing"] = {key: current[key] for key in (
            "reuse_coalescing_file", "reuse_coalescing_sha256",
        )}
    existing_path = root / "refinements" / "journals" / (child + ".json")
    if existing_path.is_file() and json.loads(_text(root, existing_path)) != journal:
        raise ValueError("An accepted refinement child cannot rewrite its audit")
    return journal


def commit_refinements(root: Path, journal: dict) -> None:
    """Write-ahead audit followed by restartable local projections; never touch raw data."""
    from .ov_staged_orchestration import _read, _save, _sha

    if not journal["changes"]:
        return
    path = root / "refinements" / "journals" / (journal["child"] + ".json")
    _private(root, path)
    if path.is_file():
        if _read(path) != journal:
            raise ValueError("Refinement journal conflicts with its accepted child")
    else:
        _save(path, journal)
    digest = _sha(_text(root, path))
    for index, row in enumerate(journal["changes"]):
        latest_path = root / "refinements" / "latest" / (row["candidate_id"] + ".json")
        _private(root, latest_path)
        _save(latest_path, {
            "child": journal["child"], "index": index, "journal_sha256": digest,
        })
