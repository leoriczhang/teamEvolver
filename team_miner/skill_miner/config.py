"""Skill-mining settings kept separately from the required tenant miner module."""

from __future__ import annotations

from dataclasses import dataclass

from teamEvolver.config_runtime import ConfigRepository

from .repository import MiningRevisionConflict, decode, encode

MINER_DEFAULTS = {
    "source_uri": "viking://resources/agent_knowledge_workspace/input/raw_knowledge_base",
    "target_uri": "viking://resources/agent_knowledge_workspace/output/processed_knowledge",
    "skill_uri": "viking://agent/skills/llm-wiki", "account_id": "", "user_id": "team",
}


def validate_miner(raw):
    if not isinstance(raw, dict) or set(raw) - set(MINER_DEFAULTS):
        raise ValueError("unknown Miner config field; credentials must use Secret Refs")
    value = {**MINER_DEFAULTS, **raw}
    for key in ("source_uri", "target_uri", "skill_uri"):
        root = value[key]
        prefix = "viking://agent/skills/" if key == "skill_uri" else "viking://resources/"
        if not isinstance(root, str) or not root.startswith(prefix):
            raise ValueError("Miner workspace roots or Skill namespace are invalid")
        relative = root[len(prefix):]
        if (not relative or "\\" in relative or "%" in relative or "\0" in relative
                or any(part in {"", ".", ".."} or part.startswith(".") for part in relative.split("/"))):
            raise ValueError("unsafe Miner workspace path")
    for key in ("account_id", "user_id"):
        if not isinstance(value[key], str) or len(value[key]) > 200 or any(c in value[key] for c in "\0\r\n"):
            raise ValueError("invalid Miner Account or User")
    return value


@dataclass(frozen=True)
class MiningSettings:
    miner_revision: int
    revision: int
    config: dict
    miner_config: dict


async def load(services, conn) -> MiningSettings:
    """Require the tenant module; only the optional Skill settings start empty."""
    configs = services.extras.get("config_repo") or ConfigRepository(services.tx)
    miner = await configs.read_tenant_module(conn, "miner")
    row = await conn.fetchrow(
        f"SELECT revision,document FROM {services.tx.pool.schema}.skill_mining_settings "
        "WHERE tenant_id=NULLIF(current_setting('app.tenant_id',true),'')",
    )
    return MiningSettings(miner.revision, int(row["revision"]) if row else 0,
                          decode(row["document"]) if row else {}, validate_miner(miner.config))


async def save(services, conn, value, *, expected_revision: int) -> MiningSettings:
    current = await load(services, conn)
    if current.revision != expected_revision:
        raise MiningRevisionConflict("Skill mining settings changed; reload before saving")
    if not isinstance(value, dict) or set(value) - {"mining"}:
        raise ValueError("Skill mining settings only accept mining fields")
    from .settings import settings_payload
    settings_payload(value)
    if expected_revision == 0:
        revision = await conn.fetchval(
            f"INSERT INTO {services.tx.pool.schema}.skill_mining_settings (tenant_id,document) "
            "VALUES (NULLIF(current_setting('app.tenant_id',true),''),$1::jsonb) "
            "ON CONFLICT (tenant_id) DO NOTHING RETURNING revision", encode(value),
        )
    else:
        revision = await conn.fetchval(
            f"UPDATE {services.tx.pool.schema}.skill_mining_settings SET document=$1::jsonb,"
            "revision=revision+1,updated_at=now() "
            "WHERE tenant_id=NULLIF(current_setting('app.tenant_id',true),'') AND revision=$2 RETURNING revision",
            encode(value), expected_revision,
        )
    if revision is None:
        raise MiningRevisionConflict("Skill mining settings changed; reload before saving")
    return MiningSettings(current.miner_revision, int(revision), value, current.miner_config)


def freeze(settings: MiningSettings) -> dict:
    """Carry both revisions and full configuration in every accepted job."""
    return {"miner_revision": settings.miner_revision, "settings_revision": settings.revision,
            "miner": dict(settings.miner_config), "settings": decode(settings.config)}
