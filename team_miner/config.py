"""Required tenant Miner config with explicit workspace boundaries."""

from __future__ import annotations

DEFAULTS = {
    "source_uri": "viking://resources/agent_knowledge_workspace/input/raw_knowledge_base",
    "target_uri": "viking://resources/agent_knowledge_workspace/output/processed_knowledge",
    "skill_uri": "viking://agent/skills/llm-wiki",
    "account_id": "", "user_id": "team",
}


async def load(configs, conn):
    row = await configs.read_tenant_module(conn, "miner")
    if any("key" in k or "password" in k or "token" in k for k in row.config):
        raise ValueError("Miner config must use secret refs")
    if set(row.config) - set(DEFAULTS):
        raise ValueError("unknown Miner config field")
    value = {**DEFAULTS, **row.config}
    from .schemas import relative_path
    for key in ("source_uri", "target_uri"):
        root = str(value[key])
        if not root.startswith("viking://resources/"):
            raise ValueError("Miner roots must be under viking://resources")
        relative_path(root[len("viking://resources/"):])
    skill = str(value["skill_uri"])
    if not skill.startswith("viking://agent/skills/"):
        raise ValueError("Miner Skill must be in shared Skill namespace")
    relative_path(skill[len("viking://agent/skills/"):])
    return value, row.revision
