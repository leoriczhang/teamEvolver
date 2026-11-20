"""Archive automatic Cases after Skill updates while retaining their inputs."""
from .repository import DatasetRepository


async def archive_skill_cases(conn, tx, *, skill_name, boundary):
    await DatasetRepository(tx).archive_before_boundary(conn, skill_id=skill_name, boundary=boundary)
