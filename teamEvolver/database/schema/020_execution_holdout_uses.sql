-- A locked acceptance source family is exposed to only one candidate revision.
CREATE TABLE {schema}.execution_holdout_uses (
    tenant_id TEXT NOT NULL REFERENCES {schema}.tenants(tenant_id) ON DELETE CASCADE,
    profile_id TEXT NOT NULL,
    source_family TEXT NOT NULL,
    dataset_id TEXT NOT NULL,
    dataset_revision INTEGER NOT NULL CHECK (dataset_revision > 0),
    candidate_id TEXT NOT NULL,
    candidate_revision INTEGER NOT NULL CHECK (candidate_revision > 0),
    exposed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id,source_family),
    FOREIGN KEY (tenant_id,profile_id) REFERENCES {schema}.execution_profiles(tenant_id,profile_id)
);
ALTER TABLE {schema}.execution_holdout_uses ENABLE ROW LEVEL SECURITY;
ALTER TABLE {schema}.execution_holdout_uses FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON {schema}.execution_holdout_uses
    USING (tenant_id = NULLIF(current_setting('app.tenant_id',true),''))
    WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id',true),''));
