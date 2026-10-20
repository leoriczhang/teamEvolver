-- Dataset revisions and independent Skill/Memory/Dataset Replay histories.
CREATE TABLE {schema}.datasets (
    tenant_id TEXT NOT NULL REFERENCES {schema}.tenants(tenant_id) ON DELETE CASCADE,
    dataset_id TEXT NOT NULL,
    revision BIGINT NOT NULL DEFAULT 1,
    name TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    document_hash TEXT NOT NULL,
    deleted BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, dataset_id)
);
CREATE TABLE {schema}.dataset_revisions (
    tenant_id TEXT NOT NULL,
    dataset_id TEXT NOT NULL,
    revision BIGINT NOT NULL,
    document JSONB NOT NULL,
    document_hash TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, dataset_id, revision),
    FOREIGN KEY (tenant_id, dataset_id) REFERENCES {schema}.datasets(tenant_id, dataset_id)
);
CREATE TABLE {schema}.dataset_cases (
    tenant_id TEXT NOT NULL,
    dataset_id TEXT NOT NULL,
    revision BIGINT NOT NULL,
    case_id TEXT NOT NULL,
    position INTEGER NOT NULL,
    case_hash TEXT NOT NULL,
    payload JSONB NOT NULL,
    PRIMARY KEY (tenant_id, dataset_id, revision, case_id),
    FOREIGN KEY (tenant_id, dataset_id, revision)
        REFERENCES {schema}.dataset_revisions(tenant_id, dataset_id, revision)
);
CREATE TABLE {schema}.dataset_materials (
    tenant_id TEXT NOT NULL REFERENCES {schema}.tenants(tenant_id) ON DELETE CASCADE,
    sha256 TEXT NOT NULL,
    content BYTEA NOT NULL,
    size BIGINT NOT NULL CHECK(size >= 0),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, sha256)
);
DO $ddl$
DECLARE domain TEXT;
BEGIN
    FOREACH domain IN ARRAY ARRAY['skill', 'memory', 'dataset'] LOOP
        EXECUTE format(
            'CREATE TABLE {schema}.%I (
                tenant_id TEXT NOT NULL REFERENCES {schema}.tenants(tenant_id) ON DELETE CASCADE,
                run_id TEXT NOT NULL,
                subject_id TEXT NOT NULL,
                dataset_id TEXT NOT NULL DEFAULT '''',
                request_hash TEXT NOT NULL,
                input_hash TEXT NOT NULL,
                input JSONB NOT NULL,
                result JSONB,
                created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                finished_at TIMESTAMPTZ,
                PRIMARY KEY(tenant_id, run_id)
            )', domain || '_replay_runs');
        EXECUTE format(
            'CREATE TABLE {schema}.%I (
                tenant_id TEXT NOT NULL,
                run_id TEXT NOT NULL,
                case_id TEXT NOT NULL,
                input_hash TEXT NOT NULL,
                result JSONB NOT NULL,
                result_hash TEXT NOT NULL,
                created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                PRIMARY KEY(tenant_id, run_id, case_id),
                FOREIGN KEY(tenant_id, run_id) REFERENCES {schema}.%I(tenant_id, run_id)
            )', domain || '_replay_cases', domain || '_replay_runs');
    END LOOP;
END
$ddl$;
DO $ddl$
DECLARE t TEXT;
BEGIN
    FOREACH t IN ARRAY ARRAY[
        'datasets', 'dataset_revisions', 'dataset_cases', 'dataset_materials',
        'skill_replay_runs', 'skill_replay_cases', 'memory_replay_runs', 'memory_replay_cases',
        'dataset_replay_runs', 'dataset_replay_cases'
    ] LOOP
        EXECUTE format('ALTER TABLE {schema}.%I ENABLE ROW LEVEL SECURITY', t);
        EXECUTE format('ALTER TABLE {schema}.%I FORCE ROW LEVEL SECURITY', t);
        EXECUTE format(
            'CREATE POLICY tenant_isolation ON {schema}.%I '
            'USING (tenant_id = NULLIF(current_setting(''app.tenant_id'', true), '''')) '
            'WITH CHECK (tenant_id = NULLIF(current_setting(''app.tenant_id'', true), ''''))', t);
    END LOOP;
END
$ddl$;
