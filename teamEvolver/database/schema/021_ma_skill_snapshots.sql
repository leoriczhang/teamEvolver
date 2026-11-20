-- Immutable MA byte snapshots are validation/cache artifacts, never Skill heads.
CREATE TABLE {schema}.ma_skill_snapshots (
    tenant_id TEXT NOT NULL,
    connection_id TEXT NOT NULL,
    skill_id TEXT NOT NULL,
    version INTEGER NOT NULL CHECK (version > 0),
    tree_sha256 TEXT NOT NULL,
    metadata JSONB NOT NULL,
    bundle JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, connection_id, skill_id, version),
    FOREIGN KEY (tenant_id, connection_id) REFERENCES {schema}.ma_connections(tenant_id, connection_id)
);
ALTER TABLE {schema}.ma_skill_snapshots ENABLE ROW LEVEL SECURITY;
ALTER TABLE {schema}.ma_skill_snapshots FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON {schema}.ma_skill_snapshots
    USING (tenant_id = NULLIF(current_setting('app.tenant_id', true), ''))
    WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), ''));
