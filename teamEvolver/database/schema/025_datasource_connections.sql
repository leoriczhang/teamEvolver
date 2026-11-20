-- Independent tenant connection heads with immutable adapter/settings revisions.
CREATE TABLE {schema}.datasource_connections (
    tenant_id TEXT NOT NULL REFERENCES {schema}.tenants(tenant_id) ON DELETE CASCADE,
    connection_id TEXT NOT NULL,
    revision BIGINT NOT NULL CHECK (revision>0),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id,connection_id)
);
CREATE TABLE {schema}.datasource_connection_versions (
    tenant_id TEXT NOT NULL,
    connection_id TEXT NOT NULL,
    revision BIGINT NOT NULL CHECK (revision>0),
    document JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id,connection_id,revision),
    FOREIGN KEY (tenant_id,connection_id) REFERENCES {schema}.datasource_connections(tenant_id,connection_id)
        ON DELETE CASCADE
);
DO $ddl$
DECLARE item TEXT;
BEGIN
    FOREACH item IN ARRAY ARRAY['datasource_connections','datasource_connection_versions'] LOOP
        EXECUTE format('ALTER TABLE {schema}.%I ENABLE ROW LEVEL SECURITY',item);
        EXECUTE format('ALTER TABLE {schema}.%I FORCE ROW LEVEL SECURITY',item);
        EXECUTE format('CREATE POLICY tenant_isolation ON {schema}.%I USING (tenant_id=NULLIF(current_setting(''app.tenant_id'',true),'''')) WITH CHECK (tenant_id=NULLIF(current_setting(''app.tenant_id'',true),''''))',item);
    END LOOP;
END
$ddl$;
