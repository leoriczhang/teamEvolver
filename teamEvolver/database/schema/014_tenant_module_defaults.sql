-- Every tenant owns one row per business module (plan 4.1 "缺失租户配置").
-- Module loaders merge their own defaults over the stored object, so an empty
-- object is the default row. tenant_configs is FORCE RLS: bind each tenant
-- before its insert (transaction-local).
DO $ddl$
DECLARE
    tenant RECORD;
BEGIN
    FOR tenant IN SELECT tenant_id FROM {schema}.tenants LOOP
        PERFORM set_config('app.tenant_id', tenant.tenant_id, true);
        INSERT INTO {schema}.tenant_configs (tenant_id, module, revision, config)
        SELECT tenant.tenant_id, module, 1, '{}'::jsonb
        FROM unnest(ARRAY['session', 'skills', 'replay', 'memory', 'miner']) AS module
        ON CONFLICT (tenant_id, module) DO NOTHING;
    END LOOP;
    PERFORM set_config('app.tenant_id', '', true);
END
$ddl$;
