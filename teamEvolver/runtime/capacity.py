"""Live tenant execution limits, separate from frozen business job inputs."""
from __future__ import annotations

from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class TenantCapacity:
    collection_concurrency: int = 1
    ingestion_concurrency: int = 1
    analysis_concurrency: int = 2
    evolution_concurrency: int = 1
    replay_concurrency: int = 1
    background_concurrency: int = 1
    llm_max_concurrency: int = 8
    llm_queue_capacity: int = 64

    @classmethod
    def parse(cls, raw: dict) -> TenantCapacity:
        values = asdict(cls())
        unknown = set(raw) - set(values)
        if unknown:
            raise ValueError(f"unknown runtime keys: {', '.join(sorted(unknown))}")
        values.update(raw)
        for key, value in values.items():
            maximum = 10000 if key == "llm_queue_capacity" else 8 if key == "llm_max_concurrency" else 32
            if type(value) is not int or not 1 <= value <= maximum:
                raise ValueError(f"{key} must be an integer between 1 and {maximum}")
        if values["llm_queue_capacity"] < values["llm_max_concurrency"]:
            raise ValueError("llm_queue_capacity must be at least llm_max_concurrency")
        return cls(**values)

    def lanes(self, kinds) -> dict[str, tuple[tuple[str, ...], int]]:
        """Each ingestion stage reserves its own capacity."""
        groups = {
            "collection": {"session.pull", "ma.discover", "ma.collect", "ma.reconcile", "ma.skill"},
            "normalize": {"session.normalize"},
            "split": {"session.split"},
            "archive": {"session.archive"},
            "analysis": {"session.analyze"},
            "evolution": {"skill.evolve", "skill.transfer"},
            "replay": {"skill.validate", "replay.run", "dataset.run", "memory.replay"},
        }
        groups["background"] = set(kinds) - set.union(*groups.values())
        return {
            name: (tuple(sorted(group & set(kinds))), getattr(
                self, f"{'ingestion' if name in {'normalize', 'split', 'archive'} else name}_concurrency"))
            for name, group in groups.items() if group & set(kinds)
        }


async def read_capacity(services, tenant):
    async with services.tx.tenant_tx(tenant) as conn:
        row = await services.extras["config_repo"].read_tenant_module(conn, "runtime")
    return row, TenantCapacity.parse(row.config)
