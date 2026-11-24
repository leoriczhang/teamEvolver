-- Live capacity belongs to each tenant and is not frozen into business jobs.
DO $ddl$
DECLARE
    tenant RECORD;
BEGIN
    FOR tenant IN SELECT tenant_id FROM {schema}.tenants LOOP
        PERFORM set_config('app.tenant_id', tenant.tenant_id, true);
        INSERT INTO {schema}.tenant_configs (tenant_id, module, revision, config)
        VALUES (tenant.tenant_id, 'runtime', 1,
            '{"collection_concurrency":1,"ingestion_concurrency":1,"analysis_concurrency":2,
              "evolution_concurrency":1,"replay_concurrency":1,"background_concurrency":1,
              "llm_max_concurrency":8,"llm_queue_capacity":64}'::jsonb)
        ON CONFLICT (tenant_id, module) DO NOTHING;
    END LOOP;
    PERFORM set_config('app.tenant_id', '', true);
END
$ddl$;
