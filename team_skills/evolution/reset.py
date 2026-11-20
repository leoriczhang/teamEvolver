"""Short transaction orchestration for retiring manual-update automatic assets."""
from __future__ import annotations

from team_skills.candidates.repository import CandidateRepository
from team_skills.evolution.repository import EvolutionRepository
from team_skills.library.repository import SkillLibraryRepository


async def retire_manual_commit(conn, tx, commit, *, tenant_id, actor="console"):
    """Only real, non-replayed changes retire candidates; no-op saves are harmless."""
    if commit.status != "published" or commit.replayed or not commit.previous_version:
        return []
    head = await SkillLibraryRepository(tx).get_skill(conn, commit.name, include_deleted=True)
    await retire_inputs(conn, tx, name=commit.name, boundary=head.get("evolution_reset_at"))
    return await CandidateRepository(tx).archive_for_skill(
        conn, tenant_id=tenant_id,
        skill_name=commit.name, actor=actor,
        reason=f"Manual Skill update to v{commit.version}; previous automatic assets archived",
    )


async def reset_skill_evolution(conn, tx, *, tenant_id, name, actor="console"):
    """Explicit reset for a legacy package, without inventing a content version."""
    head = await SkillLibraryRepository(tx).reset_evolution(conn, name)
    await retire_inputs(conn, tx, name=name, boundary=head["evolution_reset_at"])
    archived = await CandidateRepository(tx).archive_for_skill(
        conn, tenant_id=tenant_id, skill_name=name, actor=actor,
        reason=f"Manual reset at Skill v{head['version']}; previous automatic assets archived",
    )
    return {"name": name, "version": head["version"],
            "evolution_reset_at": head["evolution_reset_at"].isoformat(), "archived_candidates": archived}


async def retire_inputs(conn, tx, *, name, boundary):
    if boundary is None:
        return
    from team_replay.datasets.retirement import archive_skill_cases
    await EvolutionRepository(tx).archive_before_boundary(conn, name, boundary)
    await archive_skill_cases(conn, tx, skill_name=name, boundary=boundary)


async def retire_cloud_baseline(conn, tx, *, tenant_id, name, item):
    """Keep old imported versions, candidates and Evidence as historical assets."""
    from datetime import datetime
    boundary = datetime.fromisoformat(item["evolution_reset_at"])
    await retire_inputs(conn, tx, name=name, boundary=boundary)
    await CandidateRepository(tx).archive_for_skill(
        conn, tenant_id=tenant_id, skill_name=name, actor="ma-cloud",
        reason=f"Cloud Skill baseline changed to v{item['latest_version']}; previous automatic assets archived",
    )
