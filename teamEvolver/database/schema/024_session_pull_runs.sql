-- Track exactly which immutable source snapshots each pull observed/reused.
-- Historical runs deliberately remain untracked rather than inventing lineage.
CREATE TABLE {schema}.session_pull_runs (
    tenant_id TEXT NOT NULL,
    run_id TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id,run_id),
    FOREIGN KEY (tenant_id,run_id) REFERENCES {schema}.ingestions(tenant_id,ingestion_id) ON DELETE CASCADE
);
CREATE TABLE {schema}.session_pull_members (
    tenant_id TEXT NOT NULL,
    run_id TEXT NOT NULL,
    external_id TEXT NOT NULL CHECK (external_id<>''),
    acceptance TEXT NOT NULL CHECK (acceptance IN ('enqueued','duplicate','error','empty')),
    ingestion_id TEXT,
    source_session_key TEXT NOT NULL DEFAULT '',
    snapshot_revision TEXT NOT NULL DEFAULT '',
    error_code TEXT NOT NULL DEFAULT '',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id,run_id,external_id),
    FOREIGN KEY (tenant_id,run_id) REFERENCES {schema}.session_pull_runs(tenant_id,run_id) ON DELETE CASCADE,
    FOREIGN KEY (tenant_id,ingestion_id) REFERENCES {schema}.ingestions(tenant_id,ingestion_id),
    CHECK ((acceptance IN ('enqueued','duplicate'))=(ingestion_id IS NOT NULL))
);
CREATE INDEX session_pull_members_ingestion_idx ON {schema}.session_pull_members(tenant_id,ingestion_id);
DO $ddl$
DECLARE item TEXT;
BEGIN
    FOREACH item IN ARRAY ARRAY['session_pull_runs','session_pull_members'] LOOP
        EXECUTE format('ALTER TABLE {schema}.%I ENABLE ROW LEVEL SECURITY',item);
        EXECUTE format('ALTER TABLE {schema}.%I FORCE ROW LEVEL SECURITY',item);
        EXECUTE format('CREATE POLICY tenant_isolation ON {schema}.%I USING (tenant_id=NULLIF(current_setting(''app.tenant_id'',true),'''')) WITH CHECK (tenant_id=NULLIF(current_setting(''app.tenant_id'',true),''''))',item);
    END LOOP;
END
$ddl$;
