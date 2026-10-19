-- Durable transfer commands and immutable ZIP results. No local state authority.
CREATE TABLE {schema}.skill_transfers (
    tenant_id TEXT NOT NULL REFERENCES {schema}.tenants(tenant_id),
    command_id TEXT NOT NULL,
    operation TEXT NOT NULL CHECK (operation IN ('import', 'export')),
    request_hash TEXT NOT NULL,
    payload JSONB NOT NULL,
    payload_hash TEXT NOT NULL,
    checkpoint JSONB NOT NULL DEFAULT '{}',
    result JSONB,
    archive BYTEA,
    archive_hash TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at TIMESTAMPTZ,
    PRIMARY KEY (tenant_id, command_id),
    CHECK (archive IS NULL OR octet_length(archive) <= 67108864)
);
CREATE INDEX skill_transfer_recent_idx ON {schema}.skill_transfers(tenant_id, created_at DESC);
ALTER TABLE {schema}.skill_transfers ENABLE ROW LEVEL SECURITY;
ALTER TABLE {schema}.skill_transfers FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON {schema}.skill_transfers
USING (tenant_id = NULLIF(current_setting('app.tenant_id', true), ''))
WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), ''));
