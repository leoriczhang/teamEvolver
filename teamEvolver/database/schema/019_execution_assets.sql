-- Independently versioned business Prompt/context assets and validated compositions.
CREATE TABLE {schema}.execution_profiles (
    tenant_id TEXT NOT NULL REFERENCES {schema}.tenants(tenant_id) ON DELETE CASCADE,
    profile_id TEXT NOT NULL,
    revision INTEGER NOT NULL DEFAULT 1 CHECK (revision > 0),
    document JSONB NOT NULL,
    current_release_id TEXT NOT NULL DEFAULT '',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, profile_id)
);
CREATE TABLE {schema}.execution_assets (
    tenant_id TEXT NOT NULL,
    profile_id TEXT NOT NULL,
    component_id TEXT NOT NULL,
    current_revision INTEGER NOT NULL CHECK (current_revision > 0),
    PRIMARY KEY (tenant_id, component_id),
    FOREIGN KEY (tenant_id, profile_id) REFERENCES {schema}.execution_profiles(tenant_id, profile_id)
);
CREATE TABLE {schema}.execution_asset_versions (
    tenant_id TEXT NOT NULL,
    component_id TEXT NOT NULL,
    revision INTEGER NOT NULL CHECK (revision > 0),
    component JSONB NOT NULL,
    actor TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, component_id, revision),
    FOREIGN KEY (tenant_id, component_id) REFERENCES {schema}.execution_assets(tenant_id, component_id)
);
CREATE TABLE {schema}.execution_releases (
    tenant_id TEXT NOT NULL,
    release_id TEXT NOT NULL,
    profile_id TEXT NOT NULL,
    composition_hash TEXT NOT NULL,
    document JSONB NOT NULL,
    candidate_id TEXT NOT NULL DEFAULT '',
    candidate_revision INTEGER,
    validation_ref TEXT NOT NULL DEFAULT '',
    actor TEXT NOT NULL,
    restored_from TEXT NOT NULL DEFAULT '',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, release_id),
    FOREIGN KEY (tenant_id, profile_id) REFERENCES {schema}.execution_profiles(tenant_id, profile_id)
);
CREATE INDEX execution_releases_profile_idx ON {schema}.execution_releases(tenant_id,profile_id,created_at DESC);
CREATE TABLE {schema}.execution_receipts (
    tenant_id TEXT NOT NULL,
    profile_id TEXT NOT NULL,
    request_id TEXT NOT NULL,
    release_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    document JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, profile_id, request_id),
    FOREIGN KEY (tenant_id, release_id) REFERENCES {schema}.execution_releases(tenant_id, release_id)
);
DO $ddl$
DECLARE t TEXT;
BEGIN
    FOREACH t IN ARRAY ARRAY['execution_profiles','execution_assets','execution_asset_versions',
                            'execution_releases','execution_receipts'] LOOP
        EXECUTE format('ALTER TABLE {schema}.%I ENABLE ROW LEVEL SECURITY', t);
        EXECUTE format('ALTER TABLE {schema}.%I FORCE ROW LEVEL SECURITY', t);
        EXECUTE format('CREATE POLICY tenant_isolation ON {schema}.%I '
            'USING (tenant_id = NULLIF(current_setting(''app.tenant_id'',true),'''')) '
            'WITH CHECK (tenant_id = NULLIF(current_setting(''app.tenant_id'',true),''''))', t);
    END LOOP;
END
$ddl$;
