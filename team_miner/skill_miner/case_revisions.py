"""Verify an auxiliary source-bound Case revision without changing its original Native record."""

from __future__ import annotations

import json
import re
from pathlib import Path

from . import bounded_compile_plan as planning
from .ov_staged_orchestration import _json, _read, _save, _sha

PROTOCOL = "te-case-source-revision.v1"


def _folder(root: Path, name: str, index: int) -> Path:
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,99}", name) or type(index) is not int or not 0 <= index < 24:
        raise ValueError("Case revision needs exact safe identities")
    folder = root / "case-revisions" / name / f"{index:02d}"
    if not folder.resolve().is_relative_to(root.resolve()):
        raise ValueError("Case revision cannot escape its own archive")
    return folder


def validate_source_revision(root: Path, name: str, index: int, original: dict,
                             original_context: dict) -> tuple[dict, dict]:
    folder = _folder(root, name, index)
    files = [folder / name for name in ("completed-manifest.json", "packet.json", "compiler.md", "request.json",
                                       "accepted.json", "task.json", "raw-output.json")]
    if any(path.is_symlink() for path in files):
        raise ValueError("Case revision archive must contain ordinary frozen files")
    manifest, packet = _read(files[0]), _read(files[1])
    request, accepted, task = (_read(path) for path in files[3:6])
    raw = files[-1].read_text(encoding="utf-8")
    if (manifest.get("protocol") != PROTOCOL or manifest.get("name") != name or manifest.get("index") != index
            or manifest.get("original_case_sha256") != _sha(_json(original))
            or manifest.get("packet_sha256") != _sha(files[1].read_text(encoding="utf-8"))
            or manifest.get("compiler_sha256") != _sha(files[2].read_text(encoding="utf-8"))
            or manifest.get("request_sha256") != _sha(_json(request))
            or manifest.get("native_result_sha256") != _sha(raw)
            or len(raw) > planning.MAX_OUTPUT_CHARS
            or task.get("status") != "completed"
            or accepted.get("task_id") != manifest.get("native_task_id")
            or task.get("task_id") != manifest.get("native_task_id")):
        raise ValueError("Case revision requires the exact original Case and completed frozen Native request/result")
    controls = packet["derived_controls"]
    state = _read(root / "state.json")
    policy = state["language_policy"]
    from .case_context import CASE_CONTEXT_BATCH_STAGE
    from .ov_staged_orchestration import _package

    compiler = (root / "aux-compilers" / (CASE_CONTEXT_BATCH_STAGE + ".md")).read_text(encoding="utf-8")
    compiler_name, published = _package(compiler)
    if (state["aux_compilers"].get(CASE_CONTEXT_BATCH_STAGE) != _sha(compiler)
            or published != files[2].read_text(encoding="utf-8")
            or request.get("skill") != "viking://agent/skills/" + compiler_name):
        raise ValueError("Case revision must use the verified frozen source-bound compiler")
    expected_output = request["to"] + f"/case-output-{index:02d}.json"
    emitted = {uri for kind in ("created", "updated", "unchanged") for uri in task.get("result", {}).get(kind, [])}
    if (task.get("meta", {}).get("request") != request or emitted != {expected_output}
            or task.get("result", {}).get("conflicts")
            or len(_json(packet)) > 32000
            or len(_json(packet)) + len(files[2].read_text()) + len(request.get("instruction", "")) > 48000):
        raise ValueError("Case revision needs the exact bounded Native input and single assigned output")
    scope = _read(root / "scopes" / f"{name}.json")
    chunks = packet["evidence"]
    planning.validate_chunks(chunks)
    ordinals = []
    for chunk in chunks:
        ordinal = _read(root / "chunk-ids" / (chunk["id"] + ".json"))["ordinal"]
        if _read(root / "chunks" / f"{ordinal:08d}.json") != chunk:
            raise ValueError("Case revision changed its original source range")
        ordinals.append(ordinal)
    ids = [chunk["id"] for chunk in chunks]
    refs = list(dict.fromkeys(chunk["source_uri"] for chunk in chunks))
    primary = [chunk["id"] for chunk, ordinal in zip(chunks, ordinals, strict=True)
               if ordinal in scope["method_ordinals"]]
    contextual = [identity for identity in ids if identity not in primary]
    candidate = next(row for row in _read(root / "plan.json")["skills"] if row["name"] == name)
    if (not primary or not set(ordinals) <= set(scope["ordinals"])
            or packet.get("stage") != "case" or packet.get("output_path") != f"case-output-{index:02d}.json"
            or packet.get("input_ids") != ids or controls.get("input_ids") != ids or manifest.get("input_ids") != ids
            or controls.get("case_id") != original_context["case_id"] or controls.get("skill_name") != name
            or controls.get("candidate") != {key: candidate[key] for key in ("name", "task", "boundary", "trigger")}
            or manifest.get("source_fingerprints") != [[chunk["id"], chunk["sha256"]] for chunk in chunks]
            or controls.get("method_input_ids") != primary or manifest.get("method_input_ids") != primary
            or controls.get("context_input_ids") != contextual or manifest.get("context_input_ids") != contextual
            or controls.get("allowed_source_refs") != refs or manifest.get("source_refs") != refs
            or packet.get("derived_controls_are_evidence") is not False
            or controls.get("case_revision_protocol") != PROTOCOL
            or packet.get("output_language_policy") != policy):
        raise ValueError("Case revision changed its candidate boundary, source scope or method/context roles")
    prefix = f"viking://resources/agent_skill_workspace/runs/{root.parents[2].name}/rounds/{root.parent.name}/stages/"
    if (not manifest["input_uri"].startswith(prefix + "review-case-")
            or request.get("from") != [manifest["input_uri"]] or request.get("to") != manifest["output_uri"]
            or manifest["output_uri"] != manifest["input_uri"].removesuffix("/input") + "/output"
            or not manifest["input_uri"].endswith("/input")):
        raise ValueError("Case revision Native request must belong to the same job and round")
    value = json.loads(raw)
    planning.validate_case(value, case_id=original["case_id"], skill_name=name, source_refs=refs)
    if policy.get("language") == "zh-CN":
        from .output_language import _han, _natural_text

        display = [value["query"], *(row["text"] for row in value["checks"])]
        display += [value["metadata"][key] for key in ("name", "description") if value["metadata"].get(key)]
        if any(not any(_han(c) for c in _natural_text(text)) for text in display):
            raise ValueError("Source revision display text must follow the frozen Chinese policy")
    if _sha(_json(value)) != manifest["revision_case_sha256"]:
        raise ValueError("Case revision changed its canonical projected Case")
    return value, manifest


def register_source_revision(root: Path, name: str, index: int, *, explanation: str) -> dict:
    from .case_corrections import _source_assignment

    path = root / "case-corrections" / name / f"{index:02d}.json"
    if path.exists() or not explanation.strip():
        raise ValueError("Source revision is once-only and requires an inspected explanation")
    original = _read(root / "cases" / name / f"{index:02d}.json")
    context, original_chunks = _source_assignment(root, name, index, original)
    _, manifest = validate_source_revision(root, name, index, original, context)
    record = {"protocol": PROTOCOL, "original_case_sha256": manifest["original_case_sha256"],
              "revision_case_sha256": manifest["revision_case_sha256"], "accepted_context": context,
              "original_source_fingerprints": [[chunk["id"], chunk["sha256"]] for chunk in original_chunks],
              "manifest_sha256": _sha(_json(manifest)), "explanation": explanation,
              "unknown_facts_resolved": False, "business_quality_passed": False}
    _save(path, record)
    return record


def revised_case(root: Path, name: str, index: int, original: dict, context: dict,
                 chunks: list[dict], record: dict) -> dict:
    if record["original_source_fingerprints"] != [[chunk["id"], chunk["sha256"]] for chunk in chunks]:
        raise ValueError("Source revision original assignment changed")
    value, manifest = validate_source_revision(root, name, index, original, context)
    if record["manifest_sha256"] != _sha(_json(manifest)) or record["revision_case_sha256"] != manifest[
        "revision_case_sha256"
    ] or record["original_case_sha256"] != manifest["original_case_sha256"]:
        raise ValueError("Source revision audit record changed")
    return value
