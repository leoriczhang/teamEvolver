-- Additive source identity and revision ledger. Historical Sessions remain immutable.
CREATE TABLE {schema}.session_source_heads (
    tenant_id TEXT NOT NULL REFERENCES {schema}.tenants(tenant_id) ON DELETE CASCADE,
    source_session_key TEXT NOT NULL,
    snapshot_revision TEXT NOT NULL,
    document JSONB NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id,source_session_key)
);
CREATE TABLE {schema}.session_source_snapshots (
    tenant_id TEXT NOT NULL REFERENCES {schema}.tenants(tenant_id) ON DELETE CASCADE,
    source_session_key TEXT NOT NULL,
    snapshot_revision TEXT NOT NULL,
    ingestion_id TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id,source_session_key,snapshot_revision),
    UNIQUE (tenant_id,ingestion_id)
);
CREATE TABLE {schema}.session_analysis_cache (
    tenant_id TEXT NOT NULL REFERENCES {schema}.tenants(tenant_id) ON DELETE CASCADE,
    cache_key TEXT NOT NULL,
    result_ref TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id,cache_key)
);
DO $ddl$
DECLARE item TEXT;
BEGIN
    FOREACH item IN ARRAY ARRAY['session_source_heads','session_source_snapshots','session_analysis_cache'] LOOP
        EXECUTE format('ALTER TABLE {schema}.%I ENABLE ROW LEVEL SECURITY',item);
        EXECUTE format('ALTER TABLE {schema}.%I FORCE ROW LEVEL SECURITY',item);
        EXECUTE format('CREATE POLICY tenant_isolation ON {schema}.%I USING (tenant_id=NULLIF(current_setting(''app.tenant_id'',true),'''')) WITH CHECK (tenant_id=NULLIF(current_setting(''app.tenant_id'',true),''''))',item);
    END LOOP;
END
$ddl$;
