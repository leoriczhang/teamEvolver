-- Tenant-isolated binary objects used by the PostgreSQL ObjectStore.
CREATE TABLE IF NOT EXISTS {schema}.objects (
    tenant_id TEXT NOT NULL DEFAULT 'default',
    key TEXT NOT NULL, content BYTEA NOT NULL,
    content_hash TEXT NOT NULL DEFAULT '', size BIGINT NOT NULL DEFAULT 0,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY(tenant_id,key)
);
ALTER TABLE {schema}.objects ENABLE ROW LEVEL SECURITY;
ALTER TABLE {schema}.objects FORCE ROW LEVEL SECURITY;
DO $ddl$
BEGIN
    IF NOT EXISTS(SELECT FROM pg_policies WHERE schemaname='{schema}'
        AND tablename='objects' AND policyname='tenant_isolation') THEN
        CREATE POLICY tenant_isolation ON {schema}.objects
            USING(tenant_id=NULLIF(current_setting('app.tenant_id',true),''))
            WITH CHECK(tenant_id=NULLIF(current_setting('app.tenant_id',true),''));
    END IF;
END
$ddl$;
