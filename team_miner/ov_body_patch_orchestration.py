"""Additive body assignments over the existing durable artifact file Map transport."""

from __future__ import annotations

import asyncio
from pathlib import Path

from . import bounded_compile_plan as planning
from . import skill_body_patch as body_patch
from .source_unit_scope import exact_candidate_scope
from .source_units import load_source_unit

RULE = """Generate an additive method patch, never rewrite a whole Skill or copy full dialogues.
The host renders all accepted steps and their attached conditions deterministically. Follow the exact
derived_controls.body_patch protocol, assignment_sha256, base_state_sha256, input_ids,
next_atom_id and Chinese title.
Use the candidate task/trigger/boundary as the exclusive operational goal. Only operative_ids can support
new steps. Context may explain a condition or an unresolved source claim; a neighboring scenario, shared
table heading, course journey or example cannot authorize a new operation outside this candidate.
Keep ONE reusable method. When a step already exists in atom_directory, attach only a genuinely new
condition/exception/example to that step; do not add duplicate persona/scenario versions of it. Preserve
optional nature, consent, timing additions, inputs, completion conditions and dependencies. Keep concrete
people, amounts, channels and company claims in their original scenario as replaceable parameters.
Compare explicit numerical totals and role references in the current raw Evidence: if they conflict,
record a Chinese conflict atom identifying the unresolved source issue, not an invented corrected fact.
Never turn training dialogue into authorization for real-world eligibility or purchase decisions.
Return exactly schema_version='skill-miner.body-patch.v1', assignment_sha256, base_state_sha256,
input_ids (ALL CURRENT source-unit IDs exactly once), title, add, attach, step_order, no_change_reason.
add rows have exactly id, kind (step/limit/example/conflict), text, support_ids. attach rows additionally
have parent_id, kind (condition/example/conflict), and must point to an existing or newly added step.
IDs continue from next_atom_id, counting all add rows first then attach rows. step_order is an exact
permutation of ALL existing plus new step IDs, in execution order, not source traversal order. Reordering
does not delete or replace atoms or their conditions. All text and titles must be Chinese. Each atom text
is at most 1200 characters. No new atom: use empty add/attach and explain in Chinese no_change_reason;
when atoms are added that reason is empty. A no-add reorder needs a Chinese explanation there.
Current source IDs are support, old atom_directory is generated context and never new Evidence. The
entire JSON must fit 12000 characters. If this CURRENT assignment needs smaller inputs, return exactly
schema_version='skill-miner.body-patch.capacity.v1', assignment_sha256, base_state_sha256, input_ids and
a Chinese reason (max400). Never omit input IDs, truncate raw sources or remove an old condition to fit.
"""


def _host():
    from .ov_staged_orchestration import _json, _package, _read, _save, _sha

    return _json, _package, _read, _save, _sha


def _inside(root: Path, relative: str) -> Path:
    path = root / relative
    if (not isinstance(relative, str) or Path(relative).is_absolute() or ".." in Path(relative).parts
            or path.is_symlink() or not path.resolve().is_relative_to(root.resolve())):
        raise ValueError("Frozen body patch must remain inside its exact round")
    return path


def body_state(root: Path, candidate: dict, count: int) -> dict:
    _, _, read, _, _ = _host()
    if not count:
        return body_patch.new_body_state(candidate)
    try:
        value = read(_inside(root, f"body-patches/{candidate['name']}/states/{count:08d}.json"))
    except FileNotFoundError as exc:
        raise ValueError("已接受的正文状态文件缺失，无法安全继续追加步骤。") from exc
    return body_patch.validate_body_state(value, candidate)


def verify_body_assignment(root: Path, context: dict, packet: dict) -> dict:
    json_text, _, read, _, sha = _host()
    try:
        manifest = read(_inside(root, context["body_manifest_file"]))
    except FileNotFoundError as exc:
        raise ValueError("已冻结的正文任务清单缺失，无法安全校验产物。") from exc
    if sha(json_text(manifest)) != context["body_manifest_sha256"]:
        raise ValueError("Frozen body patch assignment changed")
    controls = packet["derived_controls"]["body_patch"]
    body_patch.validate_body_patch_manifest(manifest, controls)
    if (manifest["candidate"]["name"] != context["skill_name"]
            or manifest["evidence"] != packet["evidence"]
            or controls["input_ids"] != context["input_ids"]
            or manifest["base_state"] != body_state(root, manifest["candidate"], context["section_index"])):
        raise ValueError("Body patch changed its exact candidate, Evidence or prior accepted state")
    scope = exact_candidate_scope(root, read(root / "scopes" / (context["skill_name"] + ".json")))
    if context["input_ids"] != scope["unit_ids"][context["before"]:context["after"]]:
        raise ValueError("Body patch no longer covers its exact candidate source-unit cursor")
    operative = set(scope["operative_ids"])
    if (manifest["operative_ids"] != [identity for identity in context["input_ids"] if identity in operative]
            or manifest["context_ids"] != [identity for identity in context["input_ids"] if identity not in operative]):
        raise ValueError("Body patch original method/context authority changed")
    for chunk in packet["evidence"]:
        if load_source_unit(root, chunk["id"]) != chunk:
            raise ValueError("Body patch original source unit changed")
    return manifest


def accept_body_assignment(root: Path, context: dict, value: dict, packet: dict) -> dict:
    """An immutable next-state projection is the journal; replay never appends twice."""
    _, _, read, save, _ = _host()
    manifest = verify_body_assignment(root, context, packet)
    next_state = body_patch.reduce_body_patch(manifest["base_state"], value, manifest)
    path = root / "body-patches" / context["skill_name"] / "states" / f"{context['section_index'] + 1:08d}.json"
    if path.exists() and read(path) != next_state:
        raise ValueError("Accepted additive body projection changed after its durable outcome")
    save(path, next_state)
    return next_state


async def next_body_batch(owner, job: dict, root: Path, state: dict):
    """Finish one body per candidate, batching only independent candidate assignments."""
    from .compile_strategy import render_stage_batch_compiler
    from .ov_artifact_batch_orchestration import MAX_ARTIFACT_PACKETS, _instruction, _ledger
    from .ov_case_batch_orchestration import _bounded_case_revision

    json_text, package, read, save, sha = _host()
    plan = await asyncio.to_thread(read, root / "plan.json")
    base = await asyncio.to_thread((root / "compiler-base.md").read_text, encoding="utf-8")
    if sha(base) != state["base_sha256"]:
        raise ValueError("Frozen additive body base compiler changed")
    key = "skill_section_batch"
    await owner._ensure_aux_compiler(
        root, state, key, render_stage_batch_compiler(base, "skill_section", body_patch=True)
    )
    compiler = await asyncio.to_thread((root / "aux-compilers" / (key + ".md")).read_text, encoding="utf-8")
    _, published = package(compiler)
    packets, contexts = [], []
    for candidate in plan["skills"]:
        name = candidate["name"]
        ledger = await asyncio.to_thread(_ledger, root, name)
        if ledger.get("deferred") or ledger["skill_ready"]:
            continue
        scope_record = await asyncio.to_thread(read, root / "scopes" / (name + ".json"))
        scope = await asyncio.to_thread(exact_candidate_scope, root, scope_record)
        if state.get("round_reuse_protocol") and not ledger.get("reuse_checked"):
            from .round_reuse import try_reuse_candidate

            reused = await asyncio.to_thread(
                try_reuse_candidate, job, root, state, candidate, scope_record, owner._ov_skill_mining_repository()
            )
            ledger["reuse_checked"] = True
            if reused:
                ledger.update(skill_ready=True, evaluation_ready=True, reused=True, cursor=len(scope["unit_ids"]))
            await asyncio.to_thread(save, root / "artifact-batches/candidates" / (name + ".json"), ledger)
            if reused:
                continue
        prior = await asyncio.to_thread(body_state, root, candidate, ledger["section_count"])
        cursor = ledger["cursor"]
        if cursor >= len(scope["unit_ids"]):
            covered = [identity for receipt in prior["receipts"] for identity in receipt["input_ids"]]
            if covered != scope["unit_ids"]:
                raise ValueError("Additive body has not inspected every exact source unit once")
            if not any(atom["kind"] == "step" for atom in prior["atoms"]):
                await owner._staged_defer_candidate(job, root, state, name, "完整关联原文未提供可执行的方法步骤。")
                ledger["deferred"] = True
            else:
                content = body_patch.render_body_patch(candidate, prior)
                await asyncio.to_thread(owner._ov_skill_mining_repository().write_output, job["job_id"],
                                        f"compiled_skill/{name}/SKILL.md", content)
                ledger["skill_ready"] = True
            await asyncio.to_thread(save, root / "artifact-batches/candidates" / (name + ".json"), ledger)
            continue
        limit = min(32, int(ledger.get("body_max_units") or 32), len(scope["unit_ids"]) - cursor)
        while limit:
            ids = scope["unit_ids"][cursor:cursor + limit]
            chunks = await asyncio.to_thread(lambda: [load_source_unit(root, identity) for identity in ids])
            operative = set(scope["operative_ids"])
            assignment_id = f"{name}/{ledger['section_count']:08d}/{cursor}-{cursor + limit}"
            result = body_patch.build_body_patch(
                candidate, prior, chunks, operative_ids=[identity for identity in ids if identity in operative],
                context_ids=[identity for identity in ids if identity not in operative], assignment_id=assignment_id,
                max_controls_chars=24000,
            )
            output = f"{name}-section-{ledger['section_count']:08d}.json"
            context = {"stage": "skill_section", "skill_name": name, "before": cursor, "after": cursor + limit,
                       "section_index": ledger["section_count"], "input_ids": ids,
                       "source_refs": list(dict.fromkeys(chunk["source_uri"] for chunk in chunks)),
                       "ordinals": [chunk["ordinal"] for chunk in chunks],
                       "format_attempts": int(ledger.get("format_attempts") or 0)}
            try:
                if result["status"] != "ready":
                    raise body_patch.BodyPatchCapacityError(result["reason"])
                controls = {"candidate": result["controls"]["candidate"], "skill_name": name, "input_ids": ids,
                            "allowed_source_refs": context["source_refs"], "body_patch": result["controls"]}
                if ledger.get("error"):
                    controls["repair_error"] = ledger["error"][:1000]
                packet = planning.build_stage_packet(
                    "skill_section", output, evidence_chunks=chunks, controls=controls,
                    language_policy=state["language_policy"], max_chars=29000,
                )
                packet = await asyncio.to_thread(
                    _bounded_case_revision, root, packet, context, published, _instruction(state["language_policy"])
                )
                break
            except (body_patch.BodyPatchCapacityError, ValueError) as exc:
                if not isinstance(exc, body_patch.BodyPatchCapacityError) and not str(exc).startswith((
                    "Stage packet exceeds", "Independent Case packet needs",
                )):
                    raise
                if limit == 1:
                    raise body_patch.BodyPatchCapacityError(
                        "一个完整原文单元与已有步骤目录已超输入容量；未裁剪或丢弃任何条件。"
                    ) from exc
                limit -= 1
        digest = result["manifest"]["assignment_sha256"]
        relative = f"body-patches/{name}/assignments/{digest}.json"
        path = root / relative
        if path.exists() and await asyncio.to_thread(read, path) != result["manifest"]:
            raise ValueError("Immutable additive body assignment changed")
        await asyncio.to_thread(save, path, result["manifest"])
        context.update(body_manifest_file=relative, body_manifest_sha256=sha(json_text(result["manifest"])))
        packets.append(packet)
        contexts.append(context)
        if len(packets) == MAX_ARTIFACT_PACKETS:
            break
    if packets:
        state["phase"] = "skill_section"
        return await owner._submit_artifact_batch(job, root, state, "skill_section", packets, contexts)
    state["body_patch_complete"] = True
    job = await owner._staged_save(job, root, state, status="preparing", ov_task_id="")
    return await owner._staged_artifact_batch_next(job, root, state)
