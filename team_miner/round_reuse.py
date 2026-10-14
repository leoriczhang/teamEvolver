"""Conservative local reuse within one job's immediately preceding reviewed round."""

from __future__ import annotations

import copy
import hashlib
import re
from functools import lru_cache
from pathlib import Path

from . import benchmark_format
from . import bounded_compile_plan as planning
from .compile_strategy import compiler_business_policy, render_case_batch_compiler, render_stage_batch_compiler
from .ov_compile_mining import RUNS_ROOT, _skill_metadata

ROUND_REUSE_PROTOCOL = "te-round-reuse.v1"
OPTIMIZED_EXECUTION_PROTOCOL = "te-mining-optimized.v1"
BUSINESS_FIELDS = ("name", "task", "trigger", "inputs", "outputs", "boundary", "rationale")
PROTOCOL_FIELDS = (
    "efficiency_protocol", "workflow_planning_protocol", "source_scope_protocol", "extract_batch_protocol",
    "direct_workflow_protocol", "selection_recovery_protocol", "source_input_clarity_protocol",
    "planning_alias_protocol", "case_group_protocol",
    "compact_planning_protocol", "quote_alias_protocol", "identity_partition_protocol",
    "uncertainty_scope_protocol", "skill_body_protocol", "workflow_partition_protocol", "workflow_compiler_sha256",
    "source_fragment_protocol", "source_fragment_chars", "extract_max_chunks", "source_unit_protocol",
)
SOURCE_UNIT_PROTOCOL = "te-exact-source-units.v1"


def _host():
    from .ov_staged_orchestration import _alias, _bind_unit, _canonical_extract_references, _json, _read, _save, _sha

    return _alias, _bind_unit, _canonical_extract_references, _json, _read, _save, _sha


def _file_sha(path: Path) -> str:
    if path.is_symlink() or not path.is_file():
        raise ValueError("Round reuse requires an ordinary frozen local file")
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(64 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _stat(path: Path) -> list[int]:
    if path.is_symlink() or not path.is_file():
        raise ValueError("Round reuse source cannot be a symbolic link or missing file")
    value = path.stat()
    return [value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns]


@lru_cache(maxsize=128)
def _checked_file(path: str, digest: str, stat: tuple) -> None:
    # Cache only verification of immutable local metadata, never model/Evidence bodies.
    if _file_sha(Path(path)) != digest:
        raise ValueError("Round reuse frozen dependency SHA changed")


def _verify_file(path: Path, digest: str) -> None:
    _checked_file(str(path), digest, tuple(_stat(path)))


@lru_cache(maxsize=8)
def _checked_sources(path: str, digest: str, stat: tuple) -> dict:
    *_, read, _, _ = _host()
    _checked_file(path, digest, stat)
    return read(Path(path))


def _inside(root: Path, relative: str) -> Path:
    if not isinstance(relative, str) or not relative or "\\" in relative:
        raise ValueError("Round reuse locator is invalid")
    path = root / relative
    if Path(relative).is_absolute() or any(part in {".", ".."} for part in Path(relative).parts):
        raise ValueError("Round reuse locator escapes the reviewed round")
    if root.resolve() not in path.resolve().parents or any(parent.is_symlink() for parent in [path, *path.parents]
                                                          if parent != root.parent):
        raise ValueError("Round reuse locator escapes the reviewed round")
    return path


def _canonical(value: dict) -> dict:
    return {key: value[key] for key in BUSINESS_FIELDS}


def _sources(entries: list[dict], folder: Path) -> dict:
    sources = {}
    for entry in entries:
        if entry.get("kind") not in {"source", "human"}:
            continue
        relative = entry["relative_path"]
        if (not isinstance(relative, str) or not relative.startswith(("original/", "supplements/"))
                or any(part in {"", ".", ".."} for part in relative.split("/")) or "\\" in relative
                or relative in sources):
            raise ValueError("Round reuse source needs an exact unique frozen relative path")
        # Archives retain the original manifest's local_path; read its self-contained copied file instead.
        original_path = Path(entry["local_path"])
        expected_parent = (folder.parent.parent.parent / "round_inputs" / folder.parent.name
                           if folder.name == "input" and folder.parent.parent.name == "rounds" else folder)
        if not original_path.is_absolute() or original_path.parent != expected_parent:
            raise ValueError("Round reuse source locator does not belong to this job and frozen round")
        path = _inside(folder, original_path.name)
        if _file_sha(path) != entry["sha256"]:
            raise ValueError("Round reuse frozen source SHA changed")
        sources[relative] = {
            "kind": entry["kind"], "sha256": entry["sha256"], "local_path": str(path),
            "stat": _stat(path), "origin_uri": entry.get("origin_uri") or "",
        }
    return sources


def _relative(uri: str, prefix: str, sources: dict) -> str:
    relative = uri[len(prefix) + 1:] if isinstance(uri, str) and uri.startswith(prefix + "/") else ""
    if relative not in sources:
        raise ValueError("Round reuse source is not an exact member of this job's frozen manifest")
    return relative


def _source_unchanged(record: dict) -> None:
    if _stat(Path(record["local_path"])) != record["stat"]:
        raise ValueError("Round reuse frozen source file changed after dependency capture")


def _chunk_key(chunk: dict, prefix: str, sources: dict) -> list:
    relative = _relative(chunk["source_uri"], prefix, sources)
    source = sources[relative]
    _source_unchanged(source)
    return [relative, source["kind"], source["sha256"], chunk["start_char"], chunk["end_char"], chunk["sha256"]]


def _profile(root: Path, scope: dict, prefix: str, sources: dict) -> dict:
    if "source_units" in scope:
        return _source_unit_profile(root, scope, prefix, sources)
    _, _, _, json_text, read, _, _ = _host()
    result = {}
    keys = ("ordinals", "method_ordinals")
    if "scope_protocol" in scope:
        protocol = scope["scope_protocol"]
        if not isinstance(protocol, str) or not protocol.strip():
            raise ValueError("Round reuse needs an explicit nonempty source scope protocol")
        context = scope.get("context_ordinals", [])
        if (not isinstance(context, list) or any(type(ordinal) is not int or ordinal < 0 for ordinal in context)
                or not set(context) <= set(scope["ordinals"])):
            raise ValueError("Round reuse context Evidence must be contained in the actual raw source scope")
        result["scope_protocol"] = protocol
        if "method_lineage_verified" in scope or "method_lineage_sha256" in scope:
            verified, lineage = scope.get("method_lineage_verified"), scope.get("method_lineage_sha256")
            if (type(verified) is not bool or not isinstance(lineage, str)
                    or (verified and not re.fullmatch(r"[a-f0-9]{64}", lineage))):
                raise ValueError("Round reuse method lineage needs a verified exact dependency SHA")
            result.update(method_lineage_verified=verified, method_lineage_sha256=lineage)
        keys += ("context_ordinals",)
    for key in keys:
        selected = scope.get(key, []) if key == "context_ordinals" else scope.get(key) or scope["ordinals"]
        digest, paths = hashlib.sha256(), set()
        for ordinal in selected:
            if type(ordinal) is not int or ordinal < 0:
                raise ValueError("Round reuse scope must contain exact source ordinals")
            chunk = read(_inside(root, f"chunks/{ordinal:08d}.json"))
            planning.validate_chunks([chunk])
            descriptor = _chunk_key(chunk, prefix, sources)
            digest.update((json_text(descriptor) + "\n").encode())
            paths.add(descriptor[0])
        result[key] = {"sha256": digest.hexdigest(), "count": len(selected)}
        result["paths"] = sorted(paths | set(result.get("paths", [])))
    return result


def _source_unit_key(unit: dict, prefix: str, sources: dict) -> list:
    descriptor = _chunk_key(unit, prefix, sources)
    if unit["source_sha256"] != sources[descriptor[0]]["sha256"]:
        raise ValueError("Exact reusable unit differs from its complete frozen source SHA")
    return [*descriptor, unit["kind"]]


def _source_unit_profile(root: Path, scope: dict, prefix: str, sources: dict) -> dict:
    from .source_unit_scope import exact_candidate_scope
    from .source_units import load_source_unit

    _, _, _, json_text, _, _, sha = _host()
    precise = exact_candidate_scope(root, scope)
    descriptors = {identity: _source_unit_key(load_source_unit(root, identity), prefix, sources)
                   for identity in precise["unit_ids"]}
    result = {"source_unit_protocol": SOURCE_UNIT_PROTOCOL,
              "paths": sorted({row[0] for row in descriptors.values()})}
    for role in ("unit_ids", "operative_ids", "context_ids"):
        values = sorted([descriptors[identity] for identity in precise[role]])
        result[role] = {"sha256": sha(json_text(values)), "count": len(values)}
    groups, covered, seen = [], set(), set()
    for method in precise["method_groups"]:
        identities = method["unit_ids"]
        if (not isinstance(method.get("item_id"), str) or method["item_id"] in seen
                or not isinstance(identities, list) or not identities
                or len(set(identities)) != len(identities)
                or not set(identities) <= set(precise["operative_ids"])
                or any(not isinstance(method.get(key), str) or not method[key].strip()
                       for key in ("task", "boundary"))):
            raise ValueError("Exact candidate cache needs each complete accepted method association")
        seen.add(method["item_id"])
        covered.update(identities)
        groups.append({"task": method["task"], "boundary": method["boundary"],
                       "units": sorted([descriptors[identity] for identity in identities])})
    if covered != set(precise["operative_ids"]):
        raise ValueError("Exact candidate cache method associations must cover all operative units")
    result["method_groups"] = {"sha256": sha(json_text(sorted(groups, key=json_text))), "count": len(groups)}
    return result


def _write_record(folder: Path, key: str, value: dict) -> None:
    _, _, _, json_text, _, save, sha = _host()
    save(folder / f"{key}-{sha(json_text(value))}.json", value)


def _record(folder: Path, key: str) -> dict | None:
    _, _, _, json_text, read, _, sha = _host()
    paths = list(folder.glob(key + "-*.json"))
    if not paths:
        return None
    if len(paths) != 1:
        raise ValueError("Round reuse dependency record is ambiguous")
    value = read(paths[0])
    if paths[0].name != key + "-" + sha(json_text(value)) + ".json":
        raise ValueError("Round reuse dependency record SHA changed")
    return value


def _cache_folder(root: Path, header: dict, kind: str) -> Path:
    folder = root / "reuse" / kind
    # Old frozen manifests keep their original layout; fresh dependency generations
    # cannot pick up rows left by an interrupted preparation under different protocols.
    return folder / header["cache_namespace"] if header.get("cache_namespace") else folder


def _feedback_impact(submission: dict, previous_prefix: str, original_prefix: str, previous_sources: dict) -> dict:
    paths, reasons = set(), []
    if str(submission.get("feedback") or "").strip():
        reasons.append("Unscoped review feedback affects every candidate")
    if submission.get("attachments"):
        reasons.append("Unscoped supplemental attachments affect every candidate")
    questions = {row["qid"]: row for row in submission.get("questions") or []}
    affected = set(questions) | set(submission.get("answers") or {}) | set(submission.get("references") or {})
    affected.update(submission.get("unresolved") or [])
    for qid in affected:
        refs = (questions.get(qid) or {}).get("source_refs") or []
        selected = set()
        for uri in refs:
            matches = {
                relative for relative, row in previous_sources.items()
                if uri == previous_prefix + "/" + relative
                or (row["kind"] == "source" and uri == original_prefix + "/" + relative)
                or (row.get("origin_uri") and uri == row["origin_uri"])
            }
            if len(matches) != 1:
                reasons.append("A review question or assertion has no unambiguous exact source scope")
                continue
            selected.update(matches)
        if not selected:
            reasons.append("A review question or assertion has no exact source scope")
        paths.update(selected)
    return {"global": bool(reasons), "reasons": sorted(set(reasons)), "paths": sorted(paths)}


def _dependency_files(root: Path, metadata: dict) -> dict:
    files = {}
    for field in ("packet_file", "request_file", "compiler_file"):
        path = _inside(root, metadata[field])
        files[field] = {"path": metadata[field], "sha256": _file_sha(path)}
    result = f"children/{metadata['child']}/result.txt"
    files["result"] = {"path": result, "sha256": _file_sha(_inside(root, result))}
    return files


def _writer_compilers(body: str, state: dict | None = None) -> dict:
    *_, sha = _host()
    integrated = bool(state and state.get("skill_body_protocol") == "te-incremental-skill-body.v1")
    kwargs = {"integrated_body": integrated}
    if state and state.get("skill_body_protocol") == "te-skill-body-patch.v1":
        kwargs["body_patch"] = True
    result = {
        "skill_section_batch": sha(render_stage_batch_compiler(body, "skill_section", **kwargs)),
        "evaluation_batch": sha(render_stage_batch_compiler(body, "evaluation")),
        "case_batch": sha(render_case_batch_compiler(body)),
    }
    if state and state.get("case_group_protocol") == "te-case-groups.v1":
        from .ov_case_group_orchestration import render_case_group_compiler

        result["case_group_batch"] = sha(render_case_group_compiler(body))
    return result


def _accepted_extract_batch(root: Path, unit: dict, index: int) -> dict | None:
    """Verify one accepted file Map output against its unchanged batch journal and original Evidence."""
    from .ov_extract_batch_orchestration import EXTRACT_BATCH_STAGE, _verified

    alias, _, _, json_text, read, _, sha = _host()
    identity = unit["id"]
    if not re.fullmatch(r"unit-[a-f0-9]{24}", identity):
        raise ValueError("Reusable extract unit identity is invalid")
    relative = f"extract-batches/accepted/{identity}.json"
    path = _inside(root, relative)
    if not path.is_file():
        return None
    origin = read(path)
    child = origin["child"]
    if (not isinstance(child, str) or not re.fullmatch(r"extract_batch-[a-f0-9]{24}", child)
            or origin["unit_id"] != identity or origin["unit_index"] != index
            or origin["unit_path"] != f"units/0/{index:08d}.json"
            or origin["identity"] != child + f"::index-{index:08d}.json"
            or alias(origin["identity"]) != identity
            or _file_sha(root / origin["unit_path"]) != origin["unit_sha256"]):
        raise ValueError("Accepted extraction origin changed its exact unit or native identity")
    metadata_path = _inside(root, f"children/{child}/metadata.json")
    metadata = read(metadata_path)
    if metadata.get("stage") != EXTRACT_BATCH_STAGE or not metadata.get("task_id"):
        raise ValueError("Reusable extraction origin has no accepted native task identity")
    request, _, rows, texts, _ = _verified(root, metadata)
    assigned = [(row, text) for row, text in zip(rows, texts, strict=True)
                if row["context"]["unit_index"] == index]
    if len(assigned) != 1:
        raise ValueError("Reusable extraction origin is not one exact native file assignment")
    row, packet_text = assigned[0]
    if (origin["context"] != row["context"] or origin["packet_file"] != row["local_path"]
            or origin["packet_sha256"] != sha(packet_text)
            or origin["request_file"] != metadata["request_file"]
            or origin["request_sha256"] != sha(json_text(request))
            or origin["compiler_file"] != metadata["compiler_file"]
            or origin["compiler_sha256"] != metadata["compiler_sha256"]
            or origin["compiler_source_sha256"] != metadata["compiler_source_sha256"]
            or origin["result_file"] != f"children/{child}/index-{index:08d}.json.raw"
            or _file_sha(_inside(root, origin["result_file"])) != origin["result_sha256"]):
        raise ValueError("Reusable extraction origin changed its frozen packet, compiler or result")
    files = {
        key: {"path": origin[key], "sha256": _file_sha(_inside(root, origin[key]))}
        for key in ("packet_file", "request_file", "compiler_file")
    }
    files.update(
        result={"path": origin["result_file"], "sha256": origin["result_sha256"]},
        accepted_origin={"path": relative, "sha256": _file_sha(path)},
        native_metadata={"path": str(metadata_path.relative_to(root)), "sha256": _file_sha(metadata_path)},
    )
    return {"context": origin["context"], "files": files, "child": origin["identity"],
            "compiler_source_sha256": origin["compiler_source_sha256"]}


def _source_unit_extract_key(root: Path, context: dict, prefix: str, sources: dict) -> str:
    from .source_unit_extraction import verify_source_unit_context
    from .source_units import load_source_unit

    _, _, _, json_text, _, _, sha = _host()
    verify_source_unit_context(root, context)
    return sha(json_text({
        role: [_source_unit_key(load_source_unit(root, identity), prefix, sources) for identity in context[role]]
        for role in ("assigned_unit_ids", "source_unit_ids")
    }))


def _prepare_source_unit_extract_cache(root: Path, prior_root: Path, previous: Path, header: dict,
                                      prior_state: dict, sources: dict) -> None:
    from .source_unit_extraction import bind_source_unit_index

    *_, read, _, _ = _host()
    for index in range(prior_state["unit_count"]):
        unit_path = prior_root / "units/0" / f"{index:08d}.json"
        unit = read(unit_path)
        if unit.get("source_unit_protocol") != SOURCE_UNIT_PROTOCOL:
            raise ValueError("Exact reviewed extraction lost its accepted source-unit protocol")
        child = _accepted_extract_batch(prior_root, unit, index)
        reused = _record(prior_root / "reuse/applied/extract", unit["id"]) if child is None else None
        if child:
            if child["compiler_source_sha256"] != header["source_unit_extract_compiler_sha256"]:
                continue
            context = child["context"]
            packet = read(_inside(prior_root, child["files"]["packet_file"]["path"]))
            raw = read(_inside(prior_root, child["files"]["result"]["path"]))
            if bind_source_unit_index(prior_root, raw, packet, context, child["child"]) != unit:
                raise ValueError("Reviewed exact extraction differs from its immutable native result")
        elif reused:
            if (reused.get("source_unit_protocol") != SOURCE_UNIT_PROTOCOL
                    or reused["unit_sha256"] != _file_sha(unit_path)):
                raise ValueError("Previously reused exact extraction changed its frozen unit")
            context = reused["context"]
        else:
            continue
        key = _source_unit_extract_key(prior_root, context, header["previous_prefix"], sources)
        _write_record(_cache_folder(root, header, "extract"), key, {
            "source_unit_protocol": SOURCE_UNIT_PROTOCOL,
            "unit_path": str(unit_path.relative_to(previous)), "unit_sha256": _file_sha(unit_path),
            "context": context, "files": child["files"] if child else {}, "source_round": header["round"] - 1,
        })
        header["extract_count"] += 1


def prepare_round_reuse(job: dict, root: Path, state: dict, frozen_entries: list[dict]) -> None:
    """Freeze only local dependency metadata; existing states and requests are never migrated."""
    if state.get("execution_protocol") != OPTIMIZED_EXECUTION_PROTOCOL:
        return
    alias, bind_unit, canonical_extract, json_text, read, save, sha = _host()
    folder, number = root / "reuse", int(job["current_round"])
    base = (root / "compiler-base.md").read_text(encoding="utf-8")
    writer_compilers = _writer_compilers(base, state)
    header = {
        "schema_version": ROUND_REUSE_PROTOCOL, "job_id": job["job_id"], "round": number,
        "enabled": False, "reason": "No preceding reviewed round", "extract_count": 0, "candidate_count": 0,
        "language_policy": state["language_policy"], "compilers": state["compilers"],
        "current_prefix": job["source_snapshot"],
        "writer_compilers": writer_compilers,
        "protocols": {key: state.get(key) for key in PROTOCOL_FIELDS},
    }
    current_sources = _sources(frozen_entries, root.parent)
    save(folder / "sources.json", {"current": current_sources, "previous": {}})
    if number > 1:
        job_dir = root.parent.parent.parent
        previous = _inside(job_dir, f"rounds/round-{number - 1}")
        checkpoint = previous / "checkpoint_submission.json"
        if checkpoint.is_file():
            review = read(checkpoint)
            if review.get("round") != number - 1 or review.get("action") != "continue":
                raise ValueError("Round reuse needs the immediately preceding reviewed checkpoint")
            input_record, metadata = read(previous / "input.json"), read(previous / "meta.json")
            prefix = f"{RUNS_ROOT}/{job['job_id']}/rounds/round-{number - 1}/source"
            original_prefix = f"{RUNS_ROOT}/{job['job_id']}/rounds/round-1/source"
            if (input_record.get("source_snapshot") != prefix or metadata.get("round") != number - 1
                    or input_record.get("original_source_snapshot") != original_prefix):
                raise ValueError("Round reuse archive does not belong to this job and previous round")
            prior_root = previous / "input/staged"
            prior_state = read(prior_root / "state.json")
            if not prior_state.get("complete") or prior_state.get("round") != number - 1:
                raise ValueError("Round reuse cannot consume an unfinished previous round")
            previous_sources = _sources(read(previous / "input/manifest.json"), previous / "input")
            save(folder / "sources.json", {"current": current_sources, "previous": previous_sources})
            policy = compiler_business_policy(base)
            old_policy = compiler_business_policy((prior_root / "compiler-base.md").read_text(encoding="utf-8"))
            compatible = policy == old_policy and state["language_policy"] == prior_state["language_policy"]
            protocols_match = all(state.get(key) == prior_state.get(key) for key in PROTOCOL_FIELDS)
            header.update(
                enabled=True, reason="Previous round was reviewed", previous_root=str(previous),
                previous_prefix=prefix, business_policy_sha256=sha(json_text(policy)),
                previous_state_sha256=_file_sha(prior_root / "state.json"),
                checkpoint_sha256=_file_sha(checkpoint), archive_meta_sha256=_file_sha(previous / "meta.json"),
                previous_input_sha256=_file_sha(previous / "input.json"),
                feedback=_feedback_impact(review, prefix, original_prefix, previous_sources),
                feedback_submission_sha256=sha(json_text([
                    job.get("last_checkpoint_submission"), job.get("supplements"),
                ])), revision_sha256=_file_sha(root / "revision.txt"),
            )
            header["cache_namespace"] = sha(json_text([
                header, _file_sha(folder / "sources.json"),
            ]))
            if any(key not in previous_sources for key, row in current_sources.items() if row["kind"] == "source"):
                header["feedback"]["global"] = True
                header["feedback"]["reasons"].append("New original Evidence affects every candidate")
            exact_units = state.get("source_unit_protocol") == SOURCE_UNIT_PROTOCOL
            previous_exact = prior_state.get("source_unit_protocol") == SOURCE_UNIT_PROTOCOL
            extract_compatible = compatible and state["compilers"]["extract"] == prior_state["compilers"]["extract"]
            if exact_units or previous_exact:
                header["extract_reuse_reason"] = (
                    "Source-unit protocol changed; archived chunk indexes are not interchangeable"
                )
                if extract_compatible and exact_units and previous_exact:
                    from .source_unit_extraction import render_source_unit_extract_compiler

                    previous_base = (prior_root / "compiler-base.md").read_text(encoding="utf-8")
                    source_renderer = sha(render_source_unit_extract_compiler(base))
                    if source_renderer == sha(render_source_unit_extract_compiler(previous_base)):
                        header["source_unit_extract_compiler_sha256"] = source_renderer
                        _prepare_source_unit_extract_cache(root, prior_root, previous, header, prior_state,
                                                          previous_sources)
                        header["extract_reuse_reason"] = (
                            "Exact original units, structural context and source compiler match"
                        )
            elif extract_compatible:
                # Use disk locators, rather than retaining every prior packet or source body in RAM.
                for path in sorted((prior_root / "children").glob("*/metadata.json")):
                    child = read(path)
                    if child.get("stage") != "extract" or not child.get("task_id"):
                        continue
                    packet = read(_inside(prior_root, child["packet_file"]))
                    request = read(_inside(prior_root, child["request_file"]))
                    if (sha(json_text(packet)) != child["packet_sha256"]
                            or sha(json_text(request)) != child["request_sha256"]
                            or _file_sha(_inside(prior_root, child["compiler_file"])) != child["compiler_sha256"]):
                        raise ValueError("Previous extract frozen request or compiler changed")
                    # Before this protocol, later-round extraction also consumed revision context.
                    if number > 2 and prior_state.get("round_reuse_protocol") != ROUND_REUSE_PROTOCOL:
                        continue
                    _write_record(_cache_folder(root, header, "children"), alias(child["child"]), {
                        "context": child["context"],
                        "files": _dependency_files(prior_root, child), "child": child["child"],
                    })
                for index in range(prior_state["unit_count"]):
                    unit_path = prior_root / "units/0" / f"{index:08d}.json"
                    unit = read(unit_path)
                    child = _record(_cache_folder(root, header, "children"), unit["id"])
                    if child is None:
                        child = _accepted_extract_batch(prior_root, unit, index)
                    reused = _record(prior_root / "reuse/applied/extract", unit["id"]) if child is None else None
                    if child:
                        context = child["context"]
                        packet = read(_inside(prior_root, child["files"]["packet_file"]["path"]))
                        value = canonical_extract(
                            read(_inside(prior_root, child["files"]["result"]["path"])),
                            input_ids=context["input_ids"], source_refs=context["source_refs"],
                            reference_map=packet["derived_controls"]["reference_map"], evidence=packet["evidence"],
                        )
                        value = planning.validate_index(value, input_ids=context["input_ids"],
                                                        source_refs=context["source_refs"],
                                                        routing=value.get("schema_version") == planning.INDEX_SCHEMA_V2)
                        parents = dict(zip(context["source_refs"], [[ordinal] for ordinal in context["ordinals"]]))
                        if bind_unit(value, parents, child["child"]) != unit:
                            raise ValueError("Previous accepted extract unit differs from its frozen result")
                        chunks = packet["evidence"]
                    elif reused:
                        if reused["unit_sha256"] != _file_sha(unit_path):
                            raise ValueError("Previously reused extract unit changed")
                        context = reused["context"]
                        chunks = [read(prior_root / "chunks" / f"{ordinal:08d}.json")
                                  for ordinal in context["ordinals"]]
                    else:
                        continue
                    planning.validate_chunks(chunks)
                    key = sha(json_text([_chunk_key(chunk, prefix, previous_sources) for chunk in chunks]))
                    _write_record(_cache_folder(root, header, "extract"), key, {
                        "unit_path": str(unit_path.relative_to(previous)), "unit_sha256": _file_sha(unit_path),
                        "context": context, "files": child["files"] if child else {},
                        "source_round": number - 1,
                    })
                    header["extract_count"] += 1
            required_stages = ("skill_section", "evaluation", "case")
            prior_writers = prior_state.get("aux_compilers") or {}
            if prior_state.get("round_reuse_protocol") == ROUND_REUSE_PROTOCOL:
                prior_manifest = prior_root / "reuse/manifest.json"
                _verify_file(prior_manifest, prior_state["round_reuse_manifest_sha256"])
                prior_writers = {**read(prior_manifest).get("writer_compilers", {}), **prior_writers}
            writers_match = all(prior_writers.get(key) == digest for key, digest in writer_compilers.items())
            if (compatible and writers_match and protocols_match
                    and all(state["compilers"][key] == prior_state["compilers"][key] for key in required_stages)):
                plan_path = previous / "reviewed_output/semantic_reports/capability-plan.json"
                if not plan_path.is_file():
                    plan_path = previous / "reviewed_output/analysis/capability-plan.json"
                plan = read(plan_path)
                original_plan = read(prior_root / "plan.json")
                original_by_name = {row["name"]: row for row in original_plan["skills"]}
                for candidate in plan["skills"]:
                    name = candidate["name"]
                    if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", name) or name not in original_by_name:
                        continue
                    if _canonical(candidate) != _canonical(original_by_name[name]):
                        continue
                    artifacts = {}
                    for filename in ("SKILL.md", "EVALUATION.md", "benchmark.json", "BENCHMARK.md"):
                        relative = f"compiled_skill/{name}/{filename}"
                        path = _inside(previous, "reviewed_output/" + relative)
                        digest = _file_sha(path)
                        if digest != metadata["output_sha256"].get(relative):
                            break  # Human edits are review context, not an already validated cache entry.
                        artifacts[filename] = {"path": "reviewed_output/" + relative, "sha256": digest}
                    if len(artifacts) != 4:
                        continue
                    scope = read(prior_root / "scopes" / f"{name}.json")
                    if exact_units and scope.get("source_units", {}).get("protocol") != SOURCE_UNIT_PROTOCOL:
                        header["candidate_reuse_reason"] = "Candidate lacks verified exact source-unit membership"
                        continue
                    if scope.get("scope_protocol") and scope.get("method_lineage_verified") is not True:
                        continue
                    try:
                        document = read(_inside(previous, artifacts["benchmark.json"]["path"]))
                        cases = document["cases"]
                        identities = [case["case_id"] for case in cases]
                        if benchmark_format.validate_document(document, expected_skill_name=name):
                            continue
                        planning.assemble_dataset(cases, skill_name=name, task=candidate["task"],
                                                  identity="reviewed-reuse-validation", expected_case_ids=identities)
                        skill = _skill_metadata(_inside(previous, artifacts["SKILL.md"]["path"]))
                        if skill is None or skill.get("name") != name:
                            continue
                        if not _inside(previous, artifacts["EVALUATION.md"]["path"]).stat().st_size:
                            continue
                    except (ValueError, KeyError, TypeError):
                        continue
                    _write_record(_cache_folder(root, header, "candidates"), name, {
                        "canonical": _canonical(candidate),
                        "scope": _profile(prior_root, scope, prefix, previous_sources),
                        "artifacts": artifacts, "source_refs": candidate["source_refs"], "source_round": number - 1,
                    })
                    header["candidate_count"] += 1
    header["sources_sha256"] = _file_sha(folder / "sources.json")
    save(folder / "manifest.json", header)
    state.update(
        round_reuse_protocol=ROUND_REUSE_PROTOCOL,
        round_reuse_manifest_sha256=_file_sha(folder / "manifest.json"),
    )


def _load(job: dict, root: Path, state: dict) -> tuple[dict, dict] | None:
    _, _, _, json_text, read, _, sha = _host()
    if state.get("round_reuse_protocol") != ROUND_REUSE_PROTOCOL:
        return None
    folder = root / "reuse"
    _verify_file(folder / "manifest.json", state["round_reuse_manifest_sha256"])
    header = read(folder / "manifest.json")
    if header["job_id"] != job["job_id"] or header["round"] != int(job["current_round"]):
        raise ValueError("Round reuse cannot cross jobs or rounds")
    if not header["enabled"]:
        return None
    sources_path = folder / "sources.json"
    sources = _checked_sources(str(sources_path), header["sources_sha256"], tuple(_stat(sources_path)))
    previous = Path(header["previous_root"])
    expected = root.parent.parent.parent / "rounds" / f"round-{header['round'] - 1}"
    if previous != expected or previous.is_symlink():
        raise ValueError("Round reuse archive escaped the immediately preceding round")
    for relative, field in (("input/staged/state.json", "previous_state_sha256"),
                            ("checkpoint_submission.json", "checkpoint_sha256"),
                            ("meta.json", "archive_meta_sha256"), ("input.json", "previous_input_sha256")):
        _verify_file(_inside(previous, relative), header[field])
    if header["language_policy"] != state["language_policy"] or header["compilers"] != state["compilers"]:
        raise ValueError("Round reuse current compiler or language dependency changed")
    if "protocols" in header:
        recorded = header["protocols"]
        # A frozen earlier manifest omits later optional protocols. Missing and
        # None are equivalent only while that feature is also absent in state;
        # actual changes and unknown keys still reject the frozen dependency.
        if (not isinstance(recorded, dict) or set(recorded) - set(PROTOCOL_FIELDS)
                or any(recorded.get(key) != state.get(key) for key in PROTOCOL_FIELDS)):
            raise ValueError("Round reuse extraction, workflow or scope protocol changed")
    _verify_file(root / "compiler-base.md", state["base_sha256"])
    for stage, digest in state["compilers"].items():
        _verify_file(root / "compilers" / f"{stage}.md", digest)
    for stage, digest in header["writer_compilers"].items():
        if stage in state.get("aux_compilers", {}):
            if state["aux_compilers"][stage] != digest:
                raise ValueError("Round reuse actual artifact batch compiler changed")
            _verify_file(root / "aux-compilers" / f"{stage}.md", digest)
    _verify_file(root / "revision.txt", header["revision_sha256"])
    if sha(json_text([job.get("last_checkpoint_submission"), job.get("supplements")])) != header[
        "feedback_submission_sha256"
    ]:
        raise ValueError("Round reuse feedback or supplemental Evidence dependency changed")
    return header, sources


def try_reuse_extract(job: dict, root: Path, state: dict, chunks: list[dict], context: dict) -> bool:
    if state.get("source_unit_protocol") == SOURCE_UNIT_PROTOCOL:
        # Exact-unit callers use the separate complete-assignment path below.
        # Never reinterpret semantic unit ordinals as old transport chunk ordinals.
        return False
    loaded = _load(job, root, state)
    if loaded is None:
        return False
    alias, bind_unit, _, json_text, read, save, sha = _host()
    header, sources = loaded
    planning.validate_chunks(chunks)
    descriptors = [_chunk_key(chunk, header["current_prefix"], sources["current"]) for chunk in chunks]
    record, width, key = None, 0, ""
    # An appended supplement may share the next raw packet with an old complete extraction assignment.
    # Reuse only that complete prefix; the newly added Evidence is still extracted in the next iteration.
    for width in range(len(chunks), 0, -1):
        key = sha(json_text(descriptors[:width]))
        record = _record(_cache_folder(root, header, "extract"), key)
        if record is not None:
            break
    if record is None:
        return False
    if context["after"] != state["cursor"] + len(chunks):
        raise ValueError("Reusable extract cursor differs from the current complete raw assignment")
    context = {
        **context, "after": state["cursor"] + width, "ordinals": context["ordinals"][:width],
        "input_ids": context["input_ids"][:width], "source_refs": context["source_refs"][:width],
    }
    chunks = chunks[:width]
    previous, prior_root = Path(header["previous_root"]), Path(header["previous_root"]) / "input/staged"
    path = _inside(previous, record["unit_path"])
    if _file_sha(path) != record["unit_sha256"]:
        raise ValueError("Reusable extract unit changed")
    for file in record["files"].values():
        if _file_sha(_inside(prior_root, file["path"])) != file["sha256"]:
            raise ValueError("Reusable extract frozen result or request changed")
    unit = read(path)
    if state.get("extract_batch_protocol") and unit["value"].get("schema_version") != planning.INDEX_SCHEMA_V2:
        # A V1 index has no accepted routing classification. The fresh V2 scan must re-extract it.
        return False
    old_ordinals = record["context"]["ordinals"]
    if len(old_ordinals) != len(chunks) or context["ordinals"] != [chunk["ordinal"] for chunk in chunks]:
        raise ValueError("Reusable extract context differs from the current complete assignment")
    mapping = dict(zip(old_ordinals, context["source_refs"]))
    value = copy.deepcopy(unit["value"])
    value["input_ids"] = context["input_ids"]
    for rows in (value["methods"], value["facts"], value["deferred"]):
        for row in rows:
            row["source_refs"] = list(dict.fromkeys(mapping[ordinal] for ref in row["source_refs"]
                                                   for ordinal in unit["bindings"][ref]))
    if value.get("schema_version") == planning.INDEX_SCHEMA_V2:
        value["context_refs"] = list(dict.fromkeys(mapping[ordinal] for ref in value["context_refs"]
                                                  for ordinal in unit["bindings"][ref]))
    planning.validate_index(value, input_ids=context["input_ids"], source_refs=context["source_refs"],
                            routing=value.get("schema_version") == planning.INDEX_SCHEMA_V2)
    identity = f"reuse:{header['round']}:{key}:{record['unit_sha256']}"
    parents = dict(zip(context["source_refs"], [[ordinal] for ordinal in context["ordinals"]]))
    rebound = bind_unit(value, parents, identity)
    target = root / "units/0" / f"{state['unit_count']:08d}.json"
    save(target, rebound)
    _write_record(root / "reuse/applied/extract", alias(identity), {
        "source_round": header["round"] - 1, "source_unit_sha256": record["unit_sha256"],
        "unit_sha256": _file_sha(target), "context": context, "source_job": job["job_id"],
        "reason": "Exact unchanged Evidence, extraction compiler and language; no review context treated as facts",
    })
    state["unit_count"] += 1
    state["cursor"] = context["after"]
    state["reused_extract_count"] = int(state.get("reused_extract_count", 0)) + 1
    return True


def try_reuse_source_unit_extract(job: dict, root: Path, state: dict, context: dict) -> bool:
    """Rebind one complete reviewed semantic-unit assignment, including all structural context."""
    from .source_unit_extraction import bind_source_unit_index, build_source_unit_packet, next_source_unit_assignment
    from .source_units import load_source_unit

    if state.get("source_unit_protocol") != SOURCE_UNIT_PROTOCOL:
        return False
    loaded = _load(job, root, state)
    if loaded is None:
        return False
    alias, _, _, _, read, save, _ = _host()
    header, sources = loaded
    if not header.get("source_unit_extract_compiler_sha256"):
        return False
    if context["before"] != state["source_unit_cursor"] or context["unit_index"] != state["unit_count"]:
        raise ValueError("Reusable exact extraction changed its assigned cursor")
    _source_unit_extract_key(root, context, header["current_prefix"], sources["current"])
    record, key = None, ""
    # An appended human review may share a packet with a complete old assignment.
    # Reuse that exact primary prefix, never a partial previous model output or unit.
    for width in range(context["after"] - context["before"], 0, -1):
        selected = next_source_unit_assignment(
            root, cursor=context["before"], count=context["after"], unit_index=context["unit_index"],
            language_policy=state["language_policy"], max_units=width,
        )
        key = _source_unit_extract_key(root, selected, header["current_prefix"], sources["current"])
        record = _record(_cache_folder(root, header, "extract"), key)
        if record is not None:
            context = selected
            break
    if record is None:
        return False
    if (context["before"] != state["source_unit_cursor"] or context["unit_index"] != state["unit_count"]
            or record.get("source_unit_protocol") != SOURCE_UNIT_PROTOCOL):
        raise ValueError("Reusable exact extraction changed its assigned cursor or source-unit protocol")
    previous, prior_root = Path(header["previous_root"]), Path(header["previous_root"]) / "input/staged"
    path = _inside(previous, record["unit_path"])
    _verify_file(path, record["unit_sha256"])
    for item in record["files"].values():
        _verify_file(_inside(prior_root, item["path"]), item["sha256"])
    if _source_unit_extract_key(prior_root, record["context"], header["previous_prefix"], sources["previous"]) != key:
        raise ValueError("Reusable exact extraction changed its original source assignment")
    unit = read(path)
    old_ids = record["context"]["source_unit_ids"]
    new_ids = context["source_unit_ids"]
    mapping = dict(zip(old_ids, context["source_refs"], strict=True))
    value = copy.deepcopy(unit["value"])
    value["input_ids"] = list(context["input_ids"])
    for rows in (value["methods"], value["facts"], value["deferred"]):
        for row in rows:
            row["source_refs"] = list(dict.fromkeys(mapping[identity] for ref in row["source_refs"]
                                                   for identity in unit["source_unit_bindings"][ref]))
    value["context_refs"] = list(dict.fromkeys(mapping[identity] for ref in value["context_refs"]
                                              for identity in unit["source_unit_bindings"][ref]))
    # Compare every selected unit's precise range and source ownership, not merely the packet hash.
    for old, new in zip(old_ids, new_ids, strict=True):
        if (_source_unit_key(load_source_unit(prior_root, old), header["previous_prefix"], sources["previous"])
                != _source_unit_key(load_source_unit(root, new), header["current_prefix"], sources["current"])):
            raise ValueError("Reusable exact unit escaped its unchanged original source range")
    identity = f"reuse:{header['round']}:{key}:{record['unit_sha256']}"
    packet = build_source_unit_packet(root, context, state["language_policy"])
    rebound = bind_source_unit_index(root, value, packet, context, identity)
    target = root / "units/0" / f"{state['unit_count']:08d}.json"
    existing = read(target) if target.exists() else None
    if existing is not None and existing != rebound:
        raise ValueError("Durable exact extraction reuse changed its accepted output")
    save(target, rebound)
    _write_record(root / "reuse/applied/extract", alias(identity), {
        "source_unit_protocol": SOURCE_UNIT_PROTOCOL, "source_round": header["round"] - 1,
        "source_unit_sha256": record["unit_sha256"], "unit_sha256": _file_sha(target),
        "context": context, "source_job": job["job_id"],
        "reason": "Exact unchanged complete source units and structural context; reviewed source compiler and language",
    })
    state["unit_count"] += 1
    state["source_unit_cursor"] = context["after"]
    state["reused_extract_count"] = int(state.get("reused_extract_count", 0)) + 1
    return True


def _text_chunks(path: Path):
    with path.open("r", encoding="utf-8") as stream:
        for chunk in iter(lambda: stream.read(64 * 1024), ""):
            yield chunk


def try_reuse_candidate(job: dict, root: Path, state: dict, candidate: dict, scope: dict, repository) -> bool:
    loaded = _load(job, root, state)
    if loaded is None:
        return False
    _, _, _, _, read, save, _ = _host()
    header, sources = loaded
    name = candidate["name"]
    if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", name):
        raise ValueError("Round reuse needs the exact canonical candidate name")
    record = _record(_cache_folder(root, header, "candidates"), name)
    if (state.get("source_unit_protocol") == SOURCE_UNIT_PROTOCOL
            and scope.get("source_units", {}).get("protocol") != SOURCE_UNIT_PROTOCOL):
        _write_record(root / "reuse/misses", name, {
            "name": name, "reason": "Candidate lacks verified exact source-unit membership",
            "source_round": header["round"] - 1,
        })
        return False
    profile = _profile(root, scope, header["current_prefix"], sources["current"])
    reason = "No identical validated previous candidate"
    if scope.get("scope_protocol") and scope.get("method_lineage_verified") is not True:
        reason = "The precise candidate workflow lineage was not completely verified"
    elif header["feedback"]["global"]:
        reason = "; ".join(header["feedback"]["reasons"])
    elif set(profile["paths"]) & set(header["feedback"]["paths"]):
        reason = "Source-scoped review questions or supplemental assertions affect this candidate"
    elif record and record["canonical"] == _canonical(candidate) and record["scope"] == profile:
        previous = Path(header["previous_root"])
        artifacts = {key: _inside(previous, item["path"]) for key, item in record["artifacts"].items()}
        for key, path in artifacts.items():
            if _file_sha(path) != record["artifacts"][key]["sha256"]:
                raise ValueError("Reusable reviewed candidate artifact changed")
        metadata = _skill_metadata(artifacts["SKILL.md"])
        if metadata is None or metadata.get("name") != name or not artifacts["EVALUATION.md"].stat().st_size:
            raise ValueError("Reusable Skill or evaluation is incomplete")
        document = read(artifacts["benchmark.json"])
        errors = benchmark_format.validate_document(document, expected_skill_name=name)
        if errors:
            raise ValueError("Reusable Test Dataset is invalid: " + "; ".join(errors))
        cases = copy.deepcopy(document["cases"])
        identities = [case["case_id"] for case in cases]
        original_refs = []
        for case in cases:
            refs = case["provenance"]["session_ids"]
            rebound = []
            for uri in refs:
                relative = _relative(uri, header["previous_prefix"], sources["previous"])
                if (relative not in profile["paths"] or relative not in sources["current"]
                        or sources["previous"][relative]["sha256"] != sources["current"][relative]["sha256"]):
                    raise ValueError("Reusable Case provenance does not match this candidate's unchanged scope")
                original_refs.append(uri)
                rebound.append(header["current_prefix"] + "/" + relative)
            case["provenance"]["session_ids"] = rebound
            planning.validate_case(case, case_id=case["case_id"], skill_name=name, source_refs=rebound)
        planning.assemble_dataset(cases, skill_name=name, task=candidate["task"], identity="reuse-validation",
                                  expected_case_ids=identities)
        for filename in ("SKILL.md", "EVALUATION.md"):
            repository.write_output_stream(job["job_id"], f"compiled_skill/{name}/{filename}",
                                           _text_chunks(artifacts[filename]))
        for index, case in enumerate(cases):
            save(root / "cases" / name / f"{index:02d}.json", case)
        audit = {
            "source_job": job["job_id"], "source_round": header["round"] - 1, "name": name,
            "case_ids": identities, "artifacts": record["artifacts"], "scope": profile,
            "original_source_refs": list(dict.fromkeys(original_refs)),
            "current_source_refs": list(dict.fromkeys(
                ref for case in cases for ref in case["provenance"]["session_ids"]
            )),
            "reason": "Reviewed unchanged candidate; full twelve Cases retained and provenance rebound exactly",
        }
        existing = _record(root / "reuse/applied/candidates", name)
        if existing is not None and existing != audit:
            raise ValueError("Durable candidate reuse audit changed")
        _write_record(root / "reuse/applied/candidates", name, audit)
        names = state.setdefault("reused_candidate_names", [])
        if name not in names:
            names.append(name)
        state["reused_candidate_count"] = len(names)
        return True
    _write_record(root / "reuse/misses", name, {"name": name, "reason": reason, "source_round": header["round"] - 1})
    return False
