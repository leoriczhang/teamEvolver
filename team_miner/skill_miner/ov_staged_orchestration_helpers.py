# ruff: noqa: F401
# Imports are part of the explicit compatibility surface consumed by split mixins.
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
from starlette.exceptions import HTTPException

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
    from .ov_round_orchestration_helpers import _previous_candidate_catalog

    for locator in _previous_candidate_catalog(folder) or []:
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
            if context.get("case_id"):
                row["skill_body_excerpt_omitted"] = True
            else:
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
