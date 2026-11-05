-- Managed Agents connections consume published Skills by pull.
CREATE TABLE {schema}.ma_connections (
    tenant_id TEXT NOT NULL REFERENCES {schema}.tenants(tenant_id),
    connection_id TEXT NOT NULL,
    target_fingerprint TEXT NOT NULL UNIQUE,
    revision INTEGER NOT NULL DEFAULT 1 CHECK (revision > 0),
    status TEXT NOT NULL DEFAULT 'discovering',
    enabled BOOLEAN NOT NULL DEFAULT true,
    detail JSONB NOT NULL DEFAULT '{}',
    last_collected_at TIMESTAMPTZ,
    last_synced_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, connection_id)
);
CREATE TABLE {schema}.ma_connection_revisions (
    tenant_id TEXT NOT NULL,
    connection_id TEXT NOT NULL,
    revision INTEGER NOT NULL,
    config JSONB NOT NULL,
    config_hash TEXT NOT NULL,
    actor TEXT NOT NULL DEFAULT '',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, connection_id, revision),
    FOREIGN KEY (tenant_id, connection_id) REFERENCES {schema}.ma_connections(tenant_id, connection_id)
);
CREATE TABLE {schema}.ma_resource_bindings (
    tenant_id TEXT NOT NULL,
    connection_id TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('agent', 'skill')),
    logical_name TEXT NOT NULL,
    revision INTEGER NOT NULL DEFAULT 1,
    record JSONB NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, connection_id, kind, logical_name),
    FOREIGN KEY (tenant_id, connection_id) REFERENCES {schema}.ma_connections(tenant_id, connection_id)
);
CREATE TABLE {schema}.ma_session_cursors (
    tenant_id TEXT NOT NULL,
    connection_id TEXT NOT NULL,
    session_id TEXT NOT NULL,
    record JSONB NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, connection_id, session_id),
    FOREIGN KEY (tenant_id, connection_id) REFERENCES {schema}.ma_connections(tenant_id, connection_id)
);
CREATE TABLE {schema}.ma_operations (
    tenant_id TEXT NOT NULL,
    connection_id TEXT NOT NULL,
    operation_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    input JSONB NOT NULL,
    input_hash TEXT NOT NULL,
    checkpoint JSONB NOT NULL DEFAULT '{}',
    result JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, operation_id),
    FOREIGN KEY (tenant_id, connection_id) REFERENCES {schema}.ma_connections(tenant_id, connection_id)
);
CREATE TABLE {schema}.ma_releases (
    tenant_id TEXT NOT NULL,
    connection_id TEXT NOT NULL,
    release_id TEXT NOT NULL,
    manifest_hash TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    record JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    applied_at TIMESTAMPTZ,
    PRIMARY KEY (tenant_id, connection_id, release_id),
    FOREIGN KEY (tenant_id, connection_id) REFERENCES {schema}.ma_connections(tenant_id, connection_id)
);
CREATE INDEX ma_release_applied_idx ON {schema}.ma_releases(tenant_id, connection_id, applied_at DESC)
    WHERE status='applied';
CREATE TABLE {schema}.ma_external_effects (
    tenant_id TEXT NOT NULL,
    connection_id TEXT NOT NULL,
    effect_id TEXT NOT NULL,
    owner_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    request_hash TEXT NOT NULL,
    intent JSONB NOT NULL,
    status TEXT NOT NULL DEFAULT 'outcome_unknown',
    remote_id TEXT NOT NULL DEFAULT '',
    receipt JSONB NOT NULL DEFAULT '{}',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, effect_id),
    FOREIGN KEY (tenant_id, connection_id) REFERENCES {schema}.ma_connections(tenant_id, connection_id)
);
DO $ma_rls$
DECLARE table_name TEXT;
BEGIN
    FOREACH table_name IN ARRAY ARRAY[
        'ma_connections', 'ma_connection_revisions', 'ma_resource_bindings',
        'ma_session_cursors', 'ma_operations', 'ma_releases', 'ma_external_effects'
    ] LOOP
        EXECUTE format('ALTER TABLE {schema}.%I ENABLE ROW LEVEL SECURITY', table_name);
        EXECUTE format('ALTER TABLE {schema}.%I FORCE ROW LEVEL SECURITY', table_name);
        EXECUTE format(
            'CREATE POLICY tenant_isolation ON {schema}.%I '
            'USING (tenant_id = NULLIF(current_setting(''app.tenant_id'', true), '''')) '
            'WITH CHECK (tenant_id = NULLIF(current_setting(''app.tenant_id'', true), ''''))', table_name
        );
    END LOOP;
END $ma_rls$;
