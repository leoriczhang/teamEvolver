"""Short transactional Replay acceptance and immutable execution configuration."""
from __future__ import annotations

from .adapters.repository import ReplayAdapterRepository
from .config import load_replay_config
from .repository import ReplayRepository


async def enqueue(conn, services, *, tenant_id, domain, run_id, subject_id, request, spec=None,
                  dataset=None, options=None, operation="run"):
    repo = ReplayRepository(services.tx)
    config = await load_replay_config(services.extras["config_repo"], conn)
    binding = await ReplayAdapterRepository(services.tx).binding(conn)
    if binding:
        binding = {key: binding[key] for key in ("adapter_id", "revision", "source_hash")}
    payload = {
        "operation": operation, "config": config.config, "config_revision": config.revision,
        "adapter": binding, "spec": spec.as_dict() if spec else None, "dataset": dataset,
        "options": options or {}, "domain": domain,
    }
    record, created = await repo.accept(
        conn, tenant_id=tenant_id, domain=domain, run_id=run_id, subject_id=subject_id,
        request=request, payload=payload,
        dataset_id=dataset["document"]["dataset_id"] if dataset else "",
    )
    if created:
        await services.tasks.enqueue(
            conn, tenant_id=tenant_id, task_id=run_id, kind="dataset.run" if domain == "dataset" else "replay.run",
            subject_type=domain + "_replay", subject_id=subject_id, payload_ref=run_id,
            payload_hash=record["input_hash"], config_revision=config.revision,
            adapter_revision=binding["revision"] if binding else 0, dedupe_key=run_id,
        )
    return record
