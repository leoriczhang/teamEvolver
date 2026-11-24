-- Worker-recovered inputs are immutable and separate from accepted requests.
CREATE TABLE {schema}.replay_preparations (
    tenant_id TEXT NOT NULL,
    run_id TEXT NOT NULL,
    input_hash TEXT NOT NULL,
    result JSONB NOT NULL,
    result_hash TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, run_id)
);
ALTER TABLE {schema}.replay_preparations ENABLE ROW LEVEL SECURITY;
ALTER TABLE {schema}.replay_preparations FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON {schema}.replay_preparations
    USING (tenant_id = NULLIF(current_setting('app.tenant_id', true), ''))
    WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), ''));
