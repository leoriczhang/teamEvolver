-- Phase 7: incremental Context state and durable Memory checkpoints.
CREATE TABLE {schema}.context_sessions (
    tenant_id TEXT NOT NULL REFERENCES {schema}.tenants(tenant_id),
    user_id TEXT NOT NULL,
    session_id TEXT NOT NULL,
    external_hash TEXT NOT NULL,
    ov_session_id TEXT NOT NULL,
    ov_created BOOLEAN NOT NULL DEFAULT false,
    committed BOOLEAN NOT NULL DEFAULT false,
    result_hash TEXT NOT NULL DEFAULT '',
    last_sequence BIGINT NOT NULL DEFAULT 0,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY(tenant_id,user_id,session_id),
    UNIQUE(tenant_id,user_id,external_hash)
);
CREATE TABLE {schema}.context_events (
    tenant_id TEXT NOT NULL, user_id TEXT NOT NULL, session_id TEXT NOT NULL,
    event_id TEXT NOT NULL, sequence BIGINT NOT NULL, payload_hash TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY(tenant_id,user_id,session_id,event_id),
    UNIQUE(tenant_id,user_id,session_id,sequence),
    FOREIGN KEY(tenant_id,user_id,session_id)
        REFERENCES {schema}.context_sessions(tenant_id,user_id,session_id)
);
CREATE TABLE {schema}.context_refs (
    tenant_id TEXT NOT NULL REFERENCES {schema}.tenants(tenant_id),
    user_id TEXT NOT NULL, ref_id TEXT NOT NULL, session_id TEXT NOT NULL DEFAULT '',
    scope TEXT NOT NULL, uri TEXT NOT NULL, kind TEXT NOT NULL, version TEXT NOT NULL DEFAULT '',
    revoked BOOLEAN NOT NULL DEFAULT false,
    expires_at TIMESTAMPTZ NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY(tenant_id,user_id,ref_id)
);
CREATE TABLE {schema}.context_snapshots (
    tenant_id TEXT NOT NULL REFERENCES {schema}.tenants(tenant_id),
    user_id TEXT NOT NULL, snapshot_id TEXT NOT NULL, session_id TEXT NOT NULL DEFAULT '',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY(tenant_id,user_id,snapshot_id)
);
CREATE TABLE {schema}.context_snapshot_items (
    tenant_id TEXT NOT NULL, user_id TEXT NOT NULL, snapshot_id TEXT NOT NULL,
    ref_id TEXT NOT NULL, position INTEGER NOT NULL, item JSONB NOT NULL,
    PRIMARY KEY(tenant_id,user_id,snapshot_id,ref_id),
    FOREIGN KEY(tenant_id,user_id,snapshot_id)
        REFERENCES {schema}.context_snapshots(tenant_id,user_id,snapshot_id),
    FOREIGN KEY(tenant_id,user_id,ref_id)
        REFERENCES {schema}.context_refs(tenant_id,user_id,ref_id)
);
CREATE TABLE {schema}.context_reads (
    tenant_id TEXT NOT NULL, user_id TEXT NOT NULL, ref_id TEXT NOT NULL,
    level TEXT NOT NULL, value JSONB NOT NULL,
    PRIMARY KEY(tenant_id,user_id,ref_id,level),
    FOREIGN KEY(tenant_id,user_id,ref_id)
        REFERENCES {schema}.context_refs(tenant_id,user_id,ref_id)
);
CREATE TABLE {schema}.context_usage (
    tenant_id TEXT NOT NULL, user_id TEXT NOT NULL, session_id TEXT NOT NULL,
    usage_key TEXT NOT NULL,
    PRIMARY KEY(tenant_id,user_id,session_id,usage_key),
    FOREIGN KEY(tenant_id,user_id,session_id)
        REFERENCES {schema}.context_sessions(tenant_id,user_id,session_id)
);
CREATE TABLE {schema}.context_audit (
    tenant_id TEXT NOT NULL REFERENCES {schema}.tenants(tenant_id),
    user_id TEXT NOT NULL, id BIGSERIAL, action TEXT NOT NULL,
    subject_hash TEXT NOT NULL DEFAULT '',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY(tenant_id,user_id,id)
);
CREATE TABLE {schema}.memory_runs (
    tenant_id TEXT NOT NULL REFERENCES {schema}.tenants(tenant_id),
    run_id TEXT NOT NULL, target_uri TEXT NOT NULL, request_hash TEXT NOT NULL,
    input JSONB NOT NULL, input_hash TEXT NOT NULL,
    result JSONB, created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at TIMESTAMPTZ,
    PRIMARY KEY(tenant_id,run_id)
);
CREATE TABLE {schema}.memory_stages (
    tenant_id TEXT NOT NULL, run_id TEXT NOT NULL, stage_key TEXT NOT NULL,
    input_hash TEXT NOT NULL, upstream_task_id TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'pending', result_ref TEXT NOT NULL DEFAULT '',
    result JSONB NOT NULL DEFAULT '{}',
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY(tenant_id,run_id,stage_key),
    FOREIGN KEY(tenant_id,run_id) REFERENCES {schema}.memory_runs(tenant_id,run_id)
);
CREATE TABLE {schema}.memory_checkpoints (
    tenant_id TEXT NOT NULL REFERENCES {schema}.tenants(tenant_id),
    account_id TEXT NOT NULL, target_uri TEXT NOT NULL, group_key TEXT NOT NULL,
    source_hash TEXT NOT NULL, skill_revision TEXT NOT NULL, result_ref TEXT NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY(tenant_id,account_id,target_uri,group_key)
);
CREATE TABLE {schema}.memory_changes (
    tenant_id TEXT NOT NULL, change_id TEXT NOT NULL, run_id TEXT NOT NULL,
    target_uri TEXT NOT NULL, before_ref TEXT NOT NULL DEFAULT '', after_ref TEXT NOT NULL DEFAULT '',
    detail JSONB NOT NULL DEFAULT '{}', created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY(tenant_id,change_id),
    FOREIGN KEY(tenant_id,run_id) REFERENCES {schema}.memory_runs(tenant_id,run_id)
);
DO $ddl$
DECLARE t TEXT;
BEGIN
    FOREACH t IN ARRAY ARRAY[
        'context_sessions','context_events','context_refs','context_snapshots',
        'context_snapshot_items','context_reads','context_usage','context_audit',
        'memory_runs','memory_stages','memory_checkpoints','memory_changes'
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
