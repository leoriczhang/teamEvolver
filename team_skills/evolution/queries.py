"""Public read-only Evidence eligibility for other Skill producers."""
from .repository import EvolutionRepository


async def archived_evidence_sessions(conn, tx, skill):
    return await EvolutionRepository(tx).archived_session_ids(conn, skill)
