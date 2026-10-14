"""Durable bounded Skill mining through the unchanged public OpenViking Compile API."""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import math
import os
import re
import shutil
import tempfile
import time
from pathlib import Path

import httpx
import yaml
from fastapi import HTTPException

from . import bounded_compile_plan as planning
from . import compile_admission
from .candidate_identity import IDENTITY_PROTOCOL
from .candidate_identity_orchestration import CandidateIdentityMixin, _selection_known
from .candidate_refinement import (
    BUSINESS_FIELDS,
    REFINEMENT_PROTOCOL,
    commit_refinements,
    prepare_refinements,
)
from .compile_strategy import render_refinement_plan_compiler, stage_compiler_body
from .dataset_view import dataset_markdown
from .deferred_evidence_review import DeferredEvidenceReviewMixin
from .output_language import output_language_instruction, validate_artifact_headings
from .ov_artifact_batch_orchestration import ARTIFACT_BATCH_STAGE, OvArtifactBatchMixin
from .ov_case_batch_orchestration import CASE_BATCH_STAGE, OvCaseBatchOrchestrationMixin
from .ov_compile_mining import RUNS_ROOT, utc_now
from .ov_extract_batch_orchestration import EXTRACT_BATCH_STAGE, OvExtractBatchMixin
from .ov_merge_batch_orchestration import MERGE_BATCH_PROTOCOL, MERGE_BATCH_STAGE, OvMergeBatchMixin
from .ov_workflow_batch_transport import WORKFLOW_BATCH_STAGE, OvWorkflowBatchTransportMixin
from .ov_workflow_planning import OvWorkflowPlanningMixin
from .source_scope import EFFICIENCY_PROTOCOL, SOURCE_SCOPE_PROTOCOL_V3, SOURCE_SCOPE_PROTOCOLS

STAGED_PROTOCOL = "te-staged-compile.v1"
MAX_STAGE_ATTEMPTS = 3
MAX_PLAN_DEPTH = 16
MIN_STAGE_BUDGET_SECONDS = 180
MAX_EXTRACT_RAW_CHARS = 48000
SOURCE_FRAGMENT_PROTOCOL = "te-source-fragments.v1"
SOURCE_INPUT_CLARITY_PROTOCOL = "te-source-input-clarity.v1"
FINE_SOURCE_FRAGMENT_CHARS = 4096
FINE_EXTRACT_MAX_CHUNKS = 3


def _json(value) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def _sha(content: str) -> str:
    return hashlib.sha256(content.encode()).hexdigest()


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".staged-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _save(path: Path, value) -> None:
    _write(path, _json(value) + "\n")


def _read(path: Path):
    if path.is_symlink():
        raise ValueError("分阶段任务缓存不能是符号链接")
    return json.loads(path.read_text(encoding="utf-8"))


def _selection_packet(root: Path, state: dict, current: dict) -> tuple[dict, dict | None]:
    """Restore a frozen host assignment, never a mapping rebuilt from the current catalog."""
    packet = _read(root / current["packet_file"])
    if current.get("context", {}).get("typed_planning") != "selection":
        return packet, None
    enabled = state.get("planning_alias_protocol") == "te-planning-aliases.v1"
    from .direct_workflow_planning import FrozenPlanningIntegrityError

    if not enabled:
        if current.get("planning_alias_protocol"):
            raise FrozenPlanningIntegrityError("Frozen planning alias protocol changed")
        return packet, None
    from .planning_aliases import build_planning_alias_packet, validate_planning_alias_manifest

    folder = f"children/{current['child']}"
    paths = {
        "canonical_packet_file": folder + "/canonical-packet.json",
        "planning_alias_file": folder + "/planning-aliases.json",
    }
    try:
        if current.get("planning_alias_protocol") != state["planning_alias_protocol"] or any(
            current.get(field) != expected for field, expected in paths.items()
        ):
            raise ValueError("Frozen planning alias locators changed")
        canonical = _read(root / paths["canonical_packet_file"])
        manifest = _read(root / paths["planning_alias_file"])
        if (
            _sha((root / current["packet_file"]).read_text(encoding="utf-8")) != current["packet_sha256"]
            or
            _sha((root / paths["canonical_packet_file"]).read_text(encoding="utf-8"))
            != current.get("canonical_packet_sha256")
            or _sha(_json(manifest)) != current.get("planning_alias_sha256")
        ):
            raise ValueError("Frozen planning alias dependency SHA changed")
        validate_planning_alias_manifest(manifest, packet=packet, canonical_packet=canonical)
        expected_packet, expected_manifest = build_planning_alias_packet(canonical)
        if expected_packet != packet or expected_manifest != manifest:
            raise ValueError("Frozen planning aliases do not match the exact canonical assignment")
    except (ValueError, KeyError, TypeError, OSError) as exc:
        raise FrozenPlanningIntegrityError(str(exc)) from exc
    return canonical, manifest


def _canonical_stage_packet(root: Path, state: dict, current: dict) -> tuple[dict, dict | None]:
    """Resolve only aliases frozen for this exact current stage before any side effect."""
    if current.get("stage") != "evidence_review_quotes":
        return _selection_packet(root, state, current)
    from .direct_workflow_planning import FrozenPlanningIntegrityError
    from .evidence_quote_aliases import PROTOCOL, build_quote_alias_packet, validate_quote_alias_manifest

    if state.get("quote_alias_protocol") != PROTOCOL:
        if current.get("quote_alias_protocol"):
            raise FrozenPlanningIntegrityError("Frozen quote alias protocol changed")
        return _read(root / current["packet_file"]), None
    folder = f"children/{current['child']}"
    paths = {
        "canonical_packet_file": folder + "/canonical-packet.json",
        "quote_alias_file": folder + "/quote-aliases.json",
    }
    try:
        if current.get("quote_alias_protocol") != PROTOCOL or any(
            current.get(field) != path for field, path in paths.items()
        ):
            raise ValueError("Frozen quote alias locators changed")
        packet = _read(root / current["packet_file"])
        canonical = _read(root / paths["canonical_packet_file"])
        manifest = _read(root / paths["quote_alias_file"])
        if (
            _sha((root / current["packet_file"]).read_text(encoding="utf-8")) != current["packet_sha256"]
            or _sha((root / paths["canonical_packet_file"]).read_text(encoding="utf-8"))
            != current.get("canonical_packet_sha256")
            or _sha(_json(manifest)) != current.get("quote_alias_sha256")
        ):
            raise ValueError("Frozen quote alias dependency SHA changed")
        validate_quote_alias_manifest(manifest, packet=packet, canonical_packet=canonical)
        expected_packet, expected_manifest = build_quote_alias_packet(canonical)
        if packet != expected_packet or manifest != expected_manifest:
            raise ValueError("Frozen quote aliases differ from the exact canonical assignment")
    except (ValueError, KeyError, TypeError, OSError) as exc:
        raise FrozenPlanningIntegrityError(str(exc)) from exc
    return canonical, manifest


def _capacity_reason_limit(state: dict) -> int:
    return planning.MAX_OUTPUT_CHARS if state.get("compact_planning_protocol") == "te-compact-planning.v1" else 1000


def _decode_selection_aliases(value, manifest: dict, *, max_capacity_reason_chars: int = 1000, max_skill_rows: int = 8):
    from .direct_workflow_planning import SelectionOutputValidationError
    from .planning_aliases import decode_planning_alias_output

    try:
        return decode_planning_alias_output(value, manifest, max_capacity_reason_chars=max_capacity_reason_chars,
                                            max_skill_rows=max_skill_rows)
    except (ValueError, KeyError, TypeError) as exc:
        raise SelectionOutputValidationError(str(exc)) from exc


def _selection_alias_result(value, state: dict, manifest: dict | None) -> tuple[dict, int]:
    """Only verified machine-ID restoration may expand an already bounded model output."""
    if manifest is None:
        return value, planning.MAX_OUTPUT_CHARS
    compact = state.get("compact_planning_protocol") == "te-compact-planning.v1"
    encoded_chars = len(_json(value))
    decoded = _decode_selection_aliases(
        value, manifest, max_capacity_reason_chars=_capacity_reason_limit(state),
        max_skill_rows=len(manifest["methods"]) if compact else 8,
    )
    delta = max(0, len(_json(decoded)) - encoded_chars) if compact else 0
    return decoded, planning.MAX_OUTPUT_CHARS + delta


def _direct_record(root: Path, index: int) -> dict:
    """Direct planning preserves the verified catalog record, including every source binding."""
    mapping = _read(root / "direct-planning/mapping" / f"{index:08d}.json")
    record = _read(root / "proposals" / f"{index:08d}.json")
    original = _read(root / "catalog/method" / f"{mapping['catalog_index']:08d}.json")
    if (record != original or mapping["item_id"] != record["item"]["item_id"]
            or mapping["record_sha256"] != _sha(_json(record))):
        from .direct_workflow_planning import FrozenPlanningIntegrityError

        raise FrozenPlanningIntegrityError("Frozen direct planning method or exact source lineage changed")
    return record


def _alias(identity: str) -> str:
    return "unit-" + _sha(identity)[:24]


def _project(value: dict, alias: str) -> dict:
    """A unit carries row-specific short scopes, never its expanded original source list."""
    return {key: item for key, item in value.items() if key != "input_ids"}


def _canonical_extract_references(
    value, *, input_ids: list[str], source_refs: list[str], reference_map: dict, evidence: list[dict] | None = None
):
    """Normalize exact chunk references and host-owned assignment metadata, without changing facts."""
    if (
        not isinstance(reference_map, dict)
        or len(input_ids) != len(source_refs)
        or any(not isinstance(item, str) or not item for item in [*input_ids, *source_refs])
        or len(set(input_ids)) != len(input_ids)
        or len(set(source_refs)) != len(source_refs)
        or reference_map != dict(zip(source_refs, input_ids))
    ):
        raise ValueError("Extract reference_map must exactly and bijectively name the assigned chunks")
    reverse = {chunk_id: alias for alias, chunk_id in reference_map.items()}
    if any(reverse[item] != item for item in set(input_ids) & set(source_refs)):
        raise ValueError("Extract reference_map has ambiguous chunk and alias identifiers")
    uri_aliases = {}
    if evidence is not None:
        if (
            not isinstance(evidence, list)
            or any(not isinstance(chunk, dict) for chunk in evidence)
            or [chunk.get("id") for chunk in evidence] != input_ids
        ):
            raise ValueError("Extract URI references require the exact frozen assigned Evidence chunks")
        for chunk in evidence:
            uri = chunk.get("source_uri")
            if not isinstance(uri, str) or not uri or uri in reverse or uri in source_refs:
                raise ValueError("Extract source URI must unambiguously name an original assigned file")
            uri_aliases.setdefault(uri, []).append(reverse[chunk["id"]])
    canonical = copy.deepcopy(value)
    if isinstance(canonical, dict):
        # The native file envelope uses a separate range-ID namespace. Only row
        # source_refs determine Evidence coverage; model bookkeeping is never a source.
        canonical["input_ids"] = list(input_ids)
        for key in ("methods", "facts", "deferred"):
            if not isinstance(canonical.get(key), list):
                continue
            for row in canonical[key]:
                if isinstance(row, dict) and isinstance(row.get("source_refs"), list):
                    refs, seen = [], set()
                    for ref in row["source_refs"]:
                        if isinstance(ref, str) and ref in reverse:
                            aliases = [reverse[ref]]
                        elif isinstance(ref, str) and ref in source_refs:
                            aliases = [ref]
                        elif isinstance(ref, str) and ref in uri_aliases:
                            # A file URI explicitly selects every range of that file
                            # assigned in this packet; it never selects other packets.
                            aliases = uri_aliases[ref]
                        else:
                            refs.append(ref)
                            continue
                        for alias in aliases:
                            if alias not in seen:
                                refs.append(alias)
                                seen.add(alias)
                    row["source_refs"] = refs
        if canonical.get("schema_version") == planning.INDEX_SCHEMA_V2 and isinstance(
            canonical.get("context_refs"), list
        ):
            refs = []
            for ref in canonical["context_refs"]:
                aliases = ([reverse[ref]] if isinstance(ref, str) and ref in reverse else [ref]
                           if isinstance(ref, str) and ref in source_refs else uri_aliases.get(ref, [ref])
                           if isinstance(ref, str) else [ref])
                for alias in aliases:
                    if alias not in refs:
                        refs.append(alias)
            canonical["context_refs"] = refs
    return canonical


def _bind_unit(value: dict, parents: dict[str, list[int]], identity: str) -> dict:
    bindings = {}

    def visit(item):
        if isinstance(item, list):
            return [visit(child) for child in item]
        if isinstance(item, dict):
            result = {key: visit(child) for key, child in item.items() if key not in {"source_refs", "context_refs"}}
            if "source_refs" in item:
                selected = sorted({ordinal for reference in item["source_refs"] for ordinal in parents[reference]})
                alias = "scope-" + _sha("scope:" + _json(selected))[:24]
                bindings[alias] = selected
                result["source_refs"] = [alias]
            if "context_refs" in item:
                selected = sorted({ordinal for reference in item["context_refs"] for ordinal in parents[reference]})
                result["context_refs"] = []
                if selected:
                    alias = "scope-" + _sha("scope:" + _json(selected))[:24]
                    bindings[alias] = selected
                    result["context_refs"] = [alias]
            return result
        return item

    projected = visit(value)
    result = {
        "id": _alias(identity),
        "value": projected,
        "bindings": bindings,
        "ordinals": sorted({ordinal for selected in bindings.values() for ordinal in selected}),
    }
    if value.get("schema_version") == planning.INDEX_SCHEMA_V2:
        result["context_ordinals"] = sorted({
            ordinal for ref in value["context_refs"] for ordinal in parents[ref]
        })
        result["context_uncertain"] = value["context_uncertain"]
    return result


def _canonical_planning_header(value, *, input_ids: list[str], item_ids: list[str]):
    """Normalize only complete equivalent assignment headers; row dispositions remain strictly validated."""
    if not isinstance(value, dict):
        raise ValueError("Planning output must be a JSON object")
    header = value.get("input_ids")
    if (
        not isinstance(header, list)
        or any(not isinstance(identity, str) or not identity for identity in header)
        or len(set(header)) != len(header)
        or set(header) not in (set(input_ids), set(item_ids))
    ):
        raise ValueError("Planning input_ids must exactly cover assigned container IDs or all assigned method item IDs")
    canonical = copy.deepcopy(value)
    canonical["input_ids"] = list(input_ids)
    return canonical


def _revision_view(text: str, budget: int, skill_name: str = "") -> str:
    """Keep identities while explicitly excerpting nonfactual revision text; full input stays archived."""
    if not text:
        return ""
    start = text.find("{")
    try:
        revision, _ = json.JSONDecoder().raw_decode(text[start:]) if start >= 0 else ({}, 0)
        if not isinstance(revision, dict):
            revision = {}
    except ValueError:
        revision = {}
    candidates = revision.get("previous_candidates") or []
    identities = []
    for row in candidates:
        if not isinstance(row, dict) or (skill_name and row.get("skill_name") != skill_name):
            continue
        name, case_ids = row.get("skill_name"), row.get("case_ids") or []
        identity = {"skill_name": name}
        expected = [f"{name}-case-{index:02d}" for index in range(1, 13)]
        if not skill_name and case_ids == expected:
            identity.update(
                case_id_pattern=f"{name}-case-{{index:02d}}",
                case_id_indexes=list(range(1, 13)),
                case_ids_preserved_as_pattern=True,
            )
        elif not skill_name:
            identity.update(
                case_ids_count=len(case_ids), case_id_mapping_sha256=_sha(_json(case_ids)), exact_case_ids_archived=True
            )
        else:
            identity["case_ids"] = case_ids
        identities.append(identity)

    view = {
        "revision_context_is_evidence": False,
        "previous_candidates": identities,
        "previous_skill_bodies_omitted": True,
        "previous_capability_plan_omitted": True,
        "full_revision_archived": True,
        "feedback": str(revision.get("feedback") or "")[:2000],
        "feedback_truncated": bool(revision.get("feedback_truncated"))
        or len(str(revision.get("feedback") or "")) > 2000,
        "review_questions": [
            {"qid": row.get("qid"), "question": str(row.get("question") or "")[:200]}
            for row in revision.get("review_questions") or []
            if isinstance(row, dict)
        ],
    }
    if not revision:
        view["unparsed_revision_excerpt"] = text[:1000]
        view["revision_context_truncated"] = len(text) > 1000
    prefix = "\n以下修订上下文不是 Evidence；完整版本保存在本轮归档。\n"
    while len(prefix) + len(_json(view)) > budget:
        if view["review_questions"]:
            view["review_questions"].pop()
            view["review_questions_omitted"] = True
        elif view["feedback"]:
            view["feedback"] = view["feedback"][:-200]
            view["feedback_truncated"] = True
        elif view.get("unparsed_revision_excerpt"):
            view["unparsed_revision_excerpt"] = view["unparsed_revision_excerpt"][:-200]
            view["revision_context_truncated"] = True
        else:
            raise ValueError("候选名称及案例身份无法容纳于阶段预算；没有截断身份或 Evidence")
    return prefix + _json(view)


def _package(body: str) -> tuple[str, str]:
    match = re.fullmatch(r"---\s*\n(.*?)\n---\s*\n?(.*)", body, flags=re.DOTALL)
    if match is None:
        raise ValueError("阶段编译 Skill 缺少 frontmatter")
    metadata = yaml.safe_load(match.group(1))
    if not isinstance(metadata, dict) or set(metadata) != {"name", "description"}:
        raise ValueError("阶段编译 Skill frontmatter 无效")
    name = "skill-miner-stage-" + _sha(body)[:32]
    published = (
        "---\n"
        + yaml.dump({"name": name, "description": metadata["description"]}, allow_unicode=True, sort_keys=False)
        + "---\n\n"
        + match.group(2).strip()
    )
    return name, published


def _freeze_chunks(root: Path, entries: list[dict], source_snapshot: str, *, max_chars: int = 12000) -> int:
    """Read one original file at a time; neither packets nor a round queue retain all raw text."""
    count = 0
    for item in entries:
        if item["kind"] == "context":
            continue
        path = Path(item["local_path"])
        if path.is_symlink():
            raise ValueError("冻结 Evidence 文件不能是符号链接")
        text = path.read_text(encoding="utf-8")
        if _sha(text) != item["sha256"]:
            raise ValueError("冻结 Evidence 文件摘要不一致")
        uri = source_snapshot.rstrip("/") + "/" + item["relative_path"]
        file_ordinals = []
        for chunk in planning.source_chunks(text, uri, max_chars=max_chars):
            chunk["ordinal"] = count
            _save(root / "chunks" / f"{count:08d}.json", chunk)
            metadata = {key: value for key, value in chunk.items() if key != "content"}
            metadata["serialized_chars"] = len(_json(chunk))
            _save(root / "chunk-metadata" / f"{count:08d}.json", metadata)
            _save(root / "chunk-ids" / f"{chunk['id']}.json", {"ordinal": count})
            file_ordinals.append(count)
            count += 1
        _save(root / "source-files" / (_sha(uri) + ".json"), {"uri": uri, "ordinals": file_ordinals})
    if not count:
        raise ValueError("没有可编译的来源 Evidence")
    return count


def _chunks(root: Path, ordinals: list[int | str]) -> list[dict]:
    from .source_units import load_source_unit

    return [load_source_unit(root, index) if isinstance(index, str)
            else _read(root / "chunks" / f"{index:08d}.json") for index in ordinals]


def _chunk_metadata(root: Path, ordinals: list[int]) -> list[dict]:
    return [_read(root / "chunk-metadata" / f"{index:08d}.json") for index in ordinals]


def _group_count(root: Path, ordinals: list[int], *, max_chunks: int = 8) -> int:
    if type(max_chunks) is not int or not 1 <= max_chunks <= 8:
        raise ValueError("Evidence group count must remain bounded from one through eight")
    groups, size, count = 0, len(_json({"evidence": []})), 0
    for index in ordinals:
        item = _read(root / "chunk-metadata" / f"{index:08d}.json")
        additional = item["serialized_chars"] + (1 if count else 0)
        if count and (count == max_chunks or size + additional > 24000):
            groups += 1
            size, count = len(_json({"evidence": []})), 0
            additional = item["serialized_chars"]
        size += additional
        count += 1
    return groups + bool(count)


def _raw_group(root: Path, ordinals: list[int], cursor: int, *, max_chunks: int = 8) -> tuple[list[dict], int]:
    if type(max_chunks) is not int or not 1 <= max_chunks <= 8:
        raise ValueError("Evidence groups must remain bounded from one through eight")
    values = _chunks(root, ordinals[cursor : cursor + max_chunks])
    if not values:
        raise ValueError("阶段 Evidence 范围为空")
    group = planning.pack_chunks(values)[0]
    return group, cursor + len(group)


def _method_groups(root: Path, scope: dict) -> list[list[int]]:
    """Scenario work starts from the exact method ranges, rather than unrelated file introductions."""
    if scope.get("source_units"):
        from .source_unit_scope import exact_method_groups

        return exact_method_groups(root, scope)
    selected = scope.get("method_ordinals") or scope["ordinals"]
    if scope.get("scope_protocol") in SOURCE_SCOPE_PROTOCOLS:
        # Keep compact multi-range methods together; shared constraints precede background headings.
        # Remaining context uses bounded anchor pairs, never a method/context cross product.
        context = list(dict.fromkeys(scope.get("shared_context_ordinals", []) + scope["ordinals"]))
        context = [ordinal for ordinal in context if ordinal not in selected]
        groups, cursor, method_cursor = [], 0, 0
        while method_cursor < len(selected):
            chunks = planning.pack_chunks(_chunks(root, selected[method_cursor:method_cursor + 8]),
                                          max_packet_chars=26000)[0]
            method_cursor += len(chunks)
            while cursor < len(context) and len(chunks) < 8:
                extra = _chunks(root, [context[cursor]])[0]
                if len(_json({"evidence": [*chunks, extra]})) > 26000:
                    break
                chunks.append(extra)
                cursor += 1
            groups.append([chunk["ordinal"] for chunk in chunks])
        for index, extra in enumerate(context[cursor:]):
            pair = [selected[index % len(selected)], extra]
            if len(_json({"evidence": _chunks(root, pair)})) > 26000:
                raise ValueError("Exact method and constraint ranges exceed the bounded representative Case packet")
            groups.append(pair)
        return groups
    groups, cursor = [], 0
    while cursor < len(selected):
        chunks, cursor = _raw_group(root, selected, cursor)
        groups.append([chunk["ordinal"] for chunk in chunks])
    return groups


def _prior_candidate_rows(root: Path, *, name: str = "", relative_paths: set[str] | None = None):
    """Read verified per-candidate locators, rather than load all prior candidate bodies."""
    folder = root.parent / "previous-candidates"
    manifest = folder / "manifest.json"
    if not manifest.is_file():
        return
    for locator in _read(manifest):
        if name and locator["name"] != name:
            continue
        path = Path(locator["local_path"])
        if path.is_symlink() or path.resolve().parent != folder.resolve():
            raise ValueError("上一轮候选修订记录必须位于当前轮次的私有目录")
        text = path.read_text(encoding="utf-8")
        if _sha(text) != locator["sha256"]:
            raise ValueError("上一轮候选修订记录摘要不一致")
        row = json.loads(text)
        if row["name"] != locator["name"]:
            raise ValueError("上一轮候选修订身份不一致")
        if name or relative_paths and relative_paths.intersection(row["scope_relative_paths"]):
            yield row


def _assignment_revision(root: Path, job: dict, controls: dict, context: dict, budget: int) -> str:
    prefix = job["source_snapshot"].rstrip("/") + "/"
    uris = list(context.get("source_refs", []))
    if context.get("typed_planning"):
        folder = root / "catalog" / "method" if context["typed_planning"] == "merge" else root / "proposals"
        uris = [
            uri
            for index in context["record_indexes"]
            for uri in _read(folder / f"{index:08d}.json")["item"].get("source_files", [])
        ]
    paths = {uri[len(prefix) :] for uri in uris if uri.startswith(prefix)}
    own_name = context.get("skill_name", "")
    rows, omitted = [], False
    for prior in _prior_candidate_rows(root, name=own_name, relative_paths=paths):
        row = {
            "name": prior["name"],
            "task": prior["task"][:400],
            "boundary": prior["boundary"][:400],
            "skill_body_sha256": prior["skill_body_sha256"],
        }
        ids = prior["case_ids"]
        if own_name:
            row["case_ids"] = ids
            row["skill_body_excerpt"] = prior["skill_body_excerpt"][:3000]
            row["skill_body_excerpt_truncated"] = len(prior["skill_body_excerpt"]) > 3000
        else:
            row["case_ids_count"] = len(ids)
            row["case_id_mapping_sha256"] = _sha(_json(ids))
        rows.append(row)
        if len(rows) > 8 or len(_json(rows)) + 250 > budget:
            rows.pop()
            omitted = True
            if own_name:
                row.pop("skill_body_excerpt", None)
                row["skill_body_excerpt_truncated"] = True
                if len(_json([row])) + 250 > budget:
                    raise ValueError("本候选的精确案例身份超过阶段修订预算")
                rows.append(row)
            break
    if not rows and not omitted:
        return ""
    return "\n" + _json(
        {
            "revision_context_is_evidence": False,
            "assignment_previous_candidates": rows,
            "other_candidates_omitted": omitted,
            "complete_catalog_archived": True,
        }
    )


def _staged_execution_budget(job: dict, state: dict) -> dict:
    """Reserve time for serial phase work; retries of an unchanged request add no work."""
    base = state.setdefault("budget_base_seconds", int(job.get("runtime_timeout_seconds") or 3600))
    completed = int(state.get("completed_tasks", 0))
    pending = 0 if state.get("complete") else 1
    extra_attempts = max(0, int(state.get("attempted_tasks", 0)) - completed - pending)
    known_work = int(state.get("estimated_tasks", 0)) + extra_attempts
    units = max(int(state.get("budget_work_units", 0)), known_work, completed + pending, 1)
    timeout = max(base, math.ceil(units * max(MIN_STAGE_BUDGET_SECONDS, base / 8)))
    state["budget_work_units"] = units
    return {"round_work_units": units, "round_timeout_seconds": timeout}


class OvStagedOrchestrationMixin(
    CandidateIdentityMixin, DeferredEvidenceReviewMixin, OvCaseBatchOrchestrationMixin, OvArtifactBatchMixin,
    OvExtractBatchMixin, OvMergeBatchMixin, OvWorkflowPlanningMixin, OvWorkflowBatchTransportMixin,
):
    """The host owns authentication, repository locking, task polling and cancellation."""

    def _staged_root(self, job: dict) -> Path:
        repository = self._ov_skill_mining_repository()
        return repository._job_dir(job["job_id"]) / "round_inputs" / f"round-{int(job['current_round'])}" / "staged"

    @staticmethod
    def _staged_canonical_packet_for_current(root: Path, state: dict, current: dict):
        return _canonical_stage_packet(root, state, current)

    async def _staged_load(self, job: dict) -> tuple[Path, dict]:
        root = self._staged_root(job)
        state = await asyncio.to_thread(_read, root / "state.json")
        if state.get("protocol") != STAGED_PROTOCOL or state.get("round") != int(job["current_round"]):
            raise ValueError("阶段状态不属于当前任务轮次")
        return root, state

    async def _staged_save(self, job: dict, root: Path, state: dict, **changes) -> dict:
        labels = {
            "extract": "来源抽取",
            "plan": "能力规划",
            "merge_index": "方法合并",
            "selection": "逐项能力选择",
            "skill_section": "Skill 文件生成",
            "evaluation": "评测说明",
            "case": "测试案例生成",
            "assemble": "产物组装",
            "evidence_review": "暂缓方法原文复核",
            "identity_group": "候选身份复核",
            "workflow_partition": "完整方法归组",
            "workflow_metadata": "候选方法说明",
        }
        completed = state.get("completed_tasks", 0)
        if state.get("direct_workflow_protocol") and state["phase"] == "selection":
            labels["selection"] = "完整流程规划"
        state["estimated_tasks"] = (
            completed if state.get("complete") else max(state.get("estimated_tasks", 0), completed + 1)
        )
        progress = {
            "phase": state["phase"],
            "label": labels[state["phase"]],
            "completed": state.get("completed_tasks", 0),
            "total": state.get("estimated_tasks", 0),
            "subtask_index": completed if state.get("complete") else completed + 1,
        }
        budget = _staged_execution_budget(job, state)
        await asyncio.to_thread(_save, root / "state.json", state)
        return await asyncio.to_thread(
            self._ov_skill_mining_repository().update_job,
            job["job_id"],
            staged_protocol=STAGED_PROTOCOL,
            staged_state_path=str(root / "state.json"),
            staged_progress=progress,
            **budget,
            **changes,
        )

    @staticmethod
    def _staged_stopped(job: dict) -> bool:
        return bool(job.get("ov_cancel_reason")) or job.get("status") in {"stopping", "stopped"}

    async def _acquire_staged_compile_slot(self, job: dict, state: dict, current: dict) -> bool:
        if state.get("execution_protocol") != compile_admission.OPTIMIZED_PROTOCOL:
            return True
        repository = self._ov_skill_mining_repository()
        if await asyncio.to_thread(compile_admission.acquire, repository, job, current):
            return True
        # A terminal task may have been observed just before the process stopped.
        # Check only the two durable reservations; never expire a live task by age.
        for row in await asyncio.to_thread(compile_admission.reservations, repository.jobs_root):
            if not row.get("task_id"):
                try:
                    accepted = await self._knowledge_request(
                        row["account"], "GET", f"/api/v1/compile/submissions/{row['key']}", timeout=5.0
                    )
                except (HTTPException, httpx.HTTPError, OSError):
                    continue
                if not isinstance(accepted, dict) or not accepted.get("task_id"):
                    continue
                row["task_id"] = str(accepted["task_id"])
                await asyncio.to_thread(
                    compile_admission.accepted, repository.jobs_root, row["key"], row["task_id"]
                )
            try:
                task = await self._knowledge_request(
                    row["account"], "GET", f"/api/v1/tasks/{row['task_id']}", timeout=5.0
                )
            except (HTTPException, httpx.HTTPError, OSError):
                continue
            if isinstance(task, dict) and task.get("status") in compile_admission.TERMINAL_NATIVE_STATES:
                await asyncio.to_thread(compile_admission.release, repository.jobs_root, task_id=row["task_id"])
        return await asyncio.to_thread(compile_admission.acquire, repository, job, current)

    async def _mark_staged_compile_slot(self, job: dict, current: dict) -> None:
        await asyncio.to_thread(
            compile_admission.accepted, self._ov_skill_mining_repository().jobs_root,
            current["key"], current["task_id"],
        )

    async def _release_staged_compile_slot(self, job: dict, task_id: str = "", *, key: str = "") -> None:
        await asyncio.to_thread(
            compile_admission.release, self._ov_skill_mining_repository().jobs_root, task_id=task_id, key=key
        )

    async def _staged_release_rejected(self, job: dict, root: Path, state: dict, exc: Exception):
        """Release proven rejection only; an unknown transport acceptance keeps its reservation."""
        current = state.get("current") or {}
        if current.get("task_id"):
            return None
        if isinstance(exc, HTTPException) and exc.status_code == 409:
            try:
                accepted = await self._knowledge_request(
                    job["account"], "GET", f"/api/v1/compile/submissions/{current['key']}", timeout=5.0
                )
            except HTTPException as lookup:
                if lookup.status_code != 404:
                    return await self._round_submission_retry(self._ov_skill_mining_repository(), job, lookup)
            except (httpx.HTTPError, OSError) as lookup:
                return await self._round_submission_retry(self._ov_skill_mining_repository(), job, lookup)
            else:
                if not isinstance(accepted, dict) or not accepted.get("task_id"):
                    return await self._round_submission_retry(
                        self._ov_skill_mining_repository(), job,
                        ValueError("Exact-key lookup returned no task identity"),
                    )
                current["task_id"] = str(accepted["task_id"])
                await self._mark_staged_compile_slot(job, current)
                await asyncio.to_thread(_save, root / "children" / current["child"] / "metadata.json", current)
                job = await self._staged_save(job, root, state)
                return await self._record_round_acceptance(
                    self._ov_skill_mining_repository(), job, accepted, time.perf_counter()
                )
            await self._release_staged_compile_slot(job, key=current["key"])
        elif not current.get("submission_started_at") or (
            isinstance(exc, HTTPException) and exc.status_code in {400, 401, 403, 413, 422}
        ):
            await self._release_staged_compile_slot(job, key=current.get("key", ""))
        return None

    async def _prepare_staged_round(self, job, compiler_body, frozen_entries, language_policy, revision_instruction):
        root = self._staged_root(job)
        if (root / "state.json").is_file():
            return await self._resume_staged_round(job)
        fine_fragments = (
            job.get("staged_source_fragment_protocol") == SOURCE_FRAGMENT_PROTOCOL
            and job.get("staged_source_scope_protocol") == SOURCE_SCOPE_PROTOCOL_V3
            and job.get("staged_workflow_protocol") == "te-direct-workflow-planning.v1"
            and job.get("staged_efficiency_protocol") == EFFICIENCY_PROTOCOL
        )
        fragment_chars = FINE_SOURCE_FRAGMENT_CHARS if fine_fragments else 12000
        extract_max_chunks = FINE_EXTRACT_MAX_CHUNKS if fine_fragments else 8
        count = await asyncio.to_thread(_freeze_chunks, root, frozen_entries, job["source_snapshot"],
                                        max_chars=fragment_chars)
        groups = await asyncio.to_thread(_group_count, root, range(count), max_chunks=extract_max_chunks)
        await asyncio.to_thread(_write, root / "compiler-base.md", compiler_body)
        await asyncio.to_thread(_write, root / "revision.txt", revision_instruction)
        compilers = {}
        efficient = job.get("staged_efficiency_protocol") == EFFICIENCY_PROTOCOL
        from .direct_workflow_planning import (
            DIRECT_WORKFLOW_PROTOCOL,
            LOCAL_SELECTION_RECOVERY_PROTOCOL,
            SELECTION_RECOVERY_PROTOCOL,
        )

        direct = efficient and job.get("staged_workflow_protocol") == DIRECT_WORKFLOW_PROTOCOL
        local_scope = direct and job.get("staged_source_scope_protocol") == SOURCE_SCOPE_PROTOCOL_V3
        recovery_protocol = job.get("staged_selection_recovery_protocol")
        selection_recovery = local_scope and recovery_protocol in (
            SELECTION_RECOVERY_PROTOCOL, LOCAL_SELECTION_RECOVERY_PROTOCOL,
        )
        local_recovery = selection_recovery and recovery_protocol == LOCAL_SELECTION_RECOVERY_PROTOCOL
        input_clarity = local_scope and (
            job.get("staged_source_input_clarity_protocol") == SOURCE_INPUT_CLARITY_PROTOCOL
        )
        planning_aliases = local_scope and job.get("staged_planning_alias_protocol") == "te-planning-aliases.v1"
        from .candidate_identity import SUPPORTED_IDENTITY_PARTITION_PROTOCOLS
        from .evidence_quote_aliases import PROTOCOL as QUOTE_ALIAS_PROTOCOL
        from .source_scope import UNCERTAINTY_SCOPE_PROTOCOL
        from .workflow_planning import COMPACT_PLANNING_PROTOCOL

        compact_planning = planning_aliases and (
            job.get("staged_compact_planning_protocol") == COMPACT_PLANNING_PROTOCOL
        )
        quote_aliases = compact_planning and job.get("staged_quote_alias_protocol") == QUOTE_ALIAS_PROTOCOL
        identity_partitions = compact_planning and (
            job.get("staged_identity_partition_protocol") in SUPPORTED_IDENTITY_PARTITION_PROTOCOLS
        )
        uncertainty_scope = local_scope and job.get("staged_uncertainty_scope_protocol") == UNCERTAINTY_SCOPE_PROTOCOL
        case_groups = local_scope and job.get("staged_case_group_protocol") == "te-case-groups.v1"
        for stage in ("extract", "merge_index", "plan", "skill_section", "evaluation", "case"):
            if local_scope and stage == "extract":
                from .compile_strategy import render_extract_batch_compiler_v3

                if input_clarity:
                    from .compile_strategy import render_input_clarity_extract_batch_compiler_v3

                    body = render_input_clarity_extract_batch_compiler_v3(compiler_body)
                else:
                    body = render_extract_batch_compiler_v3(compiler_body)
            elif direct and stage == "plan":
                from .compile_strategy import render_direct_workflow_compiler, render_recovery_workflow_compiler

                if compact_planning:
                    from .compile_strategy import render_compact_aliased_workflow_compiler

                    body = render_compact_aliased_workflow_compiler(compiler_body)
                elif planning_aliases:
                    from .compile_strategy import render_aliased_workflow_compiler

                    body = render_aliased_workflow_compiler(compiler_body)
                elif local_recovery:
                    from .compile_strategy import render_local_recovery_workflow_compiler

                    body = render_local_recovery_workflow_compiler(compiler_body)
                else:
                    body = (render_recovery_workflow_compiler(compiler_body) if selection_recovery
                            else render_direct_workflow_compiler(compiler_body))
            elif efficient:
                from .compile_strategy import render_efficient_stage_compiler

                body = render_efficient_stage_compiler(compiler_body, stage)
            else:
                body = stage_compiler_body(compiler_body, stage)
            await asyncio.to_thread(_write, root / "compilers" / f"{stage}.md", body)
            compilers[stage] = _sha(body)
        state = {
            "protocol": STAGED_PROTOCOL,
            "planning_version": 2,
            "identity_protocol": IDENTITY_PROTOCOL,
            "round": int(job["current_round"]),
            "phase": "extract",
            "base_sha256": _sha(compiler_body),
            "compilers": compilers,
            "language_policy": language_policy,
            "chunk_count": count,
            "cursor": 0,
            "layer": 0,
            "unit_count": 0,
            "completed_tasks": 0,
            "attempted_tasks": 0,
            "budget_base_seconds": int(job.get("runtime_timeout_seconds") or 3600),
            "estimated_tasks": groups * 2 + 1,
            "current": None,
            "complete": False,
        }
        if job.get("staged_execution_protocol") == compile_admission.OPTIMIZED_PROTOCOL:
            state["execution_protocol"] = compile_admission.OPTIMIZED_PROTOCOL
        if efficient:
            from .ov_extract_batch_orchestration import EXTRACT_BATCH_PROTOCOL
            from .workflow_planning import WORKFLOW_PLANNING_PROTOCOL

            state.update(
                efficiency_protocol=EFFICIENCY_PROTOCOL, extract_batch_protocol=EXTRACT_BATCH_PROTOCOL,
                workflow_planning_protocol=WORKFLOW_PLANNING_PROTOCOL, merge_batch_protocol=MERGE_BATCH_PROTOCOL,
            )
        if direct:
            state["direct_workflow_protocol"] = DIRECT_WORKFLOW_PROTOCOL
            state.pop("merge_batch_protocol", None)
        if local_scope:
            state["source_scope_protocol"] = SOURCE_SCOPE_PROTOCOL_V3
        if selection_recovery:
            state["selection_recovery_protocol"] = recovery_protocol
        if input_clarity:
            state["source_input_clarity_protocol"] = SOURCE_INPUT_CLARITY_PROTOCOL
        if planning_aliases:
            state["planning_alias_protocol"] = "te-planning-aliases.v1"
        if compact_planning:
            state["compact_planning_protocol"] = COMPACT_PLANNING_PROTOCOL
        if quote_aliases:
            state["quote_alias_protocol"] = QUOTE_ALIAS_PROTOCOL
        if identity_partitions:
            state["identity_partition_protocol"] = job["staged_identity_partition_protocol"]
        if uncertainty_scope:
            state["uncertainty_scope_protocol"] = UNCERTAINTY_SCOPE_PROTOCOL
        if case_groups:
            state["case_group_protocol"] = "te-case-groups.v1"
        if local_scope and job.get("staged_workflow_partition_protocol") == "te-workflow-partition.v2":
            state["workflow_partition_protocol"] = job["staged_workflow_partition_protocol"]
            for workflow_stage in ("workflow_partition", "workflow_metadata"):
                await self._workflow_compiler(root, state, workflow_stage)
            state["workflow_compiler_sha256"] = {
                stage: state["aux_compilers"][stage] for stage in ("workflow_partition", "workflow_metadata")
            }
        body_protocols = {"te-incremental-skill-body.v1", "te-skill-body-patch.v1"}
        if (state.get("execution_protocol") == compile_admission.OPTIMIZED_PROTOCOL
                and job.get("staged_skill_body_protocol") in body_protocols):
            state["skill_body_protocol"] = job["staged_skill_body_protocol"]
        if local_scope and job.get("staged_source_unit_protocol") == "te-exact-source-units.v1":
            from .source_units import freeze_source_units

            state["source_unit_protocol"] = job["staged_source_unit_protocol"]
            units = await asyncio.to_thread(freeze_source_units, root, state)
            state.update(source_unit_count=units["unit_count"], source_unit_cursor=0)
        if fine_fragments:
            state.update(source_fragment_protocol=SOURCE_FRAGMENT_PROTOCOL, source_fragment_chars=fragment_chars,
                         extract_max_chunks=extract_max_chunks)
        if state["round"] > 1:
            state["candidate_refinement_protocol"] = REFINEMENT_PROTOCOL
            if direct:
                from .compile_strategy import render_direct_workflow_compiler, render_recovery_workflow_compiler

                if compact_planning:
                    from .compile_strategy import render_compact_aliased_workflow_compiler

                    body = render_compact_aliased_workflow_compiler(compiler_body, refinement=True)
                elif planning_aliases:
                    from .compile_strategy import render_aliased_workflow_compiler

                    body = render_aliased_workflow_compiler(compiler_body, refinement=True)
                elif local_recovery:
                    from .compile_strategy import render_local_recovery_workflow_compiler

                    body = render_local_recovery_workflow_compiler(compiler_body, refinement=True)
                else:
                    body = (render_recovery_workflow_compiler(compiler_body, refinement=True) if selection_recovery
                            else render_direct_workflow_compiler(compiler_body, refinement=True))
            else:
                body = render_refinement_plan_compiler(compiler_body)
            if efficient and not direct:
                from .workflow_planning import WORKFLOW_PLANNING_RULE

                body += "\n\n" + WORKFLOW_PLANNING_RULE
            await self._ensure_aux_compiler(root, state, "plan", body)
        if state.get("execution_protocol") == compile_admission.OPTIMIZED_PROTOCOL:
            from .round_reuse import prepare_round_reuse

            await asyncio.to_thread(prepare_round_reuse, job, root, state, frozen_entries)
        job = await self._staged_save(job, root, state)
        return await self._staged_next(job, root, state)

    async def _resume_staged_round(self, job: dict) -> dict:
        root, state = await self._staged_load(job)
        # The state journal may have committed before the public job metadata.
        job = await self._staged_save(job, root, state)
        if state.get("complete"):
            if job.get("materialized") and job.get("status") not in {"waiting_human", "succeeded", "stopped", "failed"}:
                return await self._complete_ov_skill_round(job)
            return job
        current = state.get("current")
        if not current:
            return job if self._staged_stopped(job) else await self._staged_next(job, root, state)
        if current["stage"] == EXTRACT_BATCH_STAGE:
            return await self._resume_extract_batch(job, root, state)
        if current["stage"] == WORKFLOW_BATCH_STAGE:
            return await self._resume_workflow_batch(job, root, state)
        if current["stage"] == MERGE_BATCH_STAGE:
            return await self._resume_merge_batch(job, root, state)
        if current["stage"] == CASE_BATCH_STAGE:
            return await self._resume_case_batch(job, root, state)
        if current["stage"] == ARTIFACT_BATCH_STAGE:
            return await self._resume_artifact_batch(job, root, state)
        repository = self._ov_skill_mining_repository()
        started = time.perf_counter()
        try:
            request = await asyncio.to_thread(_read, root / current["request_file"])
            body = await asyncio.to_thread((root / current["compiler_file"]).read_text, encoding="utf-8")
            packet = await asyncio.to_thread((root / current["packet_file"]).read_text, encoding="utf-8")
            if (
                _sha(_json(request)) != current["request_sha256"]
                or _sha(body) != current["compiler_sha256"]
                or _sha(packet) != current["packet_sha256"]
            ):
                if current.get("planning_alias_protocol") or current.get("quote_alias_protocol"):
                    from .direct_workflow_planning import FrozenPlanningIntegrityError

                    raise FrozenPlanningIntegrityError("Frozen alias request, compiler or packet changed")
                raise ValueError("冻结的阶段请求或编译 Skill 已被修改")
            if (current.get("context", {}).get("typed_planning") == "selection"
                    or current["stage"] == "evidence_review_quotes"):
                await asyncio.to_thread(self._staged_canonical_packet_for_current, root, state, current)
            # A lost acceptance is resolved before any provisioning or input effect.
            accepted = None
            if current.get("task_id"):
                accepted = {"task_id": current["task_id"]}
            else:
                try:
                    accepted = await self._knowledge_request(
                        job["account"], "GET", f"/api/v1/compile/submissions/{current['key']}", timeout=20.0
                    )
                except HTTPException as exc:
                    if exc.status_code != 404:
                        raise
            latest = await asyncio.to_thread(repository.get_job, job["job_id"])
            if accepted is None and self._staged_stopped(latest):
                await self._release_staged_compile_slot(job, key=current["key"])
                return await asyncio.to_thread(
                    repository.update_job, job["job_id"], status="stopped", finished_at=utc_now()
                )
            job = latest
            if accepted is None:
                if not await self._acquire_staged_compile_slot(job, state, current):
                    return await self._staged_save(job, root, state, status="preparing", ov_task_id="")
                client = self._round_client(job)
                name = request["skill"].rsplit("/", 1)[-1]
                await self._round_provision_compiler(client, name, body)
                stage_root = current["input_uri"].rsplit("/", 2)[0]
                for directory in (stage_root, current["input_uri"].rsplit("/", 1)[0], current["input_uri"]):
                    await client.mkdir(directory)
                await self._round_write_immutable(job["account"], current["input_uri"] + "/packet.json", packet)
                job = await asyncio.to_thread(repository.get_job, job["job_id"])
                if self._staged_stopped(job):
                    await self._release_staged_compile_slot(job, key=current["key"])
                    return job
                current["submission_started_at"] = utc_now()
                await asyncio.to_thread(_save, root / "state.json", state)
                accepted = await self._knowledge_request(
                    job["account"],
                    "POST",
                    "/api/v1/compile",
                    timeout=60.0,
                    extra_headers={"Idempotency-Key": current["key"]},
                    json=request,
                )
            task_id = str(accepted.get("task_id") or "") if isinstance(accepted, dict) else ""
            if not task_id:
                return await self._round_submission_retry(
                    repository, job, ValueError("阶段接受响应未返回 task_id，将通过原请求标识恢复")
                )
            current["task_id"] = task_id
            await self._mark_staged_compile_slot(job, current)
            await asyncio.to_thread(_save, root / "children" / current["child"] / "metadata.json", current)
            job = await asyncio.to_thread(repository.get_job, job["job_id"])
            job = await self._staged_save(
                job,
                root,
                state,
                compile_request=request,
                submission_idempotency_key=current["key"],
                compiler_uri=request["skill"],
                skill_uri=request["skill"],
                compiler_sha256=current["compiler_sha256"],
                compiler_source_sha256=current["compiler_source_sha256"],
            )
            return await self._record_round_acceptance(repository, job, accepted, started)
        except (httpx.HTTPError, OSError, HTTPException) as exc:
            if isinstance(exc, HTTPException) and exc.status_code in {400, 401, 403, 409, 413, 422}:
                recovered = await self._staged_release_rejected(job, root, state, exc)
                if recovered is not None:
                    return recovered
                raise
            return await self._round_submission_retry(repository, job, exc)
        except (ValueError, KeyError, TypeError) as exc:
            await self._staged_release_rejected(job, root, state, exc)
            raise

    async def _staged_submit(
        self,
        job: dict,
        root: Path,
        state: dict,
        stage: str,
        output_path: str,
        chunks: list[dict],
        controls: dict,
        context: dict,
    ) -> dict:
        packet = planning.build_stage_packet(
            stage, output_path, evidence_chunks=chunks, controls=controls, language_policy=state["language_policy"]
        )
        canonical_packet = packet
        alias_manifest = None
        alias_family = "planning"
        if (state.get("planning_alias_protocol") == "te-planning-aliases.v1"
                and context.get("typed_planning") == "selection"):
            from .planning_aliases import build_planning_alias_packet

            packet, alias_manifest = build_planning_alias_packet(canonical_packet)
        elif stage == "evidence_review_quotes" and state.get("quote_alias_protocol") == "te-evidence-quote-aliases.v1":
            from .evidence_quote_aliases import build_quote_alias_packet

            packet, alias_manifest = build_quote_alias_packet(canonical_packet)
            alias_family = "quote"
        body = await asyncio.to_thread((root / "compiler-base.md").read_text, encoding="utf-8")
        if _sha(body) != state["base_sha256"]:
            raise ValueError("本轮编译 Skill 基础快照已被修改")
        compiler_folder = "aux-compilers" if stage in state.get("aux_compilers", {}) else "compilers"
        hashes = state.get("aux_compilers", {}) if compiler_folder == "aux-compilers" else state["compilers"]
        source_body = await asyncio.to_thread((root / compiler_folder / f"{stage}.md").read_text, encoding="utf-8")
        if _sha(source_body) != hashes[stage]:
            raise ValueError("本轮冻结的阶段编译 Skill 已被修改")
        name, published = _package(source_body)
        attempt = int(context.get("attempt") or 0)
        identity_parts = [state["round"], stage, context, packet, _sha(published)]
        if alias_manifest is not None:
            identity_parts.append(_sha(_json(alias_manifest)))
        identity = _sha(_json(identity_parts))[:24]
        child = f"{stage}-{identity}"
        input_uri = f"{RUNS_ROOT}/{job['job_id']}/rounds/round-{job['current_round']}/stages/{child}/input"
        output_uri = input_uri.rsplit("/", 1)[0] + "/output"
        revision = await asyncio.to_thread((root / "revision.txt").read_text, encoding="utf-8")
        request = {
            "from": [input_uri],
            "to": output_uri,
            "skill": f"viking://agent/skills/{name}",
            "instruction": f"本次只执行 {stage} 阶段；唯一相对输出路径是 {output_path}，禁止生成其它文件。"
            f"required_paths 如使用只能包含实际路径 {output_path}；packet.output_path 是字段引用，不能作为字面文件名。"
            "原文仅 packet.evidence；derived_controls 和旧候选是非事实控制上下文。"
            + output_language_instruction(state["language_policy"]),
        }
        if context.get("repair_error"):
            request["instruction"] += "\n上次输出未被接受，请完整重写：" + context["repair_error"][:1000]
        packet_text = _json(packet)
        remaining = 48000 - len(published) - len(packet_text) - len(request["instruction"]) - 1000
        skip_revision = stage in {"identity_group", "evidence_review", "evidence_review_quotes"} or (
            stage == "extract" and bool(state.get("round_reuse_protocol"))
        )
        if not skip_revision:
            request["instruction"] += _revision_view(revision, remaining, context.get("skill_name", ""))
        remaining = 48000 - len(published) - len(packet_text) - len(request["instruction"]) - 200
        if not skip_revision:
            request["instruction"] += await asyncio.to_thread(
                _assignment_revision, root, job, controls, context, remaining
            )
        if len(published) + len(packet_text) + len(request["instruction"]) > 48000:
            raise ValueError("阶段完整 Skill/原文/修订上下文超过有界预算")
        folder = f"children/{child}"
        packet_text = _json(packet)
        await asyncio.to_thread(_write, root / folder / "packet.json", packet_text)
        await asyncio.to_thread(_write, root / folder / "compiler.md", published)
        await asyncio.to_thread(_save, root / folder / "request.json", request)
        if alias_manifest is not None:
            await asyncio.to_thread(_write, root / folder / "canonical-packet.json", _json(canonical_packet))
            await asyncio.to_thread(_save, root / folder / f"{alias_family}-aliases.json", alias_manifest)
        state["current"] = {
            "child": child,
            "stage": stage,
            "context": context,
            "attempt": attempt,
            "expected_path": output_path,
            "output_uri": output_uri,
            "input_uri": input_uri,
            "key": f"te-staged-{job['job_id']}-r{state['round']}-{identity}",
            "task_id": "",
            "request_file": folder + "/request.json",
            "packet_file": folder + "/packet.json",
            "compiler_file": folder + "/compiler.md",
            "packet_sha256": _sha(packet_text),
            "request_sha256": _sha(_json(request)),
            "compiler_sha256": _sha(published),
            "compiler_source_sha256": _sha(source_body),
        }
        state["attempted_tasks"] = int(state.get("attempted_tasks", 0)) + 1
        if alias_manifest is not None:
            state["current"].update(
                canonical_packet_file=folder + "/canonical-packet.json",
                canonical_packet_sha256=_sha(_json(canonical_packet)),
                **{f"{alias_family}_alias_protocol": state[f"{alias_family}_alias_protocol"],
                   f"{alias_family}_alias_file": folder + f"/{alias_family}-aliases.json",
                   f"{alias_family}_alias_sha256": _sha(_json(alias_manifest))},
            )
        await asyncio.to_thread(_save, root / folder / "metadata.json", state["current"])
        latest = await asyncio.to_thread(self._ov_skill_mining_repository().get_job, job["job_id"])
        if self._staged_stopped(latest):
            await asyncio.to_thread(_save, root / "state.json", state)
            return latest
        job = await self._staged_save(
            job,
            root,
            state,
            status="preparing",
            error="",
            finished_at="",
            ov_task_id="",
            compile_request=request,
            submission_idempotency_key=state["current"]["key"],
        )
        return await self._resume_staged_round(job)

    async def _staged_next(self, job: dict, root: Path, state: dict) -> dict:
        if self._staged_stopped(job):
            return job
        phase = state["phase"]
        if (phase in {"skill_section", "evaluation", "case"} and state.get("identity_review_complete")
                and state.get("efficiency_protocol") == EFFICIENCY_PROTOCOL
                and not state.get("source_routing_complete") and not state.get("current")):
            from .source_scope import route_final_candidates

            audit = await asyncio.to_thread(route_final_candidates, root, state, job["source_snapshot"])
            state["source_routing_complete"] = True
            state["source_routing_audit"] = audit
            job = await self._staged_save(job, root, state)
        if phase in {"skill_section", "evaluation", "case"} and state.get("planning_version") == 2 and not state.get(
            "identity_review_complete"
        ) and not state.get("current"):
            job = await self._staged_identity_enter(job, root, state)
            phase = state["phase"]
        if phase == "evidence_review":
            return await self._staged_evidence_review_next(job, root, state)
        if phase == "identity_group":
            return await self._staged_identity_next(job, root, state)
        if phase in {"workflow_partition", "workflow_metadata"}:
            return await self._workflow_next(job, root, state)
        if phase == "extract":
            if state.get("efficiency_protocol") == EFFICIENCY_PROTOCOL:
                return await self._staged_extract_batch_next(job, root, state)
            while state["cursor"] < state["chunk_count"]:
                chunks, after = await asyncio.to_thread(
                    _raw_group, root, range(state["chunk_count"]), state["cursor"]
                )
                refs = [_alias(chunk["id"]) for chunk in chunks]
                context = {
                    "after": after,
                    "ordinals": [chunk["ordinal"] for chunk in chunks],
                    "input_ids": [chunk["id"] for chunk in chunks],
                    "source_refs": refs,
                }
                if state.get("round_reuse_protocol"):
                    from .round_reuse import try_reuse_extract

                    if await asyncio.to_thread(try_reuse_extract, job, root, state, chunks, context):
                        job = await self._staged_save(job, root, state)
                        if self._staged_stopped(await asyncio.to_thread(
                            self._ov_skill_mining_repository().get_job, job["job_id"]
                        )):
                            return job
                        continue
                return await self._staged_submit(
                    job, root, state, "extract", "index.json", chunks,
                    {"allowed_source_refs": refs, "reference_map": dict(zip(refs, context["input_ids"]))}, context,
                )
            if state.get("planning_version") == 2:
                return await self._staged_catalog_ready(job, root, state)
            state.update(phase="plan", layer=1, cursor=0, input_unit_count=state["unit_count"], unit_count=0)
            job = await self._staged_save(job, root, state)
            return await self._staged_next(job, root, state)
        if phase == "plan":
            return await self._staged_plan_next(job, root, state)
        if phase in {"merge_index", "selection"}:
            return await self._staged_catalog_next(job, root, state)
        if phase in {"skill_section", "evaluation", "case"}:
            return await self._staged_candidate_next(job, root, state)
        return await self._staged_assemble(job, root, state)

    async def _staged_catalog_ready(self, job: dict, root: Path, state: dict) -> dict:
        counts = {"method": 0, "fact": 0, "gap": 0}
        for index in range(state["unit_count"]):
            unit = await asyncio.to_thread(_read, root / "units" / "0" / f"{index:08d}.json")
            files = {}
            for alias, ordinals in unit["bindings"].items():
                metadata = await asyncio.to_thread(_chunk_metadata, root, ordinals)
                files[alias] = list(dict.fromkeys(row["source_uri"] for row in metadata))
            for item in planning.planning_items([unit], source_files=files):
                selected = sorted({ordinal for ref in item["source_refs"] for ordinal in unit["bindings"][ref]})
                record = {
                    "item": item,
                    "unit_id": unit["id"],
                    "ordinals": selected,
                    "bindings": {ref: unit["bindings"][ref] for ref in item["source_refs"]},
                    "leaf_items": [item["item_id"]],
                }
                if state.get("source_unit_protocol"):
                    record["source_unit_bindings"] = {
                        ref: unit["source_unit_bindings"][ref] for ref in item["source_refs"]
                    }
                kind = item["kind"]
                await asyncio.to_thread(_save, root / "catalog" / kind / f"{counts[kind]:08d}.json", record)
                counts[kind] += 1
        await asyncio.to_thread(_save, root / "catalog" / "manifest.json", counts)
        if (state.get("efficiency_protocol") == EFFICIENCY_PROTOCOL
                and not state.get("workflow_partition_protocol")):
            from .workflow_planning import prepare_workflow_order

            await asyncio.to_thread(prepare_workflow_order, root, counts["method"])
        state.update(
            phase="merge_index",
            catalog_counts=counts,
            cursor=0,
            proposal_count=0,
            selection_cursor=0,
            selection_count=0,
        )
        if state.get("direct_workflow_protocol"):
            from .direct_workflow_planning import DIRECT_WORKFLOW_PROTOCOL
            from .workflow_planning import workflow_method_index

            if state["direct_workflow_protocol"] != DIRECT_WORKFLOW_PROTOCOL:
                raise ValueError("Unknown frozen direct workflow protocol")
            # Local exact copies replace the redundant model merge. All existing
            # source review and disposition readers keep the same durable layout.
            for position in range(counts["method"]):
                index = (position if state.get("workflow_partition_protocol") else
                         await asyncio.to_thread(workflow_method_index, root, position, counts["method"]))
                record = await asyncio.to_thread(_read, root / "catalog/method" / f"{index:08d}.json")
                path = root / "proposals" / f"{position:08d}.json"
                if path.exists() and await asyncio.to_thread(_read, path) != record:
                    raise ValueError("Frozen direct planning method or source lineage changed")
                await asyncio.to_thread(_save, path, record)
                await asyncio.to_thread(_save, root / "direct-planning/mapping" / f"{position:08d}.json",
                                        {"catalog_index": index, "record_sha256": _sha(_json(record)),
                                         "item_id": record["item"]["item_id"]})
            await asyncio.to_thread(_save, root / "direct-planning/manifest.json",
                                    {"protocol": DIRECT_WORKFLOW_PROTOCOL, "method_count": counts["method"],
                                     "model_merge_submissions": 0, "original_catalog_unchanged": True})
            state.update(phase="selection", proposal_count=counts["method"])
        if state.get("workflow_partition_protocol") == "te-workflow-partition.v2":
            return await self._workflow_enter(job, root, state)
        job = await self._staged_save(job, root, state)
        return await self._staged_next(job, root, state)

    @staticmethod
    def _staged_model_item(record: dict) -> dict:
        item = record["item"]
        result = {key: item[key] for key in item if key != "source_files"}
        result["source_file_ids"] = ["file-" + _sha(uri)[:24] for uri in item.get("source_files", [])]
        return result

    @staticmethod
    def _staged_readonly_note(record: dict) -> dict:
        """Context is deliberately not shaped like a consumable planning item."""
        item = record["item"]
        return {
            **{key: item[key] for key in ("text", "reason") if key in item},
            "source_file_ids": ["file-" + _sha(uri)[:24] for uri in item.get("source_files", [])],
            "readonly": True,
        }

    async def _staged_catalog_next(self, job: dict, root: Path, state: dict) -> dict:
        merging = state["phase"] == "merge_index"
        if merging and state.get("merge_batch_protocol") == MERGE_BATCH_PROTOCOL:
            return await self._staged_merge_batch_next(job, root, state)
        count = state["catalog_counts"]["method"] if merging else state["proposal_count"]
        cursor = state["cursor"] if merging else state["selection_cursor"]
        from .direct_workflow_planning import LOCAL_SELECTION_RECOVERY_PROTOCOL, MAX_DIRECT_METHODS

        local_recovery = not merging and state.get("selection_recovery_protocol") == LOCAL_SELECTION_RECOVERY_PROTOCOL
        recovery_end = state.get("direct_recovery_end") if local_recovery else None
        if local_recovery and ((state.get("direct_recovery_limit") is not None) != (recovery_end is not None)):
            raise ValueError("Frozen local selection recovery requires both its limit and original window end")
        if local_recovery and state.get("direct_recovery_limit") is not None:
            if type(recovery_end) is not int or not 0 < recovery_end <= count or cursor > recovery_end:
                raise ValueError("Frozen local selection recovery must stay inside its original method window")
            if cursor == recovery_end:
                state.pop("direct_recovery_end")
                state.pop("direct_recovery_limit")
                recovery_end = None
                job = await self._staged_save(job, root, state)
                if self._staged_stopped(job):
                    return job
        if cursor >= count:
            if merging:
                state.update(phase="selection", selection_cursor=0)
                job = await self._staged_save(job, root, state)
                return await self._staged_next(job, root, state)
            return await self._staged_selection_complete(job, root, state)
        records = []
        folder = root / "catalog" / "method" if merging else root / "proposals"
        stage = "merge_index" if merging else "plan"
        record_indexes = []
        direct = not merging and bool(state.get("direct_workflow_protocol"))
        limit = MAX_DIRECT_METHODS if direct else 8
        if direct and state.get("selection_recovery_protocol"):
            recovery_limit = state.get("direct_recovery_limit", MAX_DIRECT_METHODS)
            if type(recovery_limit) is not int or not 1 <= recovery_limit <= MAX_DIRECT_METHODS:
                raise ValueError("Frozen selection recovery limit is outside its bounded method assignment")
            limit = min(limit, recovery_limit)
        end = min(cursor + limit, count, state.get("direct_window_end", count)) if direct else min(cursor + 8, count)
        if local_recovery and recovery_end is not None:
            end = min(end, recovery_end)
        for position in range(cursor, end):
            index = position
            if merging and state.get("efficiency_protocol") == EFFICIENCY_PROTOCOL:
                from .workflow_planning import workflow_method_index

                index = await asyncio.to_thread(workflow_method_index, root, position, count)
            record = (await asyncio.to_thread(_direct_record, root, index) if direct
                      else await asyncio.to_thread(_read, folder / f"{index:08d}.json"))
            tentative = records + [record]
            controls = {
                "items": [self._staged_model_item(row) for row in tentative],
                "input_ids": list(dict.fromkeys(row["unit_id"] for row in tentative)),
                "allowed_source_refs": list(
                    dict.fromkeys(ref for row in tentative for ref in row["item"]["source_refs"])
                ),
                "planning_version": 2,
                "complete_source_available": True,
                "readonly_context_rule": "Only items are disposition inputs. Related facts/gaps are readonly; "
                "never reference their IDs in derived_from/resolved. For this method-only window resolved must be [].",
            }
            try:
                planning.build_stage_packet(
                    stage,
                    "merge.json" if merging else "selection.json",
                    controls=controls,
                    language_policy=state["language_policy"],
                    # Reserve space for exact candidate authorities and bounded
                    # readonly notes before fixing the immutable assignment.
                    max_chars=22000 if direct else 32000,
                )
            except ValueError:
                if not records:
                    raise
                break
            records = tentative
            record_indexes.append(index)
        controls = {
            "items": [self._staged_model_item(row) for row in records],
            "input_ids": list(dict.fromkeys(row["unit_id"] for row in records)),
            "allowed_source_refs": list(dict.fromkeys(ref for row in records for ref in row["item"]["source_refs"])),
            "planning_version": 2,
            "complete_source_available": True,
            "readonly_context_rule": "Only items are disposition inputs. related_facts/provisional_gaps are readonly; "
            "never reference their IDs in derived_from/resolved. For this method-only window resolved must be [].",
        }
        if not merging:
            if direct:
                from .direct_workflow_planning import related_context

                controls.update(await asyncio.to_thread(related_context, root, state, records))
                controls["direct_workflow_protocol"] = state["direct_workflow_protocol"]
                controls["max_input_items"] = MAX_DIRECT_METHODS
            else:
                await self._staged_add_related_context(root, state, records, controls)
            if state.get("identity_protocol") == IDENTITY_PROTOCOL:
                await asyncio.to_thread(
                    _selection_known,
                    job,
                    root,
                    records,
                    controls,
                    refinement_enabled=state.get("candidate_refinement_protocol") == REFINEMENT_PROTOCOL,
                )
        if direct:
            compiler_folder = "aux-compilers" if stage in state.get("aux_compilers", {}) else "compilers"
            compiler = await asyncio.to_thread((root / compiler_folder / f"{stage}.md").read_text, encoding="utf-8")
            _, published = _package(compiler)
            packet_limit = min(32000, 48000 - len(published) - 3072)
            try:
                planning.build_stage_packet(stage, "selection.json", controls=controls,
                                            language_policy=state["language_policy"], max_chars=packet_limit)
            except ValueError:
                if len(records) <= 1:
                    raise ValueError("One complete method and its compiler cannot fit the bounded planning envelope")
                split_after = cursor + len(records) // 2
                await asyncio.to_thread(
                    _save, root / "direct-planning/input-splits" / f"{_sha(_json([cursor, controls]))[:24]}.json",
                    {"before": cursor, "after": cursor + len(records), "split_after": split_after,
                     "record_indexes": record_indexes, "packet_limit": packet_limit,
                     "compiler_chars": len(published), "reason": "complete planning envelope input budget"},
                )
                state["direct_window_end"] = split_after
                state["input_budget_splits"] = int(state.get("input_budget_splits", 0)) + 1
                job = await self._staged_save(job, root, state)
                return await self._staged_catalog_next(job, root, state)
        return await self._staged_submit(
            job,
            root,
            state,
            stage,
            "merge.json" if merging else "selection.json",
            [],
            controls,
            {
                "typed_planning": "merge" if merging else "selection",
                "record_indexes": record_indexes,
                "input_ids": controls["input_ids"],
                "before": cursor,
                "after": cursor + len(records),
            },
        )

    async def _staged_add_related_context(self, root: Path, state: dict, records: list[dict], controls: dict) -> None:
        files = {uri for row in records for uri in row["item"].get("source_files", [])}
        controls.update(related_facts=[], provisional_gaps=[], context_omitted=False)
        for kind, field in (("fact", "related_facts"), ("gap", "provisional_gaps")):
            for index in range(state["catalog_counts"][kind]):
                row = await asyncio.to_thread(_read, root / "catalog" / kind / f"{index:08d}.json")
                if not files.intersection(row["item"].get("source_files", [])):
                    continue
                item = self._staged_readonly_note(row)
                controls[field].append(item)
                if len(_json(controls)) > 26000:
                    controls[field].pop()
                    controls["context_omitted"] = True
                    break
        controls["context_rule"] = (
            "Unshown catalog content is still available in original source; omission is not a whole-file gap."
        )
        path = root / "plan.json"
        if path.exists():
            plan = await asyncio.to_thread(_read, path)
            controls["already_selected"] = []
            tasks = {row["item"]["task"] for row in records}
            for row in plan["skills"]:
                if not files.intersection(row["source_refs"]) and row["task"] not in tasks:
                    continue
                brief = {key: row[key] for key in ("name", "task", "trigger", "inputs", "outputs", "boundary")}
                controls["already_selected"].append(brief)
                if len(_json(controls)) > 27000:
                    controls["already_selected"].pop()
                    controls["context_omitted"] = True
                    break

    async def _staged_plan_next(self, job: dict, root: Path, state: dict) -> dict:
        if state["cursor"] >= state["input_unit_count"]:
            if state["unit_count"] >= state["input_unit_count"]:
                raise ValueError("分层能力规划未收敛；没有丢弃任何 Evidence，请查看阶段审计")
            state.update(layer=state["layer"] + 1, cursor=0, input_unit_count=state["unit_count"], unit_count=0)
            if state["layer"] > MAX_PLAN_DEPTH:
                raise ValueError("分层能力规划超过有限深度")
        units = []
        for index in range(state["cursor"], min(state["cursor"] + 8, state["input_unit_count"])):
            row = await asyncio.to_thread(_read, root / "units" / str(state["layer"] - 1) / f"{index:08d}.json")
            projected = {"id": row["id"], "value": _project(row["value"], row["id"])}
            tentative = [*units, {**row, "projected": projected}]
            ids = [item["id"] for item in tentative]
            refs = list(dict.fromkeys(reference for item in tentative for reference in item["bindings"]))
            try:
                planning.build_stage_packet(
                    "plan",
                    "plan.json",
                    controls={
                        "inputs": [item["projected"] for item in tentative],
                        "input_ids": ids,
                        "allowed_source_refs": refs,
                        "final_merge": state["cursor"] == 0 and len(tentative) == state["input_unit_count"],
                    },
                    language_policy=state["language_policy"],
                )
            except ValueError:
                if not units:
                    raise
                break
            units = tentative
        if len(units) == 1 and state["input_unit_count"] > 1 and state["cursor"] > 0:
            row = {key: value for key, value in units[0].items() if key != "projected"}
            await asyncio.to_thread(
                _save, root / "units" / str(state["layer"]) / f"{state['unit_count']:08d}.json", row
            )
            state["unit_count"] += 1
            state["cursor"] += 1
            job = await self._staged_save(job, root, state)
            return await self._staged_plan_next(job, root, state)
        if not units or (len(units) == 1 and state["input_unit_count"] > 1):
            raise ValueError("合法的紧凑规划单元不能同时容纳两个；拒绝循环或截断材料")
        ids = [row["id"] for row in units]
        refs = list(dict.fromkeys(reference for row in units for reference in row["bindings"]))
        context = {
            "unit_indexes": list(range(state["cursor"], state["cursor"] + len(units))),
            "input_ids": ids,
            "source_refs": refs,
            "after": state["cursor"] + len(units),
            "final_merge": state["cursor"] == 0 and len(units) == state["input_unit_count"],
        }
        return await self._staged_submit(
            job,
            root,
            state,
            "plan",
            "plan.json",
            [],
            {
                "inputs": [row["projected"] for row in units],
                "input_ids": ids,
                "allowed_source_refs": refs,
                "final_merge": context["final_merge"],
            },
            context,
        )

    async def _staged_candidate_next(self, job: dict, root: Path, state: dict) -> dict:
        if state.get("execution_protocol") == compile_admission.OPTIMIZED_PROTOCOL:
            if not state.get("artifact_batches_complete"):
                return await self._staged_artifact_batch_next(job, root, state)
            while state["candidate_index"] < len(state["candidate_names"]):
                state["phase"] = "case"
                from .ov_artifact_batch_orchestration import _ledger

                name = state["candidate_names"][state["candidate_index"]]
                if (await asyncio.to_thread(_ledger, root, name)).get("reused"):
                    state["candidate_index"] += 1
                    state.update(case_index=0, case_group_attempt=0)
                    job = await self._staged_save(job, root, state)
                    continue
                break
        plan = await asyncio.to_thread(_read, root / "plan.json")
        names = state["candidate_names"]
        if state["candidate_index"] >= len(names):
            state["phase"] = "assemble"
            job = await self._staged_save(job, root, state)
            return await self._staged_assemble(job, root, state)
        name = names[state["candidate_index"]]
        candidate = next(row for row in plan["skills"] if row["name"] == name)
        scope = await asyncio.to_thread(_read, root / "scopes" / f"{name}.json")
        if state["phase"] == "case" and self._staged_cases_batched():
            return await self._staged_case_batch_next(job, root, state, candidate, scope)
        if state["phase"] == "skill_section":
            pending = state.get("writer_pending_ordinals", scope["ordinals"])
            if state["cursor"] >= len(pending):
                sections = (_read(root / "sections" / name / f"{i:08d}.json") for i in range(state["section_count"]))
                ids = await asyncio.to_thread(
                    lambda: [
                        _read(root / "chunk-metadata" / f"{ordinal:08d}.json")["id"] for ordinal in scope["ordinals"]
                    ]
                )
                try:
                    await asyncio.to_thread(
                        self._ov_skill_mining_repository().write_output_stream,
                        job["job_id"],
                        f"compiled_skill/{name}/SKILL.md",
                        planning.iter_skill_chunks(candidate, sections, expected_input_ids=ids),
                    )
                except planning.NoOperationalSkillSectionsError:
                    await self._staged_defer_candidate(
                        job,
                        root,
                        state,
                        name,
                        "完整关联原文范围未提供可执行的方法步骤"
                        if state["language_policy"].get("language") == "zh-CN"
                        else "No executable method steps were supported across the complete assigned source scope.",
                    )
                    job = await self._staged_save(job, root, state)
                    return await self._staged_candidate_next(job, root, state)
                state.pop("writer_pending_ordinals", None)
                state.update(phase="evaluation", cursor=0)
                job = await self._staged_save(job, root, state)
                return await self._staged_candidate_next(job, root, state)
            chunks, after = await asyncio.to_thread(_raw_group, root, pending, state["cursor"])
            path = "section.json"
            context = {
                "skill_name": name,
                "after": after,
                "input_ids": [chunk["id"] for chunk in chunks],
                "source_refs": list(dict.fromkeys(chunk["source_uri"] for chunk in chunks)),
                "ordinals": [chunk["ordinal"] for chunk in chunks],
            }
        else:
            groups = await asyncio.to_thread(_method_groups, root, scope)
            primary = 0 if state["phase"] == "evaluation" else state["case_index"] * len(groups) // 12
            tried = int(state.get("case_group_attempt", 0)) if state["phase"] == "case" else 0
            group_index = (primary + tried) % len(groups)
            chunks = await asyncio.to_thread(_chunks, root, groups[group_index])
            context = {
                "skill_name": name,
                "input_ids": [chunk["id"] for chunk in chunks],
                "source_refs": list(dict.fromkeys(chunk["source_uri"] for chunk in chunks)),
                "case_group_index": group_index,
                "case_group_count": len(groups),
            }
            path = "evaluation.md"
            if state["phase"] == "case":
                prior = await asyncio.to_thread(lambda: next(_prior_candidate_rows(root, name=name), None))
                prior_ids = prior["case_ids"] if prior else []
                context["case_id"] = (
                    prior_ids[state["case_index"]]
                    if len(prior_ids) == 12
                    else f"{name}-case-{state['case_index'] + 1:02d}"
                )
                path = "case.json"
        controls = {
            "candidate": {key: candidate[key] for key in ("name", "task", "boundary", "trigger")},
            "skill_name": name,
            "allowed_source_refs": context["source_refs"],
            "input_ids": context["input_ids"],
            "source_scope_rule": "This is a bounded assigned part of a larger, fully scanned original source. "
            "Unrelated sections can be skipped; absence in this part is not proof of a whole-method Evidence gap.",
        }
        if context.get("case_id"):
            controls["case_id"] = context["case_id"]
            controls["coverage_dimension"] = planning.CASE_DIMENSIONS[state["case_index"]]
            prior = [
                await asyncio.to_thread(_read, root / "cases" / name / f"{i:02d}.json")
                for i in range(state["case_index"])
            ]
            controls["previous_queries"] = [row["query"][:300] for row in prior]
            controls["previous_queries_excerpted"] = any(len(row["query"]) > 300 for row in prior)
        return await self._staged_submit(job, root, state, state["phase"], path, chunks, controls, context)

    async def _advance_staged_round(self, job: dict, task: dict) -> dict:
        status = str(task.get("status") or "")
        if status in compile_admission.TERMINAL_NATIVE_STATES:
            await self._release_staged_compile_slot(job, str(task.get("task_id") or job.get("ov_task_id") or ""))
        root, state = await self._staged_load(job)
        current = state.get("current")
        if current and str(job.get("ov_task_id") or "") != current.get("task_id"):
            return await self._resume_staged_round(job)
        if self._staged_stopped(job):
            return await asyncio.to_thread(
                self._ov_skill_mining_repository().update_job, job["job_id"], status="stopped", finished_at=utc_now()
            )
        if state.get("complete"):
            return await self._complete_ov_skill_round(job)
        if not current:
            return await self._resume_staged_round(job)
        if status not in {"completed", "succeeded", "failed", "cancelled"}:
            return job
        if status == "cancelled":
            return await self._staged_save(job, root, state, status="stopped", finished_at=utc_now())
        if current["stage"] == CASE_BATCH_STAGE:
            return await self._advance_case_batch(job, root, state, task)
        if current["stage"] == EXTRACT_BATCH_STAGE:
            return await self._advance_extract_batch(job, root, state, task)
        if current["stage"] == WORKFLOW_BATCH_STAGE:
            return await self._advance_workflow_batch(job, root, state, task)
        if current["stage"] == MERGE_BATCH_STAGE:
            return await self._advance_merge_batch(job, root, state, task)
        if current["stage"] == ARTIFACT_BATCH_STAGE:
            return await self._advance_artifact_batch(job, root, state, task)
        try:
            if status == "failed":
                error = task.get("error") or {}
                raise ValueError(str(error.get("message") if isinstance(error, dict) else error) or "阶段 Compile 失败")
            pipeline = (task.get("meta") or {}).get("pipeline") or {}
            warnings = [str(value) for value in pipeline.get("warnings") or []]
            warnings.extend(str(value) for value in (task.get("result") or {}).get("warnings") or [])
            if pipeline.get("errors") or any(
                "partial" in value.casefold() or "unfinished" in value.casefold() for value in warnings
            ):
                raise ValueError("阶段 Compile 报告部分处理或输入遗漏，不接受该产物")
            expected = current["output_uri"] + "/" + current["expected_path"]
            tree = await self._knowledge_tree(job["account"], current["output_uri"])
            files = [
                item["uri"]
                for item in tree.get("entries") or []
                if not item.get("is_dir") and not item["uri"].rsplit("/", 1)[-1].startswith(".")
            ]
            if set(files) != {expected} or len(files) != 1:
                raise ValueError("已结束的阶段未生成精确唯一的预定文件")
            result = await self._knowledge_request(
                job["account"],
                "GET",
                "/api/v1/content/read",
                params={"uri": expected, "offset": 0, "limit": -1, "raw": "true"},
                timeout=20.0,
            )
            content = result if isinstance(result, str) else result.get("content", result.get("text", ""))
            raw_limit = MAX_EXTRACT_RAW_CHARS if current["stage"] == "extract" else planning.MAX_OUTPUT_CHARS
            if not isinstance(content, str) or not content.strip() or len(content) > raw_limit:
                if isinstance(content, str) and content.strip() and (
                    current["context"].get("typed_planning") == "selection"
                ):
                    from .direct_workflow_planning import SelectionOutputValidationError

                    await asyncio.to_thread(_write, root / "children" / current["child"] / "result.txt", content)
                    raise SelectionOutputValidationError(f"Planning output exceeds the {raw_limit}-character budget")
                raise ValueError(f"阶段产物缺失或超过 {raw_limit} 字符，不接受截断结果")
            await asyncio.to_thread(_write, root / "children" / current["child"] / "result.txt", content)
            try:
                value = json.loads(content) if current["stage"] != "evaluation" else content
            except json.JSONDecodeError as exc:
                if current["context"].get("typed_planning") == "selection":
                    from .direct_workflow_planning import SelectionOutputValidationError

                    raise SelectionOutputValidationError("Planning output must be complete valid JSON") from exc
                raise
            if (state.get("direct_workflow_protocol") and current["context"].get("typed_planning") == "selection"
                    and isinstance(value, dict)
                    and value.get("schema_version") == "skill-miner.selection.capacity.v1"):
                from .direct_workflow_planning import validate_capacity

                packet, alias_manifest = await asyncio.to_thread(_selection_packet, root, state, current)
                capacity_limit = _capacity_reason_limit(state)
                value, output_limit = _selection_alias_result(value, state, alias_manifest)
                controls = packet["derived_controls"]
                validate_capacity(value, input_ids=controls["input_ids"],
                                  source_refs=controls["allowed_source_refs"], max_reason_chars=capacity_limit,
                                  _max_output_chars=output_limit)
                context = current["context"]
                before, after = state["selection_cursor"], context["after"]
                if context.get("before") != before or after - before <= 1:
                    raise ValueError("A capacity split requires at least two exact unprocessed methods")
                split_after = before + (after - before) // 2
                await asyncio.to_thread(_save, root / "direct-planning/splits" / f"{current['child']}.json",
                                        {"before": before, "after": after, "split_after": split_after,
                                         "record_indexes": context["record_indexes"], "capacity": value,
                                         "packet_sha256": current["packet_sha256"], "raw_sha256": _sha(content)})
                state["direct_window_end"] = split_after
                state["capacity_splits"] = int(state.get("capacity_splits", 0)) + 1
                state["current"] = None
                job = await self._staged_save(job, root, state, status="preparing", error="", finished_at="",
                                              ov_task_id="")
                return await self._staged_next(job, root, state)
            await self._staged_accept(job, root, state, current, value)
        except (ValueError, KeyError, TypeError) as exc:
            from .direct_workflow_planning import FrozenPlanningIntegrityError, SelectionOutputValidationError

            if isinstance(exc, FrozenPlanningIntegrityError):
                return await self._staged_save(job, root, state, status="failed", error=str(exc), finished_at=utc_now())
            return await self._staged_retry_output(
                job, root, state, str(exc), selection_output=isinstance(exc, SelectionOutputValidationError),
            )
        except (OSError, httpx.HTTPError, HTTPException) as exc:
            if isinstance(exc, HTTPException) and exc.status_code == 404:
                return await self._staged_retry_output(job, root, state, "已完成阶段的预定产物不存在")
            if isinstance(exc, HTTPException) and exc.status_code in {400, 401, 403, 409, 413, 422}:
                return await self._staged_save(
                    job, root, state, status="failed", error=f"阶段产物读取失败：{exc.detail}", finished_at=utc_now()
                )
            job = await asyncio.to_thread(
                self._ov_skill_mining_repository().update_job, job["job_id"], status="running", error="", finished_at=""
            )
            return await self._round_submission_retry(self._ov_skill_mining_repository(), job, exc)
        state["completed_tasks"] += 1
        state["current"] = None
        job = await self._staged_save(job, root, state, status="preparing", error="", finished_at="", ov_task_id="")
        return await self._staged_next(job, root, state)

    async def _staged_accept(self, job: dict, root: Path, state: dict, current: dict, value) -> None:
        context, phase = current["context"], current["stage"]
        if phase in {"evidence_review", "evidence_review_quotes"}:
            return await self._staged_evidence_review_accept(job, root, state, current, value)
        if phase == "identity_group":
            return await self._staged_identity_accept(job, root, state, current, value)
        if context.get("typed_planning"):
            return await self._staged_accept_typed(job, root, state, current, value)
        if planning.is_deferred(value) and phase in {"skill_section", "case"}:
            value = planning.validate_deferred(value, source_refs=context["source_refs"])
            if state.get("planning_version") == 2 and phase == "skill_section":
                value = {
                    "schema_version": planning.SECTION_SKIP_SCHEMA,
                    "input_ids": context["input_ids"],
                    "reason": value["reason"],
                }
                return await self._staged_accept_section(root, state, context, value)
            if phase == "case" and int(state.get("case_group_attempt", 0)) + 1 < context.get("case_group_count", 1):
                await asyncio.to_thread(
                    _save,
                    root
                    / "case-group-deferrals"
                    / context["skill_name"]
                    / f"{state['case_index']:02d}-{context['case_group_index']:08d}.json",
                    value,
                )
                state["case_group_attempt"] = int(state.get("case_group_attempt", 0)) + 1
                state["estimated_tasks"] += 1
                return
            await self._staged_defer_candidate(job, root, state, context["skill_name"], value["reason"])
            return
        if phase == "extract":
            packet = await asyncio.to_thread(_read, root / current["packet_file"])
            if packet["input_ids"] != context["input_ids"]:
                raise ValueError("Frozen extract assignment does not match the durable chunk context")
            value = _canonical_extract_references(
                value,
                input_ids=packet["input_ids"],
                source_refs=context["source_refs"],
                reference_map=packet["derived_controls"]["reference_map"],
                evidence=packet["evidence"],
            )
            value = planning.validate_index(value, input_ids=context["input_ids"], source_refs=context["source_refs"],
                                            routing=state.get("efficiency_protocol") == EFFICIENCY_PROTOCOL)
            parents = dict(zip(context["source_refs"], [[ordinal] for ordinal in context["ordinals"]]))
            unit = _bind_unit(value, parents, current["child"])
            await asyncio.to_thread(_save, root / "units" / "0" / f"{state['unit_count']:08d}.json", unit)
            state["unit_count"] += 1
            state["cursor"] = context["after"]
        elif phase == "plan":
            value = planning.validate_plan(value, source_refs=context["source_refs"], input_ids=context["input_ids"])
            units = [
                await asyncio.to_thread(_read, root / "units" / str(state["layer"] - 1) / f"{i:08d}.json")
                for i in context["unit_indexes"]
            ]
            parents = {}
            for row in units:
                for alias, selected in row["bindings"].items():
                    if alias in parents and parents[alias] != selected:
                        raise ValueError("规划范围别名的本地谱系不一致")
                    parents[alias] = selected
            unit = _bind_unit(value, parents, current["child"])
            await asyncio.to_thread(
                _save, root / "units" / str(state["layer"]) / f"{state['unit_count']:08d}.json", unit
            )
            state["unit_count"] += 1
            state["cursor"] = context["after"]
            if context["final_merge"]:
                allowed = []
                by_alias = parents
                for row in value["skills"] + value["deferred"]:
                    selected = sorted({ordinal for alias in row["source_refs"] for ordinal in by_alias[alias]})
                    raw = await asyncio.to_thread(_chunk_metadata, root, selected)
                    row["source_refs"] = list(dict.fromkeys(chunk["source_uri"] for chunk in raw))
                    allowed.extend(row["source_refs"])
                    if row in value["skills"]:
                        await asyncio.to_thread(_save, root / "scopes" / f"{row['name']}.json", {"ordinals": selected})
                value.pop("input_ids", None)
                planning.validate_plan(value, source_refs=list(dict.fromkeys(allowed)), bounded=False)
                await asyncio.to_thread(_save, root / "plan.json", value)
                groups = sum(
                    [
                        await asyncio.to_thread(
                            _group_count,
                            root,
                            (await asyncio.to_thread(_read, root / "scopes" / f"{row['name']}.json"))["ordinals"],
                        )
                        for row in value["skills"]
                    ]
                )
                state.update(
                    phase="skill_section",
                    candidate_names=[row["name"] for row in value["skills"]],
                    candidate_index=0,
                    cursor=0,
                    section_count=0,
                    case_index=0,
                    estimated_tasks=state["completed_tasks"] + 2 + len(value["skills"]) * 13 + groups,
                )
        elif phase == "skill_section":
            await self._staged_accept_section(root, state, context, value)
        elif phase == "evaluation":
            validate_artifact_headings(value, state["language_policy"])
            await asyncio.to_thread(
                self._ov_skill_mining_repository().write_output,
                job["job_id"],
                f"compiled_skill/{context['skill_name']}/EVALUATION.md",
                value,
            )
            state.update(phase="case", case_index=0, case_group_attempt=0)
        elif phase == "case":
            value = planning.validate_case(
                value, case_id=context["case_id"], skill_name=context["skill_name"], source_refs=context["source_refs"]
            )
            prior = [
                await asyncio.to_thread(_read, root / "cases" / context["skill_name"] / f"{i:02d}.json")
                for i in range(state["case_index"])
            ]
            query = planning.normalize_case_query(value["query"])
            if any(planning.normalize_case_query(row["query"]) == query for row in prior):
                raise ValueError("当前案例 query 与已有案例重复；请按原 Evidence 生成独立场景，无法支持时明确暂缓")
            await asyncio.to_thread(
                _save, root / "cases" / context["skill_name"] / f"{state['case_index']:02d}.json", value
            )
            state["case_index"] += 1
            state["case_group_attempt"] = 0
            if state["case_index"] == 12:
                state.pop("writer_pending_ordinals", None)
                state.update(
                    phase="skill_section",
                    candidate_index=state["candidate_index"] + 1,
                    cursor=0,
                    section_count=0,
                    case_index=0,
                )
        else:
            raise ValueError("未知的阶段产物")

    async def _staged_accept_section(self, root: Path, state: dict, context: dict, value) -> None:
        validator = (
            planning.validate_section_skip
            if isinstance(value, dict) and value.get("schema_version") == planning.SECTION_SKIP_SCHEMA
            else planning.validate_section
        )
        value = validator(value, input_ids=context["input_ids"])
        if value.get("schema_version") == planning.SECTION_SCHEMA:
            validate_artifact_headings(value["content"], state["language_policy"], title=value["title"])
        name = context["skill_name"]
        await asyncio.to_thread(_save, root / "sections" / name / f"{state['section_count']:08d}.json", value)
        await asyncio.to_thread(
            _save,
            root / "section-groups" / name / f"{state['section_count']:08d}.json",
            {"ordinals": context["ordinals"]},
        )
        state["section_count"] += 1
        state["cursor"] = context["after"]

    async def _staged_split_invalid_selection(self, job: dict, root: Path, state: dict, error: str) -> dict:
        """Retry a smaller existing window, accepting none of the rejected model dispositions."""
        current = state["current"]
        context = current["context"]
        before, after = state["selection_cursor"], context["after"]
        indexes = context["record_indexes"]
        if (context.get("before") != before or not isinstance(indexes, list)
                or indexes != list(range(before, after)) or not 1 < after - before <= 24):
            raise ValueError("Selection recovery must partition the exact unprocessed frozen assignment")
        packet, aliases = await asyncio.to_thread(_selection_packet, root, state, current)
        expected_sha = current["canonical_packet_sha256"] if aliases is not None else current["packet_sha256"]
        if _sha(_json(packet)) != expected_sha:
            raise ValueError("Frozen selection recovery packet SHA changed")
        records = [await asyncio.to_thread(_direct_record, root, index) for index in indexes]
        if ([row["item"]["item_id"] for row in records]
                != [row["item_id"] for row in packet["derived_controls"]["items"]]):
            raise ValueError("Selection recovery cannot change assigned methods or source lineage")
        split_after = before + (after - before) // 2
        from .direct_workflow_planning import LOCAL_SELECTION_RECOVERY_PROTOCOL

        local_recovery = state["selection_recovery_protocol"] == LOCAL_SELECTION_RECOVERY_PROTOCOL
        recovery_end = state.get("direct_recovery_end", after) if local_recovery else None
        if local_recovery and (
            type(recovery_end) is not int or not after <= recovery_end <= state["proposal_count"]
        ):
            raise ValueError("Local selection recovery cannot change its original method window")
        content = await asyncio.to_thread((root / "children" / current["child"] / "result.txt").read_text,
                                         encoding="utf-8")
        await asyncio.to_thread(
            _save, root / "direct-planning/validation-splits" / f"{current['child']}.json",
            {"protocol": state["selection_recovery_protocol"], "before": before, "after": after,
             "split_after": split_after, "record_indexes": indexes, "attempt": current["attempt"],
             "error": error[:1000], "planning_coverage": context.get("planning_coverage"),
             "packet_sha256": current["packet_sha256"], "raw_sha256": _sha(content),
             "accepted_dispositions": 0, **({"recovery_end": recovery_end} if local_recovery else {})},
        )
        state["direct_window_end"] = split_after
        state["direct_recovery_limit"] = min(state.get("direct_recovery_limit", 24), split_after - before)
        if local_recovery:
            state["direct_recovery_end"] = recovery_end
        state["validation_splits"] = int(state.get("validation_splits", 0)) + 1
        state["current"] = None
        job = await self._staged_save(job, root, state, status="preparing", error="", finished_at="", ov_task_id="")
        return await self._staged_next(job, root, state)

    async def _staged_retry_output(
        self, job: dict, root: Path, state: dict, error: str, *, selection_output: bool = False,
    ) -> dict:
        current = state["current"]
        from .direct_workflow_planning import LOCAL_SELECTION_RECOVERY_PROTOCOL, SELECTION_RECOVERY_PROTOCOL

        if (selection_output and state.get("selection_recovery_protocol") in (
            SELECTION_RECOVERY_PROTOCOL, LOCAL_SELECTION_RECOVERY_PROTOCOL,
        )
                and state.get("direct_workflow_protocol")
                and current["context"].get("typed_planning") == "selection"
                and current["attempt"] >= 1 and len(current["context"]["record_indexes"]) > 1):
            return await self._staged_split_invalid_selection(job, root, state, error)
        if current["attempt"] + 1 >= MAX_STAGE_ATTEMPTS:
            if current["stage"] == "evidence_review":
                upgraded = await self._staged_evidence_review_upgrade(job, root, state, error)
                if upgraded is not None:
                    return upgraded
            return await self._staged_save(
                job, root, state, status="failed", error=f"阶段产物校验失败：{error[:1000]}", finished_at=utc_now()
            )
        packet, _ = await asyncio.to_thread(_selection_packet, root, state, current)
        context = {**current["context"], "attempt": current["attempt"] + 1, "repair_error": error[:1000]}
        controls = packet["derived_controls"]
        if (state.get("execution_protocol") == compile_admission.OPTIMIZED_PROTOCOL
                and context.get("typed_planning") == "selection"):
            from .selection_conflicts import repair_selection_controls

            compiler = await asyncio.to_thread((root / current["compiler_file"]).read_text, encoding="utf-8")
            _, published = _package(compiler)
            controls = await asyncio.to_thread(
                repair_selection_controls, job, root, controls, context, language_policy=state["language_policy"],
                max_packet_chars=min(32000, 48000 - len(published) - 3072),
            )
        return await self._staged_submit(
            job,
            root,
            state,
            current["stage"],
            current["expected_path"],
            packet["evidence"],
            controls,
            context,
        )

    async def _staged_defer_candidate(self, job: dict, root: Path, state: dict, name: str, reason: str) -> None:
        plan = await asyncio.to_thread(_read, root / "plan.json")
        marker = root / "deferred-candidates" / f"{name}.json"
        if marker.is_file():
            deferred = await asyncio.to_thread(_read, marker)
        else:
            candidate = next((row for row in plan["skills"] if row["name"] == name), None)
            if candidate is None:
                scope = await asyncio.to_thread(_read, root / "scopes" / f"{name}.json")
                values = await asyncio.to_thread(_chunk_metadata, root, scope["ordinals"])
                refs = list(dict.fromkeys(row["source_uri"] for row in values))
            else:
                refs = candidate["source_refs"]
            deferred = {"reason": f"{name}: {reason}", "source_refs": refs}
            await asyncio.to_thread(_save, marker, deferred)
        plan["skills"] = [row for row in plan["skills"] if row["name"] != name]
        if deferred not in plan["deferred"]:
            plan["deferred"].append(deferred)
        await asyncio.to_thread(_save, root / "plan.json", plan)
        workspace = self._ov_skill_mining_repository().workspace(job)
        path = workspace / "compiled_skill" / name
        if path.exists():
            await asyncio.to_thread(shutil.rmtree, path)
        state["candidate_names"] = [value for value in state["candidate_names"] if value != name]
        state.pop("writer_pending_ordinals", None)
        state.update(phase="skill_section", cursor=0, section_count=0, case_index=0)

    async def _staged_accept_typed(self, job: dict, root: Path, state: dict, current: dict, value) -> None:
        context = current["context"]
        merging = context["typed_planning"] == "merge"
        folder = root / "catalog" / "method" if merging else root / "proposals"
        records = [
            await asyncio.to_thread(_direct_record, root, i)
            if not merging and state.get("direct_workflow_protocol")
            else await asyncio.to_thread(_read, folder / f"{i:08d}.json")
            for i in context["record_indexes"]
        ]
        items = [record["item"] for record in records]
        packet, alias_manifest = await asyncio.to_thread(_selection_packet, root, state, current)
        value, output_limit = _selection_alias_result(value, state, alias_manifest)
        controls = packet["derived_controls"]
        if controls["input_ids"] != context["input_ids"] or [row["item_id"] for row in controls["items"]] != [
            row["item_id"] for row in items
        ]:
            from .direct_workflow_planning import FrozenPlanningIntegrityError

            raise FrozenPlanningIntegrityError("Frozen planning assignment does not match the durable method context")
        from .direct_workflow_planning import SelectionOutputValidationError

        if not merging and state.get("selection_recovery_protocol"):
            diagnostics = planning.planning_coverage_diagnostics(value, [row["item_id"] for row in items])
            if diagnostics:
                context["planning_coverage"] = diagnostics
            else:
                context.pop("planning_coverage", None)
        try:
            value = _canonical_planning_header(
                value, input_ids=context["input_ids"], item_ids=[row["item_id"] for row in items]
            )
        except ValueError as exc:
            if not merging:
                raise SelectionOutputValidationError(str(exc)) from exc
            raise
        if not merging and state.get("efficiency_protocol") == EFFICIENCY_PROTOCOL:
            from .workflow_planning import canonical_selection_reuse

            value = canonical_selection_reuse(value, controls)
            if state.get("compact_planning_protocol") == "te-compact-planning.v1":
                from .workflow_planning import coalesce_exact_authority_reuse

                try:
                    value, audit = coalesce_exact_authority_reuse(value, controls, _max_output_chars=output_limit)
                except (ValueError, KeyError, TypeError) as exc:
                    raise SelectionOutputValidationError(str(exc)) from exc
                audit_path = f"children/{current['child']}/reuse-coalescing.json"
                await asyncio.to_thread(_save, root / audit_path, audit)
                current.update(reuse_coalescing_file=audit_path, reuse_coalescing_sha256=_sha(_json(audit)))
                await asyncio.to_thread(_save, root / "children" / current["child"] / "metadata.json", current)
        if merging:
            checked = planning.validate_merge(value, items=items, input_ids=context["input_ids"])
        else:
            if state.get("execution_protocol") == compile_admission.OPTIMIZED_PROTOCOL:
                from .selection_conflicts import collect_selection_conflicts

                context["selection_conflicts"] = await asyncio.to_thread(
                    collect_selection_conflicts, job, root, value, controls,
                    previous=context.get("selection_conflicts", []),
                )
            known = None
            refinement_enabled = controls.get("selection_schema") == planning.SELECTION_SCHEMA_V3
            if refinement_enabled and state.get("candidate_refinement_protocol") != REFINEMENT_PROTOCOL:
                raise ValueError("Frozen selection did not authorize business metadata refinement")
            if controls.get("selection_schema") in {planning.SELECTION_SCHEMA_V2, planning.SELECTION_SCHEMA_V3}:
                known = {row["candidate_id"]: row for row in controls["known_candidates"]}
            try:
                checked = planning.validate_selection(
                    value,
                    items=items,
                    input_ids=context["input_ids"],
                    known_candidates=known,
                    refinement_enabled=refinement_enabled,
                    max_input_items=24 if state.get("direct_workflow_protocol") else 8,
                    _max_output_chars=output_limit,
                )
            except (ValueError, KeyError, TypeError) as exc:
                raise SelectionOutputValidationError(str(exc)) from exc
            refinement_journal = (
                await asyncio.to_thread(
                    prepare_refinements, root, job, current, value, checked, records, controls["known_candidates"],
                    state=state,
                )
                if refinement_enabled
                else None
            )
        parents = {}
        by_item = {row["item"]["item_id"]: row for row in records}
        for row in records:
            for alias, selected in row["bindings"].items():
                if alias in parents and parents[alias] != selected:
                    raise ValueError("逐项规划范围的本地谱系不一致")
                parents[alias] = selected
        if merging:
            unit = _bind_unit(checked, parents, current["child"])
            files = {}
            for alias, ordinals in unit["bindings"].items():
                metadata = await asyncio.to_thread(_chunk_metadata, root, ordinals)
                files[alias] = list(dict.fromkeys(row["source_uri"] for row in metadata))
            for proposed, original in zip(planning.planning_items([unit], source_files=files), checked["methods"]):
                selected = sorted({ordinal for ref in proposed["source_refs"] for ordinal in unit["bindings"][ref]})
                record = {
                    "item": proposed,
                    "unit_id": unit["id"],
                    "ordinals": selected,
                    "bindings": {ref: unit["bindings"][ref] for ref in proposed["source_refs"]},
                    "leaf_items": list(
                        dict.fromkeys(
                            leaf for identity in original["derived_from"] for leaf in by_item[identity]["leaf_items"]
                        )
                    ),
                }
                await asyncio.to_thread(_save, root / "proposals" / f"{state['proposal_count']:08d}.json", record)
                state["proposal_count"] += 1
            state["cursor"] = context["after"]
        else:
            plan_path = root / "plan.json"
            plan = (
                await asyncio.to_thread(_read, plan_path)
                if plan_path.exists()
                else {"schema_version": planning.PLAN_SCHEMA, "skills": [], "deferred": []}
            )
            # Reject the entire selection before changing any scope file. A later
            # conflicting name must not leave partially accepted lineage behind.
            identities = {row["name"]: row for row in plan["skills"]}
            refined_names = {row["name"] for row in refinement_journal["changes"]} if refinement_journal else set()
            for row in checked["skills"]:
                previous = identities.get(row["name"])
                if previous and any(
                    previous[field] != row[field] for field in ("task", "trigger", "inputs", "outputs", "boundary")
                ) and row["name"] not in refined_names:
                    raise SelectionOutputValidationError(
                        f"候选名称 {row['name']} 已用于不同方法；请明确合并或使用独立名称"
                    )
                identities[row["name"]] = row
            if refinement_journal:
                await asyncio.to_thread(commit_refinements, root, refinement_journal)
            for row in checked["skills"]:
                old_scope = None
                selected = sorted({ordinal for ref in row["source_refs"] for ordinal in parents[ref]})
                metadata = await asyncio.to_thread(_chunk_metadata, root, selected)
                uris = list(dict.fromkeys(chunk["source_uri"] for chunk in metadata))
                full = []
                for uri in uris:
                    full.extend(
                        (await asyncio.to_thread(_read, root / "source-files" / (_sha(uri) + ".json")))["ordinals"]
                    )
                canonical = {key: item for key, item in row.items() if key not in {"derived_from", "source_refs"}}
                canonical["source_refs"] = uris
                previous = next((item for item in plan["skills"] if item["name"] == row["name"]), None)
                if previous:
                    for field in ("task", "trigger", "inputs", "outputs", "boundary"):
                        if previous[field] != canonical[field] and row["name"] not in refined_names:
                            raise SelectionOutputValidationError(
                                f"候选名称 {row['name']} 已用于不同方法；请基于已选方法明确合并或使用独立名称"
                            )
                    if row["name"] in refined_names:
                        previous.update({field: canonical[field] for field in BUSINESS_FIELDS})
                    previous["source_refs"] = list(dict.fromkeys(previous["source_refs"] + uris))
                    old_scope = await asyncio.to_thread(_read, root / "scopes" / f"{row['name']}.json")
                    selected = sorted(set(selected) | set(old_scope["method_ordinals"]))
                    full = sorted(set(full) | set(old_scope["ordinals"]))
                else:
                    plan["skills"].append(canonical)
                scope = {"ordinals": sorted(set(full)), "method_ordinals": selected}
                if old_scope and state.get("efficiency_protocol") == EFFICIENCY_PROTOCOL:
                    from .candidate_refinement import merge_candidate_scopes

                    scope = merge_candidate_scopes(old_scope, scope)
                await asyncio.to_thread(
                    _save,
                    root / "scopes" / f"{row['name']}.json",
                    scope,
                )
            for row in checked["deferred"]:
                selected = sorted({ordinal for ref in row["source_refs"] for ordinal in parents[ref]})
                metadata = await asyncio.to_thread(_chunk_metadata, root, selected)
                gap = {
                    "reason": row["reason"],
                    "source_refs": list(dict.fromkeys(chunk["source_uri"] for chunk in metadata)),
                }
                if gap not in plan["deferred"]:
                    plan["deferred"].append(gap)
            await asyncio.to_thread(_save, plan_path, plan)
            for field, target in (("skills", "candidate"), ("knowledge", "knowledge"), ("deferred", "deferred")):
                for row in checked[field]:
                    for identity in row["derived_from"]:
                        source = by_item[identity]
                        decision = {
                            "target": target,
                            "name": row.get("name", ""),
                            "reason": row.get("reason", row.get("rationale", "")),
                        }
                        for leaf in source["leaf_items"]:
                            await asyncio.to_thread(_save, root / "method-decisions" / f"{leaf}.json", decision)
            state["selection_cursor"] = context["after"]
            state["selection_count"] += 1
            state.pop("direct_window_end", None)
        await asyncio.to_thread(
            _save,
            root / "dispositions" / current["child"],
            {"kind": context["typed_planning"], "input_items": items, "result": checked},
        )

    async def _staged_selection_complete(self, job: dict, root: Path, state: dict) -> dict:
        path = root / "plan.json"
        if not path.exists():
            await asyncio.to_thread(_save, path, {"schema_version": planning.PLAN_SCHEMA, "skills": [], "deferred": []})
        plan = await asyncio.to_thread(_read, path)
        refs = list(dict.fromkeys(ref for row in plan["skills"] + plan["deferred"] for ref in row["source_refs"]))
        planning.validate_plan(
            plan, source_refs=refs, bounded=False, require_nonempty_deferred=False, require_reference_coverage=False
        )
        groups = 0
        for row in plan["skills"]:
            scope = await asyncio.to_thread(_read, root / "scopes" / f"{row['name']}.json")
            groups += await asyncio.to_thread(_group_count, root, scope["ordinals"])
        state.update(
            phase="skill_section",
            candidate_names=[row["name"] for row in plan["skills"]],
            candidate_index=0,
            cursor=0,
            section_count=0,
            case_index=0,
            estimated_tasks=state["completed_tasks"] + 1 + len(plan["skills"]) * 13 + groups,
        )
        job = await self._staged_save(job, root, state)
        return await self._staged_next(job, root, state)

    async def _staged_assemble(self, job: dict, root: Path, state: dict) -> dict:
        repository = self._ov_skill_mining_repository()
        plan = await asyncio.to_thread(_read, root / "plan.json")
        for candidate in plan["skills"]:
            name = candidate["name"]
            cases = [await asyncio.to_thread(_read, root / "cases" / name / f"{index:02d}.json") for index in range(12)]
            prior = await asyncio.to_thread(lambda: next(_prior_candidate_rows(root, name=name), None))
            prior_ids = prior["case_ids"] if prior else []
            dataset = planning.assemble_dataset(
                cases,
                skill_name=name,
                task=candidate["task"],
                identity=f"{job['job_id']}:{job['current_round']}:{name}",
                expected_case_ids=prior_ids if len(prior_ids) == 12 else None,
            )
            await asyncio.to_thread(
                repository.write_output, job["job_id"], f"compiled_skill/{name}/benchmark.json", _json(dataset)
            )
            await asyncio.to_thread(
                repository.write_output, job["job_id"], f"compiled_skill/{name}/BENCHMARK.md", dataset_markdown(dataset)
            )
        chinese = state["language_policy"].get("language") == "zh-CN"
        if state.get("planning_version") == 2:
            await self._staged_disposition_views(job, root, state, chinese)
        questions = []
        for index, row in enumerate(plan["deferred"][:50]):
            refs, reason = row["source_refs"], row["reason"]
            if len(refs) > 50:
                refs = [job["source_snapshot"], *refs[:49]]
                reason += (
                    " 精确全量范围见能力拆分计划及来源分片清单；所列文件仅为代表。"
                    if chinese
                    else " The complete scope is in the capability plan and source manifest; "
                    "listed files are representative."
                )
            questions.append(
                {
                    "qid": f"staged-gap-{index + 1}",
                    "question": ("请补充以下缺口的 Evidence：" if chinese else "Please supply Evidence for this gap: ")
                    + row["reason"],
                    "reason": reason,
                    "source_refs": refs,
                    "required": True,
                }
            )
        gaps = "# Evidence 缺口\n\n" if chinese else "# Evidence gaps\n\n"
        gaps += "\n\n".join(row["reason"] for row in plan["deferred"]) or (
            "未报告暂缓项。" if chinese else "No deferred items were reported."
        )
        evidence = "# Evidence 来源映射\n\n" if chinese else "# Evidence source mapping\n\n"
        evidence += "\n\n".join(row["task"] + "\n" + "\n".join(row["source_refs"]) for row in plan["skills"])
        if not plan["skills"]:
            evidence += "\n".join(ref for row in plan["deferred"] for ref in row["source_refs"])
            if state.get("planning_version") == 2:
                evidence += (
                    "\n\n所有原文已逐片段扫描；事实信息与方法去向详见 method-dispositions.md。\n"
                    if chinese
                    else "\n\nAll original source ranges were scanned. See method-dispositions.md "
                    "for factual information and each method disposition.\n"
                ) + job["source_snapshot"]

        for path, content in {
            "analysis/capability-plan.json": _json(plan),
            "analysis/evidence-map.md": evidence,
            "analysis/gaps.md": gaps,
            "analysis/review.json": _json({"schema_version": "skill-miner.review.v1", "questions": questions}),
        }.items():
            await asyncio.to_thread(repository.write_output, job["job_id"], path, content)
        job = await asyncio.to_thread(repository.finalize_materialized, job["job_id"])
        state["complete"] = True
        state["phase"] = "assemble"
        job = await self._staged_save(job, root, state, error="", status="running")
        return await self._complete_ov_skill_round(job)

    async def _staged_disposition_views(self, job: dict, root: Path, state: dict, chinese: bool) -> None:
        """Expose bounded audit pages while the complete lineage remains in the private journal."""
        repository = self._ov_skill_mining_repository()
        title = "方法与事实处置清单" if chinese else "Method and fact dispositions"
        pages, rows, counters = [], [], {"candidate": 0, "knowledge": 0, "deferred": 0, "provisional_gap": 0}

        async def flush():
            if not rows:
                return
            index = len(pages) + 1
            path = f"analysis/dispositions/page-{index:04d}"
            text = f"# {title} {index}\n\n"
            for row in rows:
                label = "条目 " + row["item_id"] if chinese else row["item_id"]
                text += f"## {label}\n\n{row['proposal']}\n\n{row['disposition']}\n\n"
                if row["candidate_name"]:
                    text += row["candidate_name"] + "\n\n"
                text += row["reason"] + "\n\n" + "\n".join(row["source_refs"]) + "\n\n"
                if row["source_refs_are_representative"]:
                    text += (
                        "完整精确范围保存在当前轮次的逐项处置账目与来源清单；以上为代表来源。\n\n"
                        if chinese
                        else "Exact complete scope is retained in this round's disposition journal "
                        "and source manifest; the listed references are representative.\n\n"
                    )
            await asyncio.to_thread(
                repository.write_output,
                job["job_id"],
                path + ".json",
                _json({"schema_version": "skill-miner.dispositions.v1", "items": rows}),
            )
            await asyncio.to_thread(repository.write_output, job["job_id"], path + ".md", text)
            pages.append(path.removeprefix("analysis/") + ".md")
            rows.clear()

        aliases_path = root / "identity" / "aliases.json"
        aliases = await asyncio.to_thread(_read, aliases_path) if aliases_path.is_file() else {}
        for kind in ("method", "fact", "gap"):
            for index in range(state["catalog_counts"][kind]):
                record = await asyncio.to_thread(_read, root / "catalog" / kind / f"{index:08d}.json")
                item = record["item"]
                if kind == "method":
                    decision = await asyncio.to_thread(_read, root / "method-decisions" / f"{item['item_id']}.json")
                    original_path = root / "identity" / "original-decisions" / f"{item['item_id']}.json"
                    original = await asyncio.to_thread(_read, original_path) if original_path.is_file() else decision
                    original_name, original_target = original["name"], original["target"]
                    alias_name = decision["name"]
                    if alias_name in aliases:
                        decision = {**decision, "name": aliases[alias_name]["name"],
                                    "reason": decision["reason"] + " " + aliases[alias_name]["reason"]}
                    marker = root / "deferred-candidates" / f"{decision['name']}.json"
                    if decision["name"] and marker.is_file():
                        deferred = await asyncio.to_thread(_read, marker)
                        decision = {"target": "deferred", "name": decision["name"], "reason": deferred["reason"]}
                else:
                    decision = {
                        "target": "knowledge" if kind == "fact" else "provisional_gap",
                        "name": "",
                        "reason": (
                            "原文事实保留，不是独立可执行方法"
                            if chinese
                            else "Original factual information retained; it is not an executable method."
                        )
                        if kind == "fact"
                        else item["reason"],
                    }
                if kind != "method":
                    original_name, original_target = "", decision["target"]
                counters[decision["target"]] += 1
                refs = item.get("source_files") or []
                labels = (
                    {
                        "candidate": "候选方法（Skill Candidate）",
                        "knowledge": "事实信息（不要求补证）",
                        "deferred": "真实暂缓",
                        "provisional_gap": "片段级限制（不自动要求补证）",
                    }
                    if chinese
                    else {
                        "candidate": "Skill Candidate",
                        "knowledge": "Factual information",
                        "deferred": "Deferred",
                        "provisional_gap": "Provisional fragment limitation",
                    }
                )
                rows.append(
                    {
                        "item_id": item["item_id"],
                        "kind": kind,
                        "proposal": item.get("task", item.get("text", item.get("reason", ""))),
                        "disposition": labels[decision["target"]],
                        "target": decision["target"],
                        "candidate_name": decision["name"],
                        "original_candidate_name": original_name,
                        "original_target": original_target,
                        "reason": decision["reason"],
                        "source_refs": refs if len(refs) <= 8 else [job["source_snapshot"], *refs[:7]],
                        "source_refs_are_representative": len(refs) > 8,
                    }
                )
                if len(rows) == 64:
                    await flush()
        await flush()
        summary = f"# {title}\n\n" + _json(counters) + "\n\n"
        summary += (
            "逐原始提议记录去向；结构校验通过不代表业务事实正确或 True Replay 通过。\n\n"
            if chinese
            else "Each original proposal retains a disposition. Structural validation does not "
            "establish business correctness or a passed True Replay.\n\n"
        )
        summary += "\n".join(
            f"- [{'第 ' + str(index) + ' 页' if chinese else path}]({path})"
            for index, path in enumerate(pages, 1)
        )
        await asyncio.to_thread(repository.write_output, job["job_id"], "analysis/method-dispositions.md", summary)
