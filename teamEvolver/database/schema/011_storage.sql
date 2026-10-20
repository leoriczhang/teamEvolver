-- Object payloads, receipts and reconstructible indices; no domain rows here.
CREATE TABLE {schema}.storage_objects (
    tenant_id TEXT NOT NULL REFERENCES {schema}.tenants(tenant_id),
    key TEXT NOT NULL, content BYTEA NOT NULL, content_hash TEXT NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY(tenant_id,key)
);
ALTER TABLE {schema}.storage_objects ENABLE ROW LEVEL SECURITY;
ALTER TABLE {schema}.storage_objects FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON {schema}.storage_objects
    USING(tenant_id=NULLIF(current_setting('app.tenant_id',true),''))
    WITH CHECK(tenant_id=NULLIF(current_setting('app.tenant_id',true),''));
