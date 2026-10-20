-- Immutable accepted commands, resumable evolution branches and audited history.
CREATE TABLE {schema}.skill_commands (
    tenant_id TEXT NOT NULL REFERENCES {schema}.tenants(tenant_id) ON DELETE CASCADE,
    command_id TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('skill.evolve', 'skill.validate')),
    request_hash TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    payload JSONB NOT NULL,
    result JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at TIMESTAMPTZ,
    PRIMARY KEY (tenant_id, command_id)
);

CREATE TABLE {schema}.skill_evolution_branches (
    tenant_id TEXT NOT NULL,
    command_id TEXT NOT NULL,
    branch_key TEXT NOT NULL,
    session_ids TEXT[] NOT NULL,
    result JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, command_id, branch_key),
    FOREIGN KEY (tenant_id, command_id)
        REFERENCES {schema}.skill_commands(tenant_id, command_id) ON DELETE CASCADE
);

CREATE TABLE {schema}.skill_evolution_sessions (
    tenant_id TEXT NOT NULL REFERENCES {schema}.tenants(tenant_id) ON DELETE CASCADE,
    session_id TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    command_id TEXT NOT NULL,
    consumed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, session_id, content_hash)
);

CREATE TABLE {schema}.skill_evidence (
    tenant_id TEXT NOT NULL REFERENCES {schema}.tenants(tenant_id) ON DELETE CASCADE,
    skill_name TEXT NOT NULL,
    session_id TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    projection JSONB NOT NULL,
    captured_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, skill_name, session_id)
);
CREATE INDEX skill_evidence_recent_idx
    ON {schema}.skill_evidence(tenant_id, skill_name, captured_at DESC);
CREATE INDEX skill_commands_history_idx
    ON {schema}.skill_commands(tenant_id, kind, created_at DESC);
CREATE INDEX skill_candidates_published_version_idx
    ON {schema}.skill_candidates(tenant_id, skill_name, published_version)
    WHERE status = 'published';

DO $ddl$
DECLARE t TEXT;
BEGIN
    FOREACH t IN ARRAY ARRAY[
        'skill_commands', 'skill_evolution_branches', 'skill_evolution_sessions', 'skill_evidence'
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
