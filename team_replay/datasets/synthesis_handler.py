"""Worker-owned Dataset synthesis using frozen Session and Skill inputs."""
from __future__ import annotations

import hashlib

from teamEvolver.runtime import HandlerResult

from .repository import DatasetRepository
from .schema import make_document
from .synthesis import synthesize_evolution_datasets


async def synthesize(task, services, history, payload):
    factory = services.extras.get("replay_llm_factory")
    if factory is None:
        from team_skills.evolution.model import model_for
        llm = await model_for(services, task.tenant_id)
    else:
        llm = factory(payload["config"], task.tenant_id)
    fixed = payload["dataset"]
    from ..artifacts import decode_bundle_payload
    bundle = decode_bundle_payload(fixed["bundle"])
    generated = await synthesize_evolution_datasets(
        llm, skill_name=fixed["skill_id"],
        sessions=[case["provenance"]["session_snapshot"] for case in fixed["document"]["cases"]],
        candidate_skill={"description": fixed["skill_id"], "content": bundle["SKILL.md"].decode()},
        case_count=2,
    )
    if not generated:
        return HandlerResult.fail("dataset_synthesis_no_cases")
    sources = {
        case["provenance"]["session_id"]: case["provenance"] for case in fixed["document"]["cases"]
    }
    for case in generated:
        source_id = next(iter(case["provenance"].get("session_ids") or []), "")
        if source_id not in sources:
            return HandlerResult.fail("dataset_synthesis_source_unavailable")
        case["provenance"].update({
            key: value for key, value in sources[source_id].items()
            if key in {"session_id", "session_snapshot", "source_runtime", "context_snapshot"}
        })
    repo = DatasetRepository(services.tx)
    saved = []
    async with services.tx.tenant_tx(task.tenant_id) as conn:
        await services.tasks.require_current(conn, task)
        for index, case in enumerate(generated):
            dataset_id = "synth-" + hashlib.sha256(f"{task.task_id}:{index}".encode()).hexdigest()[:32]
            case["skill_ids"] = [fixed["skill_id"]]
            document = make_document(
                dataset_id=dataset_id, name=str(case.get("metadata", {}).get("name") or dataset_id),
                skills=[{"skill_id": fixed["skill_id"], "revision": fixed["skill_revision"]}],
                cases=[case], source={"kind": "synthesized", "run_id": task.task_id},
            )
            files = [{key: material[key] for key in ("path", "content_b64") if key in material}
                     for material in case["materials"]]
            saved.append(await repo.save(conn, tenant_id=task.tenant_id, document=document, files=files))
        result = {"datasets": [{key: value for key, value in row.items() if key != "ingested_at"} for row in saved],
                  "count": len(saved), "source_session_count": len(fixed["document"]["cases"])}
        await history.finish(conn, "dataset", task.task_id, result)
    return HandlerResult.succeed(task.task_id)
